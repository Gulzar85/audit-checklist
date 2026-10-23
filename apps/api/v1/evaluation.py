"""``/api/v1/checklist-versions/{id}/evaluation/``, ``/api/v1/executions/{id}/evaluation/``, ``/api/v1/evaluation-runs/{id}/`` and ``/api/v1/evaluation/metadata/`` (Phase 16).

Thin adapters: visibility comes from the checklist / execution selectors (an invisible object is a 404), authorization from ``apps.evaluation.policies`` (403) and
every write from ``apps.evaluation.services`` - the same code the web UI uses.  There is no endpoint that writes a result, an outcome or an action request: runs are
produced by the server from the stored answers and the stored score.  Unknown fields are rejected, so a client-supplied ``outcome`` / ``evaluation`` is an error,
not ignored; actions cannot be executed directly (only a published rule can request them, and only a stored final run runs them).  A published version's rules are
read-only (409).  The condition-level explanation (observed values) needs ``evaluation.explain``.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.api.exceptions import ConflictAPIException, StructuredValidationError
from apps.api.permissions import IsActiveUser
from apps.checklists import selectors as checklist_selectors
from apps.checklists.rules.operators import describe_all as describe_operators
from apps.core.utils import parse_uuid
from apps.evaluation import config as cfg
from apps.evaluation import constants as k
from apps.evaluation import policies, selectors, services, sources
from apps.evaluation.models import EvaluationRun
from apps.executions import selectors as execution_selectors

CONFLICT_CODES = {"version_immutable", "template_archived", "not_submitted", "concurrent_evaluation"}


def run_service(call):  # noqa: ANN001, ANN201
    try:
        return call()
    except DjangoPermissionDenied as exc:
        raise PermissionDenied from exc
    except services.EvaluationError as exc:
        if isinstance(exc, services.EvaluationConflict) or exc.code in CONFLICT_CODES:
            raise ConflictAPIException(detail={"detail": exc.message, "code": exc.code}) from exc
        raise StructuredValidationError({"__all__": [exc.message]}, extra={"code": exc.code, **exc.extra}) from exc


class _Strict(serializers.Serializer):
    def to_internal_value(self, data):  # noqa: ANN001, ANN201
        unknown = {key: "This field cannot be set." for key in data if key not in self.fields}
        if unknown:
            raise serializers.ValidationError(unknown)
        return super().to_internal_value(data)


class ConfigIn(_Strict):
    enabled = serializers.BooleanField(required=False)
    document = serializers.DictField(required=False)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=500)


class ValidateIn(_Strict):
    enabled = serializers.BooleanField(required=False)
    document = serializers.DictField(required=False)


class PreviewIn(_Strict):
    enabled = serializers.BooleanField(required=False)
    document = serializers.DictField(required=False)
    answers = serializers.DictField(required=False, default=dict)
    not_applicable = serializers.ListField(child=serializers.CharField(max_length=40), required=False, default=list)
    score = serializers.DictField(required=False)
    execution_status = serializers.CharField(required=False, default="submitted", max_length=20)


class EvaluateIn(_Strict):
    reason = serializers.CharField(required=False, allow_blank=True, max_length=500)


def _version(request: Request, pk: str):  # noqa: ANN202
    parsed = parse_uuid(pk)
    version = checklist_selectors.get_version_for_user(request.user, parsed) if parsed else None
    if version is None:
        raise NotFound
    return version


def _execution(request: Request, pk: str):  # noqa: ANN202
    execution = execution_selectors.get_execution(request.user, pk)
    if execution is None:
        raise NotFound
    return execution


def issue_list(issues: list[cfg.Issue]) -> list[dict[str, str]]:
    return [i.as_dict() for i in issues]


def config_out(version, row, *, user) -> dict[str, Any]:  # noqa: ANN001
    base = {"version": str(version.pk), "version_number": version.version_number, "status": version.status, "editable": policies.can_configure(user, version),
            "configured": row is not None}
    if row is None:
        return {**base, "enabled": True, "document": cfg.empty_document(), "notes": "", "content_hash": None, "issues": []}
    issues, _compiled, _doc = services.validate_config(version)
    return {**base, "enabled": row.enabled, "document": row.document, "notes": row.notes, "content_hash": row.content_hash, "issues": issue_list(issues)}


def _visible_ref(user: Any, request_row: Any) -> dict[str, Any] | None:
    """The finding / workflow instance an action produced - only when the caller may see it."""
    if request_row.result_type == "finding":
        from apps.findings import selectors as finding_selectors

        found = finding_selectors.get_finding(user, request_row.result_id)
        return {"type": "finding", "id": str(found.pk), "reference": found.reference, "status": found.status} if found else None
    if request_row.result_type == "workflow":
        from apps.workflows import selectors as workflow_selectors

        instance = workflow_selectors.instances_visible_to(user).filter(pk=parse_uuid(request_row.result_id)).first()
        return {"type": "workflow", "id": str(instance.pk), "reference": instance.reference, "status": instance.status} if instance else None
    return None


def action_out(user: Any, a: Any) -> dict[str, Any]:
    return {"id": str(a.pk), "rule": a.rule_code, "index": a.action_index, "type": a.action_type, "status": a.status, "message": a.message, "error": a.error,
            "tag": a.result_id if a.result_type == "tag" else None, "result": _visible_ref(user, a), "finished_at": a.finished_at}


def run_out(r: EvaluationRun, *, detail: bool, user: Any = None) -> dict[str, Any]:
    d = r.detail or {}
    rules = {x["code"]: x for x in d.get("rules", [])}
    out = {"id": str(r.pk), "applicable": True, "stored": True, "outcome": r.outcome, "trigger": r.trigger, "is_current": r.is_current, "calculated_at": r.created_at,
           "version": str(r.version_id), "ruleset_hash": r.ruleset_hash, "engine_version": r.engine_version, "execution_revision": r.execution_revision,
           "score_result": str(r.score_result_id) if r.score_result_id else None, "reason": r.reason, "created_by": str(r.created_by_id) if r.created_by_id else None,
           "matched": [{"code": c, "name": rules.get(c, {}).get("name", c), "outcome": rules.get(c, {}).get("outcome")} for c in d.get("matched", [])],
           "undecided": list(d.get("undecided", []))}
    if detail:
        out.update({"rules": d.get("rules", []), "explanation": d.get("explanation", []), "actions": [action_out(user, a) for a in selectors.actions_for(r)]})
    return out


def evaluation_out(e: Any, *, detail: bool) -> dict[str, Any]:
    d = e.as_dict()
    out = {"applicable": True, "stored": False, "provisional": True, "outcome": d["outcome"], "engine_version": d["engine_version"],
           "matched": [{"code": c, "name": next((r["name"] for r in d["rules"] if r["code"] == c), c)} for c in d["matched"]], "undecided": d["undecided"]}
    if detail:
        out.update({"rules": d["rules"], "explanation": d["explanation"], "actions": []})
    return out


class MetadataView(APIView):
    permission_classes = [IsActiveUser]

    @extend_schema(responses=dict, summary="The finite vocabularies of an evaluation rule set")
    def get(self, request: Request) -> Response:
        return Response({
            "sources": list(k.SOURCES), "fields": sources.describe_fields(), "operators": describe_operators()["operators"],
            "outcomes": [{"value": v, "label": str(label)} for v, label in k.Outcome.choices], "rule_outcomes": list(k.RULE_OUTCOMES),
            "precedence": {str(o): rank for o, rank in k.PRECEDENCE.items()}, "on_unknown": [{"value": v, "label": str(label)} for v, label in k.OnUnknown.choices],
            "actions": [{"value": v, "label": str(label)} for v, label in k.ActionType.choices], "triggers": [{"value": v, "label": str(label)} for v, label in k.Trigger.choices],
            "groups": ["AND", "OR", "NOT"], "limits": {"rules": k.MAX_RULES, "conditions": k.MAX_CONDITIONS, "depth": k.MAX_DEPTH, "actions": k.MAX_ACTIONS, "message": k.MAX_MESSAGE},
            "engine_version": k.ENGINE_VERSION,
        })


class VersionEvaluationView(APIView):
    """``GET`` the rule set, ``PUT`` the draft's rule set (``enabled`` / ``document`` replace the given parts), ``DELETE`` it."""

    permission_classes = [IsActiveUser]

    @extend_schema(responses=dict, summary="A checklist version's evaluation rules")
    def get(self, request: Request, pk: str) -> Response:
        version = _version(request, pk)
        if not policies.can_view_config(request.user, version):
            raise PermissionDenied
        return Response(config_out(version, selectors.get_ruleset(version), user=request.user))

    @extend_schema(request=ConfigIn, responses=dict, summary="Save the evaluation rules of a draft version")
    def put(self, request: Request, pk: str) -> Response:
        version = _version(request, pk)
        data = ConfigIn(data=request.data)
        data.is_valid(raise_exception=True)
        d = data.validated_data
        row = run_service(lambda: services.save_config(version, actor=request.user, enabled=d.get("enabled"), document=d.get("document"), notes=d.get("notes")))
        return Response(config_out(version, row, user=request.user))

    @extend_schema(responses=None, summary="Remove the evaluation rules of a draft version")
    def delete(self, request: Request, pk: str) -> Response:
        version = _version(request, pk)
        run_service(lambda: services.remove_config(version, actor=request.user))
        return Response(status=204)


class VersionEvaluationValidateView(APIView):
    permission_classes = [IsActiveUser]

    @extend_schema(request=ValidateIn, responses=dict, summary="Validate the saved (or a candidate) rule set against the version's questions")
    def post(self, request: Request, pk: str) -> Response:
        version = _version(request, pk)
        if not policies.can_configure_at(request.user, version):
            raise PermissionDenied
        data = ValidateIn(data=request.data)
        data.is_valid(raise_exception=True)
        issues, compiled, _doc = run_service(lambda: services.validate_config(version, enabled=data.validated_data.get("enabled"), document=data.validated_data.get("document")))
        return Response({"ok": not any(i.level == "error" for i in issues), "issues": issue_list(issues), "rules": len(compiled)})


class VersionEvaluationPreviewView(APIView):
    """Evaluate synthetic sample answers and score facts with the authoritative engine.  Non-production: nothing is stored and no action runs."""

    permission_classes = [IsActiveUser]

    @extend_schema(request=PreviewIn, responses=dict, summary="What-if evaluation of sample inputs")
    def post(self, request: Request, pk: str) -> Response:
        version = _version(request, pk)
        if not policies.can_configure_at(request.user, version):
            raise PermissionDenied
        data = PreviewIn(data=request.data)
        data.is_valid(raise_exception=True)
        d = data.validated_data
        result = run_service(lambda: services.preview_sample(version, enabled=d.get("enabled"), document=d.get("document"), answers=d["answers"], not_applicable=d["not_applicable"],
                                                             score=d.get("score"), execution_status=d["execution_status"]))
        evaluation = result["evaluation"]
        return Response({"valid": result["valid"], "issues": result["issues"], "non_production": True, "result": evaluation_out(evaluation, detail=True) if evaluation else None,
                         "would_request": [{"rule": a["rule"], "index": a["index"], "type": a["action"]["type"]} for a in evaluation.actions] if evaluation else []})


class ExecutionEvaluationView(APIView):
    """The evaluation of an execution: the stored current run, or a provisional live computation while it is in progress."""

    permission_classes = [IsActiveUser]
    detail = False

    @extend_schema(responses=dict, summary="An execution's evaluation")
    def get(self, request: Request, pk: str) -> Response:
        execution = _execution(request, pk)
        allowed = policies.can_explain if self.detail else policies.can_view_result
        if not allowed(request.user, execution):
            raise PermissionDenied
        view = services.evaluation_of(execution)
        if not view["applicable"]:
            return Response({"applicable": False, "reason": view["reason"]})
        if view.get("pending"):
            return Response({"applicable": True, "stored": False, "pending": True, "outcome": "pending"})
        if view["stored"]:
            return Response(run_out(view["run"], detail=self.detail, user=request.user))
        if not policies.can_preview(request.user, execution):
            raise PermissionDenied
        return Response(evaluation_out(view["evaluation"], detail=self.detail))


class ExecutionEvaluationDetailView(ExecutionEvaluationView):
    detail = True


class ExecutionEvaluationRunsView(APIView):
    """Every stored run of the execution (the original and any re-evaluations), newest first."""

    permission_classes = [IsActiveUser]

    @extend_schema(responses=dict, summary="An execution's evaluation history")
    def get(self, request: Request, pk: str) -> Response:
        execution = _execution(request, pk)
        if not policies.can_view_result(request.user, execution):
            raise PermissionDenied
        return Response({"results": [run_out(r, detail=False) for r in selectors.runs_for(execution)]})


class ExecutionEvaluateView(APIView):
    """``POST``: a provisional refresh while in progress (``evaluation.evaluate``), or an authorized re-evaluation of a submitted execution (``evaluation.reevaluate`` + a reason)."""

    permission_classes = [IsActiveUser]

    @extend_schema(request=EvaluateIn, responses=dict, summary="Evaluate or re-evaluate an execution")
    def post(self, request: Request, pk: str) -> Response:
        execution = _execution(request, pk)
        data = EvaluateIn(data=request.data)
        data.is_valid(raise_exception=True)
        if policies.can_preview(request.user, execution):
            view = services.evaluation_of(execution)
            if not view["applicable"]:
                return Response({"applicable": False, "reason": view["reason"]})
            return Response(evaluation_out(view["evaluation"], detail=True))
        run_, changed = run_service(lambda: services.reevaluate(execution, actor=request.user, reason=data.validated_data.get("reason", "")))
        detail = policies.can_explain(request.user, execution)
        return Response({**run_out(run_, detail=detail, user=request.user), "changed": changed})


class EvaluationRunDetailView(APIView):
    """One stored run: the summary with ``evaluation.result_view``; rules, conditions, explanations and action outcomes with ``evaluation.explain``."""

    permission_classes = [IsActiveUser]

    @extend_schema(responses=dict, summary="An evaluation run")
    def get(self, request: Request, pk: str) -> Response:
        run_ = selectors.get_run(request.user, pk)
        if run_ is None:
            raise NotFound
        if not policies.can_view_result(request.user, run_.execution):
            raise PermissionDenied
        return Response(run_out(run_, detail=policies.can_explain(request.user, run_.execution), user=request.user))
