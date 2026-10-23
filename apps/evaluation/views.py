"""Class-based views of the evaluation screens (Phase 16): the draft rule-set editor, the controlled rule form (condition builder), the what-if preview and the
execution evaluation page with its authorized re-evaluation.

Same pattern as the checklist builder and scoring: the version / execution is resolved through the authorized selector (not visible -> 404), edits need the permission
and a draft (403 / a flashed refusal), every write is a service call, and nothing here evaluates a rule - the preview and the result pages render what
``apps.evaluation.services`` returns.  The rule form is server-driven: each HTMX change re-posts the whole form and the server re-renders it, so what the author sees is
always what the single validator will accept.
"""

from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.views import View
from django.views.generic import TemplateView

from apps.checklists import selectors as checklist_selectors
from apps.checklists.rules import snapshot_for_version
from apps.core.utils import parse_uuid
from apps.executions import selectors as execution_selectors

from . import config as cfg
from . import constants as k
from . import policies, presentation, selectors, services
from .services.configuration import OrgLookups


class VersionMixin(LoginRequiredMixin):
    """Resolves the checklist version from the URL (404 when invisible) and requires the right to read its evaluation rules."""

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if request.user.is_authenticated:
            if not policies.can_view_config(request.user, self.version):
                raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    @property
    def version(self):  # noqa: ANN201
        if not hasattr(self, "_version"):
            parsed = parse_uuid(self.kwargs["version"])
            version = checklist_selectors.get_version_for_user(self.request.user, parsed) if parsed else None
            if version is None:
                raise Http404
            self._version = version
        return self._version

    @property
    def row(self):  # noqa: ANN201
        if not hasattr(self, "_row"):
            self._row = selectors.get_ruleset(self.version)
        return self._row

    def editor_url(self) -> str:
        return reverse("evaluation:editor", args=[self.version.pk])


class EditorView(VersionMixin, TemplateView):
    template_name = "evaluation/editor.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        version, row = self.version, self.row
        snapshot = snapshot_for_version(version, include_rules=False)
        document = row.document if row else cfg.empty_document()
        issues, _compiled, _doc = services.validate_config(version) if row else ([], [], None)
        rules = presentation.catalogue(snapshot, document, issues)
        context.update(
            version=version, template=version.template, row=row, enabled=row.enabled if row else True, document=document, issues=issues,
            errors=[i for i in issues if i.level == "error"], warnings=[i for i in issues if i.level == "warning"], rules=rules,
            general_issues=[i for i in issues if not i.subject.startswith("rule:")], can_edit=policies.can_configure(self.request.user, version),
            can_try=policies.can_configure_at(self.request.user, version), sample_fields=presentation.sample_fields(snapshot, document),
            score_statuses=("final", "provisional", "not_applicable", "unavailable"), content_hash=row.content_hash if row else "",
            subtitle=f"{version.template.code} · version {version.version_number}", limits={"rules": k.MAX_RULES},
        )
        return context


class EditPostView(VersionMixin, View):
    """Base of the editor's small POST actions: authorize, call a service, flash the outcome, go back to the editor."""

    http_method_names = ["post"]
    success_message = "Saved."

    def perform(self, request: HttpRequest) -> Any:  # pragma: no cover - interface
        raise NotImplementedError

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if not policies.can_configure_at(request.user, self.version):
            raise PermissionDenied
        try:
            self.perform(request)
        except services.EvaluationError as exc:
            messages.error(request, exc.message)
        else:
            messages.success(request, self.success_message)
        return HttpResponseRedirect(self.editor_url())


class SettingsView(EditPostView):
    success_message = "Evaluation settings saved."

    def perform(self, request: HttpRequest) -> Any:
        return services.save_config(self.version, actor=request.user, enabled=request.POST.get("enabled") == "on", notes=request.POST.get("notes", ""))


class RuleToggleView(EditPostView):
    success_message = "Rule updated."

    def perform(self, request: HttpRequest) -> Any:
        return services.toggle_rule(self.version, actor=request.user, code=self.kwargs["code"], enabled=request.POST.get("action") == "enable")


class RuleMoveView(EditPostView):
    success_message = "Rule moved."

    def perform(self, request: HttpRequest) -> Any:
        return services.move_rule(self.version, actor=request.user, code=self.kwargs["code"], direction="up" if request.POST.get("direction") == "up" else "down")


class RuleDeleteView(EditPostView):
    success_message = "Rule deleted."

    def perform(self, request: HttpRequest) -> Any:
        return services.set_rule(self.version, actor=request.user, code=self.kwargs["code"], rule=None)


def _reference_data(organization: Any) -> dict[str, Any]:
    from django.db.models import Q

    from apps.findings.models import FindingCategory, FindingSeverity
    from apps.workflows.constants import DefinitionStatus, VersionStatus
    from apps.workflows.models import WorkflowDefinition

    return {
        "categories": list(FindingCategory.objects.filter(organization=organization, is_active=True).order_by("sort_order").values_list("code", "name")),
        "severities": list(FindingSeverity.objects.filter(organization=organization, is_active=True).order_by("rank").values_list("code", "name")),
        "workflows": list(WorkflowDefinition.objects.filter(Q(organization=organization) | Q(organization__isnull=True), target_type="execution", status=DefinitionStatus.ACTIVE,
                                                            versions__status=VersionStatus.PUBLISHED).distinct().values_list("code", "name")),
    }


class RuleFormView(VersionMixin, TemplateView):
    """Create or edit one rule: metadata, the condition builder (rows), outcome and approved actions."""

    template_name = "evaluation/rule_form.html"

    def existing(self) -> dict[str, Any] | None:
        code = self.kwargs.get("code")
        if not code:
            return None
        rules = (self.row.document.get("rules", []) if self.row else [])
        found = next((r for r in rules if isinstance(r, dict) and r.get("code") == code), None)
        if found is None:
            raise Http404
        return found

    def build_context(self, post: Any | None) -> dict[str, Any]:
        snapshot = snapshot_for_version(self.version, include_rules=False)
        rule = self.existing()
        rules = (self.row.document.get("rules", []) if self.row else [])
        next_priority = max([r.get("priority", 0) for r in rules if isinstance(r, dict) and isinstance(r.get("priority"), int)] or [0]) + 10
        if post is None:
            rows, groups, fits = presentation.rows_from_condition((rule or {}).get("condition"), snapshot)
            values = {"code": (rule or {}).get("code", ""), "name": (rule or {}).get("name", ""), "description": (rule or {}).get("description", ""),
                      "priority": (rule or {}).get("priority", next_priority), "outcome": (rule or {}).get("outcome", "review_required"), "message": (rule or {}).get("message", ""),
                      "on_unknown": (rule or {}).get("on_unknown", "skip"), "stop": bool((rule or {}).get("stop")), "enabled": (rule or {}).get("enabled", True)}
            actions = presentation.action_rows((rule or {}).get("actions", []) if isinstance((rule or {}).get("actions", []), list) else [])
            rows = presentation.pad_rows(rows, snapshot)
            keep = not fits
        else:
            rows = presentation.rows_from_post(post, snapshot)
            groups = {"root_logic": "OR" if post.get("root_logic") == "OR" else "AND", "gl": {g: ("OR" if post.get(f"gl_{g}") == "OR" else "AND") for g in range(1, 4)},
                      "gn": {g: post.get(f"gn_{g}") == "on" for g in range(1, 4)}}
            values = {"code": post.get("code", "").strip() or (rule or {}).get("code", ""), "name": post.get("name", ""), "description": post.get("description", ""),
                      "priority": post.get("priority", ""), "outcome": post.get("outcome", "review_required"), "message": post.get("message", ""),
                      "on_unknown": post.get("on_unknown", "skip"), "stop": post.get("stop") == "on", "enabled": post.get("enabled") == "on"}
            actions = presentation.action_rows(presentation.actions_from_post(post))
            keep = post.get("keep_condition") == "1"
        if post is not None and post.get("do") == "add_row":
            rows = rows + [{"i": len(rows), "blank": True, "src": "", "fld": "", "op": "", "group": "0", "neg": False}]
        context = {"version": self.version, "template": self.version.template, "rule": rule, "is_new": rule is None, "values": values, "rows": rows, "groups": groups, "actions": actions,
                   "keep_condition": keep, "keep_text": presentation.catalogue(snapshot, {"rules": [rule]}, [])[0]["condition"] if (rule and keep) else "",
                   "sources": presentation.source_choices(), "outcomes": [(v, k.Outcome(v).label) for v in k.RULE_OUTCOMES], "on_unknown_choices": k.OnUnknown.choices,
                   "action_types": k.ActionType.choices, "reference": _reference_data(self.version.template.organization), "can_edit": policies.can_configure(self.request.user, self.version),
                   "row_count": len(rows), "form_url": self.request.path, "editor_url": self.editor_url(), "limits": {"conditions": k.MAX_CONDITIONS}}
        return context

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context.update(self.build_context(None))
        return context

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if not policies.can_configure_at(request.user, self.version):
            raise PermissionDenied
        post = request.POST
        if post.get("do") != "save":  # an HTMX change (source / field / operator / add a row): re-render the form
            context = self.build_context(post)
            template = "evaluation/_rule_form.html" if request.headers.get("HX-Request") == "true" else self.template_name
            return render(request, template, {**context, "view": self})
        snapshot = snapshot_for_version(self.version, include_rules=False)
        existing = self.existing()
        rows = presentation.rows_from_post(post, snapshot)
        if post.get("keep_condition") == "1" and existing is not None:
            condition = existing.get("condition")
        else:
            condition = presentation.condition_from_form(post, rows)
        raw_priority = post.get("priority", "").strip()
        try:
            priority: Any = int(raw_priority)
        except ValueError:
            priority = raw_priority or None
        code = (existing or {}).get("code") or post.get("code", "").strip()
        rule: dict[str, Any] = {"code": code, "name": post.get("name", "").strip(), "description": post.get("description", "").strip(), "enabled": post.get("enabled") == "on",
                                "priority": priority, "condition": condition, "outcome": post.get("outcome", ""), "message": post.get("message", "").strip(),
                                "on_unknown": post.get("on_unknown", "skip"), "stop": post.get("stop") == "on", "actions": presentation.actions_from_post(post)}
        try:
            services.set_rule(self.version, actor=request.user, code=code, rule=rule, original_code=(existing or {}).get("code"))
        except services.EvaluationError as exc:
            messages.error(request, exc.message)
            context = self.build_context(post)
            return render(request, self.template_name, {**context, "view": self})
        issues: list[cfg.Issue] = []
        cfg.compile_rule(snapshot, rule, 0, OrgLookups(self.version.template.organization), issues)
        problems = [i for i in issues if i.level == "error"]
        if problems:
            messages.warning(request, "The rule was saved as a draft but cannot be published yet: " + " ".join(i.message for i in problems[:3]))
            return HttpResponseRedirect(reverse("evaluation:rule", args=[self.version.pk, code]))
        messages.success(request, f"Rule {code} saved.")
        return HttpResponseRedirect(self.editor_url())


class PreviewView(VersionMixin, View):
    """What-if: evaluate the sample inputs in the form under the saved rules (HTMX fragment).  Non-production: nothing is stored, no action runs."""

    http_method_names = ["post"]

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if not policies.can_configure_at(request.user, self.version):
            raise PermissionDenied
        snapshot = snapshot_for_version(self.version, include_rules=False)
        answers: dict[str, Any] = {}
        not_applicable: list[str] = []
        for code, item in snapshot.items.items():
            raw = request.POST.getlist(f"a_{code}") if item.answer_type == "multiple_choice" else request.POST.get(f"a_{code}", "").strip()
            if request.POST.get(f"na_{code}") == "on":
                not_applicable.append(code)
            elif item.answer_type == "boolean" and raw:
                answers[code] = raw == "true"
            elif raw not in ("", []):
                answers[code] = raw
        score = {"status": request.POST.get("score_status", "final"), "percentage": request.POST.get("score_percentage", "").strip(), "earned": request.POST.get("score_earned", "").strip(),
                 "available": request.POST.get("score_available", "").strip(), "grade": request.POST.get("score_grade", "").strip(), "critical": request.POST.get("score_critical") == "on"}
        try:
            result = services.preview_sample(self.version, enabled=None, document=None, answers=answers, not_applicable=not_applicable, score=score,
                                             execution_status=request.POST.get("execution_status", "submitted"))
            error = ""
        except services.EvaluationError as exc:
            result, error = {"valid": False, "issues": [], "evaluation": None}, exc.message
        except Exception as exc:  # noqa: BLE001 - e.g. an answer the question's own validator refuses; shown, never a 500
            result, error = {"valid": False, "issues": [], "evaluation": None}, f"The sample inputs could not be evaluated: {exc}"
        evaluation = result.get("evaluation")
        return render(request, "evaluation/_preview_result.html", {"result": result, "error": error, "evaluation": evaluation.as_dict() if evaluation else None, "version": self.version})


class ExecutionEvaluationView(LoginRequiredMixin, TemplateView):
    """The evaluation of one execution (needs ``evaluation.result_view``; conditions, explanations and action outcomes need ``evaluation.explain``)."""

    template_name = "evaluation/execution_evaluation.html"

    @property
    def execution(self):  # noqa: ANN201
        if not hasattr(self, "_execution"):
            execution = execution_selectors.get_execution(self.request.user, self.kwargs["pk"])
            if execution is None:
                raise Http404
            self._execution = execution
        return self._execution

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if request.user.is_authenticated and not policies.can_view_result(request.user, self.execution):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        from apps.api.v1.evaluation import action_out

        context = super().get_context_data(**kwargs)
        ex, user = self.execution, self.request.user
        view = services.evaluation_of(ex)
        run = view.get("run")
        evaluation = view.get("evaluation")
        live = evaluation is not None and policies.can_preview(user, ex)
        explain = policies.can_explain(user, ex)
        detail = (run.detail if run is not None else (evaluation.as_dict() if live else None)) if explain else None
        context.update(
            execution=ex, view=view, run=run, evaluation=evaluation.as_dict() if live else None, hidden_live=evaluation is not None and not live, detail=detail, can_explain=explain,
            actions=[action_out(user, a) for a in selectors.actions_for(run)] if (run is not None and explain) else [],
            history=list(selectors.runs_for(ex)), can_reevaluate=policies.can_reevaluate(user, ex) and run is not None,
            matched=[r for r in (detail or {}).get("rules", []) if r["status"] == "matched"],
            other=[r for r in (detail or {}).get("rules", []) if r["status"] != "matched"],
        )
        return context


class ReevaluateView(LoginRequiredMixin, View):
    http_method_names = ["post"]

    def post(self, request: HttpRequest, pk: str) -> HttpResponse:
        execution = execution_selectors.get_execution(request.user, pk)
        if execution is None:
            raise Http404
        try:
            _run, changed = services.reevaluate(execution, actor=request.user, reason=request.POST.get("reason", ""))
        except PermissionDenied:
            raise
        except services.EvaluationError as exc:
            messages.error(request, exc.message)
        else:
            messages.success(request, "The execution was evaluated again and the previous result is kept in the history." if changed
                             else "The re-evaluation gave the same result; nothing changed and no action was repeated.")
        return HttpResponseRedirect(reverse("evaluation:execution", args=[execution.pk]))
