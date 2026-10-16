"""Class-based views for the conditional-rules builder: the rule list, the rule editor and the simulator.

Same pattern as the rest of the builder: the template is resolved through the authorized selector (not visible -> 404),
edit actions need the draft version and edit rights (403), every write is a service call, and the editor is
server-driven - each HTMX request posts the whole editor, the server applies one operation and re-renders the fragment,
so what the author sees is always what the (single) rule validator will accept.  The simulator evaluates the *saved* rules
for sample answers with the same evaluator as the API and persists nothing.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.urls import reverse
from django.views.generic import TemplateView

from . import constants as c
from . import policies, selectors, services
from .forms import RuleForm
from .rules import builder, compile_rules, describe_rule, evaluate, limit, snapshot_for_version, validate_rule_set
from .rules.schema import compile_rule
from .rules.snapshot import RuleSpec
from .services import RuleValidationError
from .views import BuilderPostView, TemplateObjectMixin


def _hx(request: HttpRequest) -> bool:
    return request.headers.get("HX-Request") == "true"


def _summary(snapshot: Any, rule: Any) -> str:
    try:
        return describe_rule(snapshot, rule.condition, rule.actions)
    except (KeyError, TypeError, AttributeError):  # a stored rule that no longer reads (never produced by the services)
        return "This rule could not be summarized. Open it to review it."


class RulesVersionMixin(TemplateObjectMixin):
    """The version the rule screens show: ``?version=N``, else the draft, else the latest published version."""

    @property
    def version(self):  # noqa: ANN201
        if not hasattr(self, "_rules_version"):
            number = self.request.GET.get("version", "")
            if number:
                version = selectors.get_version(self.template, int(number)) if number.isascii() and number.isdigit() else None
            else:
                version = self.draft or self.template.versions.filter(status=c.VersionStatus.PUBLISHED).order_by("-version_number").first()
            if version is None:
                raise Http404
            self._rules_version = version
        return self._rules_version

    def shared_context(self) -> dict[str, Any]:
        versions = list(selectors.versions_of(self.template))
        return {
            "template": self.template, "version": self.version, "versions": versions, "version_label": f"Version {self.version.version_number}",
            "can_edit": policies.can_edit_version(self.request.user, self.version),
            "rules_url": reverse("checklists:rules", args=[self.template.pk]),
        }


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------
class RuleListView(RulesVersionMixin, TemplateView):
    template_name = "checklists/rules/list.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        version = self.version
        snapshot = snapshot_for_version(version)
        report = validate_rule_set(snapshot)
        by_rule: dict[str, list[Any]] = defaultdict(list)
        for issue in report.issues:
            by_rule[issue.rule].append(issue)
        rules = list(selectors.rules_for_version(version))
        rows = [{"rule": r, "summary": _summary(snapshot, r), "issues": by_rule.get(r.code, [])} for r in rules]
        context.update(
            self.shared_context(), rows=rows, report=report,
            general_issues=by_rule.get("", []),
            enabled_count=sum(1 for r in rules if r.is_active),
            item_count=len(snapshot.items), limits={"rules": limit("max_rules_per_version")},
        )
        return context


class _RulePostView(BuilderPostView):
    """A one-click rule action on the draft; always returns to the rule list."""

    def redirect_url(self) -> str:
        return reverse("checklists:rules", args=[self.template.pk])

    def rule(self, draft: Any):  # noqa: ANN201
        rule = selectors.get_rule(draft, self.kwargs["rule_pk"])
        if rule is None:  # another version, checklist or organization: never revealed
            raise Http404
        return rule


class RuleToggleView(_RulePostView):
    def perform(self, draft):  # noqa: ANN001, ANN201
        enable = self.request.POST.get("action") == "enable"
        services.set_rule_enabled(self.rule(draft), enable, actor=self.request.user)
        self.success_message = "Rule enabled." if enable else "Rule disabled. It is kept but no longer applies."


class RuleDeleteView(_RulePostView):
    success_message = "Rule deleted."

    def perform(self, draft):  # noqa: ANN001, ANN201
        services.delete_rule(self.rule(draft), actor=self.request.user)


class RuleMoveView(_RulePostView):
    success_message = "Rule moved."

    def perform(self, draft):  # noqa: ANN001, ANN201
        services.move_rule(self.rule(draft), self.direction(), actor=self.request.user)


# ---------------------------------------------------------------------------
# Editor
# ---------------------------------------------------------------------------
class RuleEditorView(TemplateObjectMixin, TemplateView):
    """New / edit a rule.  GET shows the page; POST applies one operation (``op``) and re-renders the editor fragment."""

    template_name = "checklists/rules/editor.html"
    body_template_name = "checklists/rules/_editor_body.html"
    rule = None

    def authorize(self) -> None:
        self.version = self.require_draft()
        self.snapshot = snapshot_for_version(self.version)
        self.load_rule()

    def load_rule(self) -> None:
        """New rule by default; the edit view loads its rule (404 if it is not in this draft)."""

    # -- state -----------------------------------------------------------------------------------------------
    def initial_state(self) -> dict[str, Any]:
        if self.rule is None:
            return builder.blank_state()
        return builder.state_from_rule(self.rule.name, self.rule.description, self.rule.condition, self.rule.actions)

    @property
    def code(self) -> str:
        return self.rule.code if self.rule is not None else "NEW"

    def payload(self, state: dict[str, Any], *, strict: bool) -> tuple[Any, list[dict[str, Any]], dict[str, Any] | None, str]:
        """``(condition, actions, shape, json_error)`` from the structured rows, or from the JSON when the rule needs it."""
        if state["json_only"]:
            data, error = builder.parse_json_input(state["json_input"])
            data = data or {}
            return data.get("condition"), data.get("actions", []), None, error
        condition, actions, shape = builder.build_payload(self.snapshot, state, strict=strict)
        return condition, actions, shape, ""

    def run_checks(self, state: dict[str, Any], condition: Any, actions: list[dict[str, Any]], shape: dict[str, Any] | None,
                   *, name: str) -> dict[str, Any]:
        """Validate the rule as it stands (with the version's other rules) and return what to show; never saves."""
        checks: dict[str, Any] = {"errors": [], "warnings": [], "summary": ""}
        if not actions or (shape is not None and not builder.ready(state)):
            return checks
        candidate = RuleSpec(self.code, name, condition, actions, True, 10**6, "")
        others = [r for r in self.snapshot.rules if r.code != self.code]
        report = validate_rule_set(self.snapshot.with_rules([*others, candidate]))
        if shape is not None:  # each finding lands on its own row; the rest are listed
            checks["errors"] = builder.attach_issues(state, report.errors, shape)
        else:
            checks["errors"] = [_describe_issue(i) for i in report.errors]
        checks["warnings"] = [i.message for i in report.warnings if i.rule == self.code or self.code in i.message]
        mine = next((r for r in report.compiled if r.code == self.code), None)
        if mine is not None and not report.errors:
            checks["summary"] = describe_rule(self.snapshot, mine.stored_condition, mine.stored_actions)
        return checks

    # -- rendering -------------------------------------------------------------------------------------------
    def build_context(self, state: dict[str, Any], form: RuleForm, *, strict: bool = False, built: tuple[Any, ...] | None = None) -> dict[str, Any]:
        condition, actions, shape, json_error = built or self.payload(state, strict=strict)
        name = state["name"] or "Untitled rule"
        checks = self.run_checks(state, condition, actions, shape, name=name)
        if json_error:
            checks["errors"].append(json_error)
        state["errors"] = [e for e in state["errors"] if e not in checks["errors"]]
        roots, groups = builder.condition_rows(self.snapshot, state)
        if not state["json_only"] and not json_error and not state["errors"] and not (condition is None and not actions):
            state["json_input"] = json.dumps({"condition": condition, "actions": actions}, indent=2, ensure_ascii=False)
        total = len(state["conditions"]) + sum(len(g["conditions"]) for g in state["groups"])
        return {
            "template": self.template, "version": self.version, "rule": self.rule, "state": state, "form": form, "checks": checks,
            "roots": roots, "groups": groups, "actions": builder.action_rows(self.snapshot, state),
            "source_choices": builder.source_choices(self.snapshot), "action_groups": builder.action_groups(),
            "severities": builder.SEVERITIES, "evidence_kinds": builder.EVIDENCE_KINDS, "editor_url": self.request.path,
            "version_label": f"Draft version {self.version.version_number}",
            "can_add_condition": total < limit("max_conditions_per_rule"),
            "can_add_group": limit("max_group_depth") >= 2 and len(state["groups"]) < builder.MAX_UI_GROUPS and total < limit("max_conditions_per_rule"),
            "can_add_action": len(state["actions"]) < limit("max_actions_per_rule"),
            "rules_url": reverse("checklists:rules", args=[self.template.pk]),
            "simulate_url": reverse("checklists:rule_simulate", args=[self.template.pk]),
        }

    def get_template_names(self) -> list[str]:
        return [self.body_template_name if getattr(self, "partial", False) else self.template_name]

    def respond(self, state: dict[str, Any], form: RuleForm, *, strict: bool = False, partial: bool = False,
                built: tuple[Any, ...] | None = None) -> HttpResponse:
        self.partial = partial
        return self.render_to_response(self.build_context(state, form, strict=strict, built=built))

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        state = self.initial_state()
        form = RuleForm(initial={"name": state["name"], "description": state["description"]})
        return self.respond(state, form)

    # -- operations ------------------------------------------------------------------------------------------
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        state = builder.parse_post(request.POST)
        raw_op = request.POST.get("op")
        if raw_op is None and not _hx(request):  # a plain form submit (the Enter key) means Save
            raw_op = "save"
        operation, args_ = builder.split_operation(raw_op)
        form = RuleForm(request.POST)
        if operation == "save":
            return self.save(request, state, form)
        if operation == "apply_json":
            self.apply_json(state)
        else:
            builder.apply_operation(state, operation, args_)
        form = RuleForm(initial={"name": state["name"], "description": state["description"]})
        return self.respond(state, form, partial=_hx(request))

    def apply_json(self, state: dict[str, Any]) -> None:
        """Load the advanced-JSON panel into the editor - after the same validation a save would apply."""
        data, error = builder.parse_json_input(state["json_input"])
        if error:
            state["errors"] = [error]
            return
        condition, actions = data.get("condition"), data.get("actions", [])
        compiled, issues = compile_rule(self.snapshot, {"code": self.code, "name": state["name"], "condition": condition, "actions": actions})
        problems = [f"{i.path}: {i.message}" if i.path else i.message for i in issues if i.level == "error"]
        if problems:
            state["errors"] = problems
            return
        loaded = builder.state_from_rule(state["name"], state["description"], compiled.stored_condition, compiled.stored_actions)
        loaded["notice"] = "The JSON was checked and loaded into the editor. Save the rule to keep it."
        state.clear()
        state.update(loaded)

    def save(self, request: HttpRequest, state: dict[str, Any], form: RuleForm) -> HttpResponse:
        built = self.payload(state, strict=True)
        condition, actions, shape, json_error = built
        if json_error:
            state["errors"] = [json_error]
        elif form.is_valid() and not builder.has_row_errors(state):
            data = form.cleaned_data
            try:
                saved = self.persist(data["name"], data["description"], condition, actions)
            except RuleValidationError as exc:
                if shape is not None:
                    state["errors"] = builder.attach_issues(state, exc.issues, shape)
                else:
                    state["errors"] = [f"{i.path}: {i.message}" if i.path else i.message for i in exc.issues]
            except ValidationError as exc:
                state["errors"] = _messages(exc)
            else:
                messages.success(request, f"Rule {saved.code} saved.")
                return HttpResponseRedirect(reverse("checklists:rules", args=[self.template.pk]))
        return self.respond(state, form, strict=True, built=built)

    def persist(self, name: str, description: str, condition: Any, actions: list[dict[str, Any]]) -> Any:
        return services.create_rule(self.version, name=name, description=description, condition=condition, actions=actions, actor=self.request.user)


class RuleCreateView(RuleEditorView):
    pass


class RuleUpdateView(RuleEditorView):
    def load_rule(self) -> None:
        self.rule = selectors.get_rule(self.version, self.kwargs["rule_pk"])
        if self.rule is None:
            raise Http404

    def persist(self, name: str, description: str, condition: Any, actions: list[dict[str, Any]]) -> Any:
        return services.update_rule(self.rule, actor=self.request.user, name=name, description=description, condition=condition, actions=actions)


def _messages(exc: ValidationError) -> list[str]:
    return list(exc.messages)


def _describe_issue(issue: Any) -> str:
    return f"{issue.path}: {issue.message}" if issue.path else issue.message


# ---------------------------------------------------------------------------
# Simulator
# ---------------------------------------------------------------------------
class RuleSimulatorView(RulesVersionMixin, TemplateView):
    """Try the saved rules with sample answers.  A simulation: nothing is saved and no answer is stored."""

    template_name = "checklists/rules/simulator.html"
    result_template_name = "checklists/rules/_result.html"

    def get_template_names(self) -> list[str]:
        return [self.result_template_name if getattr(self, "partial", False) else self.template_name]

    def answers_from(self, snapshot: Any, post: Any) -> dict[str, Any]:
        """Sample answers from the form: only this version's questions are read, each value is length-bounded."""
        cap = limit("max_simulation_value_chars")
        answers: dict[str, Any] = {}
        for item in snapshot.items.values():
            key, family = f"ans_{item.code}", item.definition.comparison_family
            if family == "presence":
                if post.get(key) == "1":
                    answers[item.code] = item.definition.example
            elif family == "multi_choice":
                values = [v[:200] for v in post.getlist(key)][: limit("max_operand_items")]
                if values:
                    answers[item.code] = values
            else:
                raw = post.get(key, "")[:cap]
                if raw.strip():
                    answers[item.code] = raw
        return answers

    def build(self, answers: dict[str, Any], snapshot: Any) -> dict[str, Any]:
        result = evaluate(compile_rules(snapshot), answers)
        sections = []
        for code, section in sorted(snapshot.sections.items(), key=lambda kv: kv[1].sort_order):
            items = sorted(snapshot.items_in_section(code), key=lambda i: i.sort_order)
            sections.append({"section": section, "visible": result.sections[code]["visible"],
                             "items": [{"item": i, "result": result.items[i.code]} for i in items]})
        return {"result": result, "sections": sections, "answered": len(answers),
                "hidden_count": sum(1 for r in result.items.values() if not r.visible)}

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        snapshot = snapshot_for_version(self.version)
        context.update({**self.shared_context(), **self.build({}, snapshot)})
        context["inputs"] = [
            {"section": s, "items": [{"item": i, "widget": builder.value_widget(i), "family": i.definition.comparison_family}
                                     for i in sorted(snapshot.items_in_section(s.code), key=lambda i: i.sort_order)]}
            for s in sorted(snapshot.sections.values(), key=lambda s: s.sort_order)
        ]
        context["simulate_url"] = self.request.path
        return context

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if int(request.META.get("CONTENT_LENGTH") or 0) > limit("max_simulation_payload_bytes"):
            return HttpResponse("The simulation request is too large.", status=413)
        snapshot = snapshot_for_version(self.version)
        self.partial = True
        context = {**self.shared_context(), **self.build(self.answers_from(snapshot, request.POST), snapshot)}
        return self.render_to_response(context)


__all__ = [
    "RuleCreateView", "RuleDeleteView", "RuleListView", "RuleMoveView", "RuleSimulatorView", "RuleToggleView", "RuleUpdateView",
]
