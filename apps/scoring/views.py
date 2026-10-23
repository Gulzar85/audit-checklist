"""Class-based views of the scoring screens (Phase 15): the draft scoring editor and its small POST actions, the what-if preview, and the
execution score page with its authorized recalculation.

Same pattern as the checklist builder: the version / execution is resolved through the authorized selector (not visible -> 404), edits need
the permission and a draft (403 / a flashed refusal), every write is a service call, and nothing here calculates a score - the preview and
the score page render what ``apps.scoring.services`` returns.
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


class VersionMixin(LoginRequiredMixin):
    """Resolves the checklist version from the URL (404 when invisible) and requires the right to read its scoring."""

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
            self._row = selectors.get_config(self.version)
        return self._row

    def editor_url(self) -> str:
        return reverse("scoring:editor", args=[self.version.pk])


class EditorView(VersionMixin, TemplateView):
    template_name = "scoring/editor.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        version, row = self.version, self.row
        snapshot = snapshot_for_version(version, include_rules=False)
        settings = row.settings if row else cfg.default_settings()
        document = row.document if row else cfg.empty_document()
        issues = cfg.validate(settings, document, snapshot) if row else []
        scored = [i for i in presentation.catalogue(snapshot, document, issues)]
        context.update(
            version=version, template=version.template, row=row, subtitle=f"{version.template.code} · version {version.version_number}", settings=settings, document=document, issues=issues,
            errors=[i for i in issues if i.level == "error"], warnings=[i for i in issues if i.level == "warning"],
            can_edit=policies.can_configure(self.request.user, version), can_try=policies.can_configure_at(self.request.user, version), items=scored, scored_count=sum(1 for i in scored if i.get("scored")),
            sections=[{"code": code, "name": s.name, "weight": (document.get("sections") or {}).get(code, "")} for code, s in snapshot.sections.items()],
            bands=presentation.blank_rows(list(document.get("bands") or []), 6), critical=presentation.blank_rows(list(document.get("critical") or []), 4),
            critical_items=[i for i in scored if i["scored"]], sample_fields=presentation.sample_fields(snapshot, document),
            methods=k.Method.choices, na_policies=k.NaPolicy.choices, missing_policies=k.MissingPolicy.choices, critical_ops=k.CriticalOp.choices,
            content_hash=row.content_hash if row else "",
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
        except services.ScoringError as exc:
            messages.error(request, exc.message)
        else:
            messages.success(request, self.success_message)
        return HttpResponseRedirect(self.redirect_url())

    def redirect_url(self) -> str:
        return self.editor_url()


def _rows(post: Any, *names: str) -> list[tuple[str, ...]]:
    lists = [post.getlist(n) for n in names]
    width = max((len(x) for x in lists), default=0)
    out = []
    for i in range(width):
        row = tuple((x[i] if i < len(x) else "").strip() for x in lists)
        if any(row):
            out.append(row)
    return out


class SettingsView(EditPostView):
    success_message = "Scoring settings saved."

    def perform(self, request: HttpRequest) -> Any:
        p = request.POST
        try:
            precision = int(p.get("precision", "2"))
        except ValueError as exc:
            raise services.ScoringError("The rounding precision must be a whole number from 0 to 4.", "invalid_setting") from exc
        return services.save_config(self.version, actor=request.user, settings={
            "enabled": p.get("enabled") == "on", "method": p.get("method", ""), "na_policy": p.get("na_policy", ""), "missing_policy": p.get("missing_policy", ""),
            "precision": precision, "critical_overrides_grade": p.get("critical_overrides_grade") == "on", "critical_label": p.get("critical_label", ""),
        }, notes=p.get("notes", ""))


class WeightsView(EditPostView):
    success_message = "Section weights saved."

    def perform(self, request: HttpRequest) -> Any:
        weights = {key[2:]: value.strip() for key, value in request.POST.items() if key.startswith("w_") and value.strip()}
        return services.set_part(self.version, actor=request.user, part="sections", value=weights)


class BandsView(EditPostView):
    success_message = "Grade bands saved."

    def perform(self, request: HttpRequest) -> Any:
        bands = [{"min": lo, "grade": grade, "label": label} for lo, grade, label in _rows(request.POST, "band_min", "band_grade", "band_label")]
        return services.set_part(self.version, actor=request.user, part="bands", value=bands)


class CriticalView(EditPostView):
    success_message = "Critical rules saved."

    def perform(self, request: HttpRequest) -> Any:
        rules = [{"item": item, "op": op, "value": value, "label": label}
                 for item, op, value, label in _rows(request.POST, "crit_item", "crit_op", "crit_value", "crit_label")]
        return services.set_part(self.version, actor=request.user, part="critical", value=rules)


class ItemRuleView(VersionMixin, TemplateView):
    """The scoring rule of one question: a form shaped by the question's answer type."""

    template_name = "scoring/item_form.html"

    def spec(self):  # noqa: ANN201
        snapshot = snapshot_for_version(self.version, include_rules=False)
        spec = snapshot.items.get(self.kwargs["code"])
        if spec is None:
            raise Http404
        return spec

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        spec = self.spec()
        row = self.row
        rule = ((row.document.get("items") or {}).get(spec.code) if row else None) or None
        points = (rule or {}).get("points", {})
        if spec.answer_type in k.FIXED_POINT_KEYS:
            fields = [{"key": key, "label": presentation._label(spec, key), "value": points.get(key, "")} for key in k.FIXED_POINT_KEYS[spec.answer_type]]
        elif spec.answer_type == "boolean":
            fields = [{"key": key, "label": presentation._label(spec, key), "value": points.get(key, "")} for key in ("true", "false")]
        elif spec.answer_type in k.CHOICE_TYPES:
            fields = [{"key": o.value, "label": o.label + ("" if o.is_active else " (inactive)"), "value": points.get(o.value, "")} for o in spec.options]
        else:
            fields = []
        context.update(version=self.version, template=self.version.template, spec=spec, rule=rule, fields=fields, scorable=spec.answer_type in k.SCORABLE_TYPES,
                       ranges=presentation.blank_range_rows(rule), can_edit=policies.can_configure(self.request.user, self.version),
                       aggregate=(rule or {}).get("aggregate", "sum"), weight=(rule or {}).get("weight", "1"))
        return context

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if not policies.can_configure_at(request.user, self.version):
            raise PermissionDenied
        spec, p = self.spec(), request.POST
        if p.get("remove") == "1":
            try:
                services.set_item_rule(self.version, actor=request.user, code=spec.code, rule=None)
            except services.ScoringError as exc:
                messages.error(request, exc.message)
                return HttpResponseRedirect(request.path)
            messages.success(request, f"Question {spec.code} is no longer scored.")
            return HttpResponseRedirect(self.editor_url())
        rule: dict[str, Any] = {"weight": p.get("weight", "").strip() or "1"}
        if spec.answer_type in k.FIXED_POINT_KEYS or spec.answer_type == "boolean":
            keys = k.FIXED_POINT_KEYS.get(spec.answer_type, ("true", "false"))
            rule["points"] = {key: p.get(f"p_{key}", "").strip() for key in keys}
        elif spec.answer_type in k.CHOICE_TYPES:
            rule["points"] = {o.value: p.get(f"p_{o.value}", "").strip() for o in spec.options}
            if spec.answer_type == "multiple_choice":
                rule["aggregate"] = p.get("aggregate", "sum")
        elif spec.answer_type == "numeric":
            ranges = []
            for n in range(presentation.RANGE_ROWS):
                lo, hi, points = (p.get(f"r{n}_min", "").strip(), p.get(f"r{n}_max", "").strip(), p.get(f"r{n}_points", "").strip())
                if lo or hi or points:
                    ranges.append({"min": lo or None, "max": hi or None, "min_inclusive": p.get(f"r{n}_mininc") == "on",
                                   "max_inclusive": p.get(f"r{n}_maxinc") == "on", "points": points})
            rule["ranges"] = ranges
        elif spec.answer_type != "score":
            messages.error(request, f"A “{spec.answer_type}” question cannot be scored.")
            return HttpResponseRedirect(request.path)
        try:
            services.set_item_rule(self.version, actor=request.user, code=spec.code, rule=rule)
        except services.ScoringError as exc:
            messages.error(request, exc.message)
            return HttpResponseRedirect(request.path)
        messages.success(request, f"Scoring of question {spec.code} saved.")
        return HttpResponseRedirect(self.editor_url())


class PreviewView(VersionMixin, View):
    """What-if: score the sample answers in the form with the saved configuration (HTMX fragment).  Nothing is stored."""

    http_method_names = ["post"]

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if not policies.can_configure_at(request.user, self.version):
            raise PermissionDenied
        snapshot = snapshot_for_version(self.version, include_rules=False)
        answers: dict[str, Any] = {}
        not_applicable: list[str] = []
        for code, spec in snapshot.items.items():
            raw = request.POST.getlist(f"a_{code}") if spec.answer_type == "multiple_choice" else request.POST.get(f"a_{code}", "").strip()
            if request.POST.get(f"na_{code}") == "on":
                not_applicable.append(code)
            elif spec.answer_type == "boolean" and raw:
                answers[code] = raw == "true"
            elif raw not in ("", []):
                answers[code] = raw
        try:
            result = services.preview_sample(self.version, settings=None, document=None, answers=answers, not_applicable=not_applicable,
                                             final=request.POST.get("final") == "on")
            error = ""
        except services.ScoringError as exc:
            result, error = {"valid": False, "issues": [], "outcome": None}, exc.message
        except Exception as exc:  # noqa: BLE001 - e.g. an answer the question's own validator refuses; shown, never a 500
            result, error = {"valid": False, "issues": [], "outcome": None}, f"The sample answers could not be evaluated: {exc}"
        return render(request, "scoring/_preview_result.html", {"result": result, "error": error, "outcome": result.get("outcome"), "version": self.version})


class ExecutionScoreView(LoginRequiredMixin, TemplateView):
    """The score of one execution with its explained breakdown (needs ``scoring.score_view``; the breakdown needs ``scoring.breakdown_view``)."""

    template_name = "scoring/execution_score.html"

    @property
    def execution(self):  # noqa: ANN201
        if not hasattr(self, "_execution"):
            execution = execution_selectors.get_execution(self.request.user, self.kwargs["pk"])
            if execution is None:
                raise Http404
            self._execution = execution
        return self._execution

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if request.user.is_authenticated and not policies.can_view_score(request.user, self.execution):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        ex, user = self.execution, self.request.user
        view = services.score_of(ex)
        result = view.get("result")
        outcome = view.get("outcome")
        live_allowed = outcome is not None and policies.can_preview(user, ex)
        context.update(
            execution=ex, view=view, result=result, outcome=outcome if live_allowed else None, hidden_live=outcome is not None and not live_allowed,
            breakdown=(result.breakdown if result is not None else (outcome.breakdown() if live_allowed else None)) if policies.can_view_breakdown(user, ex) else None,
            history=list(selectors.results_for(ex)) if policies.can_view_breakdown(user, ex) else [],
            can_recalculate=policies.can_finalize_recalculate(user, ex) and result is not None,
        )
        return context


class RecalculateView(LoginRequiredMixin, View):
    http_method_names = ["post"]

    def post(self, request: HttpRequest, pk: str) -> HttpResponse:
        execution = execution_selectors.get_execution(request.user, pk)
        if execution is None:
            raise Http404
        try:
            _result, changed = services.recalculate(execution, actor=request.user, reason=request.POST.get("reason", ""))
        except PermissionDenied:
            raise
        except services.ScoringError as exc:
            messages.error(request, exc.message)
        else:
            messages.success(request, "The score was recalculated and the previous result is kept in the history." if changed
                             else "The recalculation gave the same result; nothing changed.")
        return HttpResponseRedirect(reverse("scoring:execution", args=[execution.pk]))
