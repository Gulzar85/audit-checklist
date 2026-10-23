"""``/api/v1/checklist-versions/{id}/scoring/``, ``/api/v1/executions/{id}/score/`` and ``/api/v1/scoring/metadata/`` (Phase 15).

Thin adapters: visibility comes from the checklist / execution selectors (an invisible object is a 404), authorization from
``apps.scoring.policies`` (403) and every write from ``apps.scoring.services`` - the same code the web UI uses.  There is no endpoint that
writes a score, a grade, a total or a result: results are computed by the server from the stored answers.  Unknown fields are rejected, so a
client-supplied ``score`` / ``grade`` / ``percentage`` is an error, not ignored.  A published version's scoring is read-only (409).
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
from apps.core.utils import parse_uuid
from apps.executions import selectors as execution_selectors
from apps.scoring import config as cfg
from apps.scoring import constants as k
from apps.scoring import policies, selectors, services
from apps.scoring.models import ScoreResult

CONFLICT_CODES = {"version_immutable", "template_archived", "not_submitted", "concurrent_recalculation"}


def run(call):  # noqa: ANN001, ANN201
    try:
        return call()
    except DjangoPermissionDenied as exc:
        raise PermissionDenied from exc
    except services.ScoringError as exc:
        if isinstance(exc, services.ScoringConflict) or exc.code in CONFLICT_CODES:
            raise ConflictAPIException(detail={"detail": exc.message, "code": exc.code}) from exc
        raise StructuredValidationError({"__all__": [exc.message]}, extra={"code": exc.code, **exc.extra}) from exc


class _Strict(serializers.Serializer):
    def to_internal_value(self, data):  # noqa: ANN001, ANN201
        unknown = {key: "This field cannot be set." for key in data if key not in self.fields}
        if unknown:
            raise serializers.ValidationError(unknown)
        return super().to_internal_value(data)


class ConfigIn(_Strict):
    settings = serializers.DictField(required=False)
    document = serializers.DictField(required=False)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=500)


class PreviewIn(_Strict):
    settings = serializers.DictField(required=False)
    document = serializers.DictField(required=False)
    answers = serializers.DictField(required=False, default=dict)
    not_applicable = serializers.ListField(child=serializers.CharField(max_length=40), required=False, default=list)
    final = serializers.BooleanField(required=False, default=True)


class ValidateIn(_Strict):
    settings = serializers.DictField(required=False)
    document = serializers.DictField(required=False)


class RecalculateIn(_Strict):
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


def _fail_invalid(exc: Exception) -> None:
    raise StructuredValidationError({"__all__": [str(exc)]}, extra={"code": "invalid"}) from exc


def issue_list(issues: list[cfg.Issue]) -> list[dict[str, str]]:
    return [i.as_dict() for i in issues]


def config_out(version, row, *, user) -> dict[str, Any]:  # noqa: ANN001
    base = {"version": str(version.pk), "version_number": version.version_number, "status": version.status,
            "editable": policies.can_configure(user, version), "configured": row is not None}
    if row is None:
        return {**base, "settings": cfg.default_settings(), "document": cfg.empty_document(), "notes": "", "content_hash": None, "issues": []}
    return {**base, "settings": row.settings, "document": row.document, "notes": row.notes, "content_hash": row.content_hash,
            "issues": issue_list(cfg.validate(row.settings, row.document, _snapshot(version)))}


def _snapshot(version):  # noqa: ANN001, ANN202
    from apps.checklists.rules import snapshot_for_version

    return snapshot_for_version(version, include_rules=False)


def _num(value: Any) -> str | None:
    return cfg.dstr(value) if value is not None else None


def result_out(r: ScoreResult, *, breakdown: bool) -> dict[str, Any]:
    out = {"id": str(r.pk), "applicable": True, "stored": True, "status": r.status, "kind": r.kind, "is_current": r.is_current, "method": r.method,
           "earned": _num(r.earned), "available": _num(r.available), "percentage": _num(r.percentage), "grade": r.grade, "label": r.label,
           "display_label": r.display_label, "critical": r.critical, "algorithm": r.algorithm, "config_hash": r.config_hash,
           "version": str(r.version_id), "execution_revision": r.execution_revision, "calculated_at": r.created_at, "reason": r.reason,
           "calculated_by": str(r.created_by_id) if r.created_by_id else None}
    if breakdown:
        out.update(r.breakdown)
    return out


def outcome_out(outcome, *, breakdown: bool) -> dict[str, Any]:  # noqa: ANN001
    full = outcome.as_dict()
    summary = {key: full[key] for key in ("status", "method", "earned", "available", "percentage", "grade", "label", "display_label", "critical", "algorithm")}
    out = {"applicable": True, "stored": False, **summary}
    if breakdown:
        out.update({key: full[key] for key in ("items", "sections", "warnings", "explanation", "critical_hits")})
    return out


class MetadataView(APIView):
    permission_classes = [IsActiveUser]

    @extend_schema(responses=dict, summary="The finite vocabularies of a scoring configuration")
    def get(self, request: Request) -> Response:
        return Response({
            "methods": [{"value": v, "label": str(label)} for v, label in k.Method.choices],
            "na_policies": [{"value": v, "label": str(label)} for v, label in k.NaPolicy.choices],
            "missing_policies": [{"value": v, "label": str(label)} for v, label in k.MissingPolicy.choices],
            "critical_ops": [{"value": v, "label": str(label)} for v, label in k.CriticalOp.choices],
            "result_statuses": [{"value": v, "label": str(label)} for v, label in k.ResultStatus.choices],
            "scorable_types": list(k.SCORABLE_TYPES), "fixed_point_keys": {key: list(val) for key, val in k.FIXED_POINT_KEYS.items()},
            "precision": {"min": 0, "max": 4}, "limits": {"max_points": k.MAX_POINTS, "max_weight": k.MAX_WEIGHT, "max_items": k.MAX_ITEMS,
                                                          "max_bands": k.MAX_BANDS, "max_critical": k.MAX_CRITICAL, "max_ranges": k.MAX_RANGES},
            "algorithm": k.ALGORITHM_VERSION, "defaults": cfg.default_settings(),
        })


class VersionScoringView(APIView):
    """``GET`` the configuration, ``PUT`` the draft's configuration (``settings`` / ``document`` replace the given parts)."""

    permission_classes = [IsActiveUser]

    @extend_schema(responses=dict, summary="A checklist version's scoring configuration")
    def get(self, request: Request, pk: str) -> Response:
        version = _version(request, pk)
        if not policies.can_view_config(request.user, version):
            raise PermissionDenied
        return Response(config_out(version, selectors.get_config(version), user=request.user))

    @extend_schema(request=ConfigIn, responses=dict, summary="Save the scoring configuration of a draft version")
    def put(self, request: Request, pk: str) -> Response:
        version = _version(request, pk)
        data = ConfigIn(data=request.data)
        data.is_valid(raise_exception=True)
        row = run(lambda: services.save_config(version, actor=request.user, settings=data.validated_data.get("settings"),
                                               document=data.validated_data.get("document"), notes=data.validated_data.get("notes")))
        return Response(config_out(version, row, user=request.user))

    @extend_schema(responses=None, summary="Remove the scoring of a draft version")
    def delete(self, request: Request, pk: str) -> Response:
        version = _version(request, pk)
        run(lambda: services.remove_config(version, actor=request.user))
        return Response(status=204)


class VersionScoringValidateView(APIView):
    permission_classes = [IsActiveUser]

    @extend_schema(request=ValidateIn, responses=dict, summary="Validate the saved (or a candidate) configuration against the version's questions")
    def post(self, request: Request, pk: str) -> Response:
        version = _version(request, pk)
        if not policies.can_configure_at(request.user, version):
            raise PermissionDenied
        data = ValidateIn(data=request.data)
        data.is_valid(raise_exception=True)
        issues = run(lambda: services.validate_config(version, settings=data.validated_data.get("settings"), document=data.validated_data.get("document")))
        return Response({"ok": not any(i.level == "error" for i in issues), "issues": issue_list(issues)})


class VersionScoringPreviewView(APIView):
    """Score synthetic sample answers with the authoritative calculation.  Nothing is stored; no execution is created."""

    permission_classes = [IsActiveUser]

    @extend_schema(request=PreviewIn, responses=dict, summary="What-if score of sample answers")
    def post(self, request: Request, pk: str) -> Response:
        version = _version(request, pk)
        if not policies.can_configure_at(request.user, version):
            raise PermissionDenied
        data = PreviewIn(data=request.data)
        data.is_valid(raise_exception=True)
        d = data.validated_data
        result = run(lambda: services.preview_sample(version, settings=d.get("settings"), document=d.get("document"), answers=d["answers"],
                                                     not_applicable=d["not_applicable"], final=d["final"]))
        outcome = result["outcome"]
        return Response({"valid": result["valid"], "issues": result["issues"], "disabled": result.get("disabled", False),
                         "result": outcome_out(outcome, breakdown=True) if outcome is not None else None})


class ExecutionScoreView(APIView):
    """The score summary of an execution: the stored final result, or a provisional live computation while it is in progress."""

    permission_classes = [IsActiveUser]
    breakdown = False

    @extend_schema(responses=dict, summary="An execution's score")
    def get(self, request: Request, pk: str) -> Response:
        execution = _execution(request, pk)
        allowed = policies.can_view_breakdown if self.breakdown else policies.can_view_score
        if not allowed(request.user, execution):
            raise PermissionDenied
        view = services.score_of(execution)
        if not view["applicable"]:
            return Response({"applicable": False, "reason": view["reason"]})
        if view.get("pending"):
            return Response({"applicable": True, "stored": False, "pending": True, "status": "pending"})
        if view["stored"]:
            return Response(result_out(view["result"], breakdown=self.breakdown))
        if not policies.can_preview(request.user, execution):
            raise PermissionDenied
        return Response(outcome_out(view["outcome"], breakdown=self.breakdown))


class ExecutionScoreBreakdownView(ExecutionScoreView):
    breakdown = True


class ExecutionScoreHistoryView(APIView):
    """Every stored result of the execution (the original and any authorized recalculations)."""

    permission_classes = [IsActiveUser]

    @extend_schema(responses=dict, summary="An execution's score history")
    def get(self, request: Request, pk: str) -> Response:
        execution = _execution(request, pk)
        if not policies.can_view_breakdown(request.user, execution):
            raise PermissionDenied
        return Response({"results": [result_out(r, breakdown=False) for r in selectors.results_for(execution)]})


class ExecutionScoreRecalculateView(APIView):
    """``POST`` an authorized recalculation: a provisional refresh while in progress, or a reasoned recalculation of a final score."""

    permission_classes = [IsActiveUser]

    @extend_schema(request=RecalculateIn, responses=dict, summary="Recalculate an execution's score")
    def post(self, request: Request, pk: str) -> Response:
        execution = _execution(request, pk)
        data = RecalculateIn(data=request.data)
        data.is_valid(raise_exception=True)
        if policies.can_preview(request.user, execution):
            view = services.score_of(execution)
            if not view["applicable"]:
                return Response({"applicable": False, "reason": view["reason"]})
            return Response(outcome_out(view["outcome"], breakdown=True))
        reason = data.validated_data.get("reason", "")
        result, changed = run(lambda: services.recalculate(execution, actor=request.user, reason=reason))
        return Response({**result_out(result, breakdown=True), "changed": changed})
