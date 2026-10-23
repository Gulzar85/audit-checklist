"""Phase 14: definitions, drafts, validation, publication, immutability, versioning, precedence, conditions (Phase 6 reuse), permissions."""

from __future__ import annotations

import pytest
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction

from apps.core.models import ActivityLog
from apps.workflows import conditions, services as wf
from apps.workflows.models import WorkflowDefinition, WorkflowState, WorkflowTransition, WorkflowVersion

from workflows_support import CA_STATES, CA_TRANSITIONS, build, build_workflow_world, ca_workflow


@pytest.fixture
def w(org_a, org_b, make_user, assign, make_type):
    return build_workflow_world(org_a, org_b, make_user, assign, make_type)


def simple(w, **kw):
    return build(w, code=kw.pop("code", "simple"), states=[{"code": "a", "name": "A", "kind": "initial"}, {"code": "b", "name": "B", "kind": "success"}],
                 transitions=[{"source": "a", "target": "b", "action": "go", "label": "Go", "actor_rule": "authorized"}], **kw)


class TestDefinitions:
    def test_create_with_an_empty_draft_and_audit(self, w):
        d = wf.create_definition(actor=w["designer"], organization=w["org_a"], code="Review", name="Review", target_type="finding", description="x")
        assert (d.code, d.target_type, d.status, d.is_system) == ("review", "finding", "active", False)
        v = d.versions.get()
        assert (v.number, v.status) == (1, "draft") and ActivityLog.objects.filter(verb="workflow.definition_created", actor=w["designer"]).exists()

    def test_code_scope_target_type_and_event_validation(self, w):
        wf.create_definition(actor=w["admin"], organization=w["org_a"], code="one", name="One", target_type="finding")
        with pytest.raises(wf.WorkflowError):  # unique within the organization
            wf.create_definition(actor=w["admin"], organization=w["org_a"], code="one", name="Again", target_type="finding")
        for kw, code in (({"target_type": "user"}, "invalid_target_type"), ({"target_type": "finding", "code": "Bad Code!"}, "invalid_code"),
                         ({"target_type": "finding", "auto_start_event": "nope"}, "invalid_event"), ({"target_type": "finding", "name": " "}, "invalid_text")):
            with pytest.raises(wf.WorkflowError) as exc:
                wf.create_definition(actor=w["admin"], organization=w["org_a"], **{"code": "x2", "name": "X", **kw})
            assert exc.value.workflow_code in (code, "duplicate_code", "conflict") or code in str(exc.value.workflow_code)

    def test_creating_needs_the_permission_in_that_organization(self, w, org_b):
        for who in ("aviewer", "wf_runner", "b_admin"):
            with pytest.raises(PermissionDenied):
                wf.create_definition(actor=w[who], organization=w["org_a"], code="x", name="X", target_type="finding")
        with pytest.raises(PermissionDenied):  # only platform superusers create system workflows
            wf.create_definition(actor=w["admin"], organization=None, code="sys", name="Sys", target_type="finding")

    def test_one_default_per_target_and_the_organization_default_overrides_the_system_one(self, w, django_user_model):
        root = django_user_model.objects.create_superuser(email="root-wf@example.com", password="x-Strong-pass-123")
        system = wf.create_definition(actor=root, organization=None, code="sys-ca", name="System", target_type="corrective_action", is_default=True)
        own = wf.create_definition(actor=w["admin"], organization=w["org_a"], code="own-ca", name="Own", target_type="corrective_action", is_default=True)
        other = wf.create_definition(actor=w["admin"], organization=w["org_a"], code="own-ca-2", name="Own 2", target_type="corrective_action", is_default=True)
        assert [WorkflowDefinition.objects.get(pk=d.pk).is_default for d in (system, own, other)] == [True, False, True]
        for d in (system, other, own):  # publish all so they can be resolved
            _publish_minimal(w, d, root)
        assert wf.resolve_definition(w["org_a"], "corrective_action").pk == other.pk  # the organization's default wins
        WorkflowDefinition.objects.filter(pk=other.pk).update(is_default=False)
        assert wf.resolve_definition(w["org_a"], "corrective_action").pk == system.pk  # then the system default applies
        assert wf.resolve_definition(w["org_b"], "corrective_action").pk == system.pk
        assert wf.resolve_definition(w["org_a"], "corrective_action", "own-ca").pk == own.pk  # an explicit code

    def test_update_and_archive(self, w):
        d = simple(w)
        wf.update_definition(d, actor=w["designer"], name="Renamed", description="d")
        d.refresh_from_db()
        assert d.name == "Renamed"
        with pytest.raises(wf.WorkflowError):
            wf.update_definition(d, actor=w["designer"], auto_start_event="bogus")
        wf.archive_definition(d, actor=w["designer"])
        with pytest.raises(wf.WorkflowConflict):
            wf.archive_definition(d, actor=w["designer"])
        with pytest.raises(wf.WorkflowConflict):
            wf.update_definition(d, actor=w["designer"], name="x")
        with pytest.raises(wf.WorkflowConflict):
            wf.create_draft(d, actor=w["designer"])


class TestDraftEditing:
    def test_states_and_transitions_are_validated_on_write(self, w):
        d = wf.create_definition(actor=w["admin"], organization=w["org_a"], code="edit", name="Edit", target_type="corrective_action")
        v = d.versions.get()
        a = wf.add_state(v, actor=w["designer"], code="a", name="A", kind="initial")
        b = wf.add_state(v, actor=w["designer"], code="b", name="B", kind="success")
        with pytest.raises(wf.WorkflowConflict) as exc:
            wf.add_state(v, actor=w["designer"], code="c", name="C", kind="initial")
        assert exc.value.workflow_code == "initial_exists"
        with pytest.raises(wf.WorkflowConflict):
            wf.add_state(v, actor=w["designer"], code="a", name="Dup")
        for kw in ({"kind": "bogus"}, {"approval_mode": "most"}, {"min_approvals": 0}, {"due_hours": 0}, {"assignee_policy": {"type": "script", "code": "1"}}, {"separation": ["root"]},
                   {"assignee_policy": {"type": "permission", "permission": "not.registered"}}, {"assignee_policy": {"type": "user", "users": ["00000000-0000-0000-0000-000000000000"]}}):
            with pytest.raises(wf.WorkflowError):
                wf.add_state(v, actor=w["designer"], code="x1", name="X", **kw)
        with pytest.raises(wf.WorkflowError) as exc:
            wf.update_state(a, actor=w["designer"], code="renamed")
        assert exc.value.workflow_code == "code_immutable"
        for kw in ({"handler": "os.system"}, {"handler": "finding.close"}, {"actor_rule": "root"}, {"decision": "x"}, {"source": "nope"}, {"condition": {"attribute": "priority", "operator": "EVAL", "value": "x"}},
                   {"condition": {"attribute": "__class__", "operator": "EQUALS", "value": "x"}}):
            with pytest.raises(wf.WorkflowError):
                wf.add_transition(v, actor=w["designer"], **{"source": a, "target": b, "action": "go", "label": "Go", **kw})
        t = wf.add_transition(v, actor=w["designer"], source="a", target="b", action="go", label="Go", handler="corrective_action.submit",
                              condition={"attribute": "priority", "operator": "EQUALS", "value": "high"})
        assert t.condition == {"attribute": "priority", "operator": "EQUALS", "value": "high"} and t.handler == "corrective_action.submit"
        wf.update_transition(t, actor=w["designer"], label="Proceed", sort_order=20)
        with pytest.raises(wf.WorkflowConflict):
            wf.delete_state(a, actor=w["designer"])  # still used by a transition
        wf.delete_transition(t, actor=w["designer"])
        wf.delete_state(b, actor=w["designer"])
        assert not WorkflowState.objects.filter(pk=b.pk).exists()

    def test_editing_needs_the_permission(self, w):
        d = wf.create_definition(actor=w["admin"], organization=w["org_a"], code="perm", name="Perm", target_type="finding")
        v = d.versions.get()
        for who in ("aviewer", "wf_runner", "b_admin", "r2_actor"):
            with pytest.raises(PermissionDenied):
                wf.add_state(v, actor=w[who], code="a", name="A")
            with pytest.raises(PermissionDenied):
                wf.update_version(v, actor=w[who], change_summary="x")

    def test_a_second_draft_is_refused_and_a_clone_is_independent(self, w):
        d = simple(w)
        pub = d.versions.get(status="published")
        draft = wf.create_draft(d, actor=w["designer"], change_summary="v2")
        assert (draft.number, draft.status) == (2, "draft") and draft.states.count() == 2 and draft.transitions.count() == 1
        with pytest.raises(wf.WorkflowConflict) as exc:
            wf.create_draft(d, actor=w["designer"])
        assert exc.value.workflow_code == "draft_exists"
        wf.add_state(draft, actor=w["designer"], code="c", name="C", kind="intermediate")
        assert pub.states.count() == 2 and draft.states.count() == 3  # the published version is untouched


class TestValidationAndPublication:
    def test_a_valid_version_publishes_sealed_with_a_hash(self, w):
        d = ca_workflow(w)
        v = d.versions.get()
        assert v.status == "published" and len(v.config_hash) == 64 and v.published_by == w["admin"] and v.published_at
        assert v.config_hash == wf.config_hash(v)
        assert ActivityLog.objects.filter(verb="workflow.version_published", metadata__config_hash=v.config_hash).exists()

    @pytest.mark.parametrize("states,transitions,expected", [
        ([], [], "no states"),
        ([{"code": "a", "name": "A", "kind": "intermediate"}, {"code": "b", "name": "B", "kind": "success"}], [{"source": "a", "target": "b", "action": "go", "label": "Go"}], "initial state"),
        ([{"code": "a", "name": "A", "kind": "initial"}, {"code": "b", "name": "B", "kind": "failure"}], [{"source": "a", "target": "b", "action": "go", "label": "Go"}], "completes the workflow successfully"),
        ([{"code": "a", "name": "A", "kind": "initial"}, {"code": "b", "name": "B", "kind": "success"}, {"code": "c", "name": "C", "kind": "intermediate"}],
         [{"source": "a", "target": "b", "action": "go", "label": "Go"}], "dead end"),
        ([{"code": "a", "name": "A", "kind": "initial"}, {"code": "b", "name": "B", "kind": "success"}, {"code": "c", "name": "C", "kind": "failure"}],
         [{"source": "a", "target": "b", "action": "go", "label": "Go"}], "cannot be reached"),
        ([{"code": "a", "name": "A", "kind": "initial"}, {"code": "b", "name": "B", "kind": "success"}],
         [{"source": "a", "target": "b", "action": "go", "label": "Go"}, {"source": "b", "target": "a", "action": "back", "label": "Back"}], "cannot have transitions leaving"),
        ([{"code": "a", "name": "A", "kind": "initial"}, {"code": "m", "name": "M", "kind": "intermediate"}, {"code": "b", "name": "B", "kind": "success"}],
         [{"source": "a", "target": "m", "action": "go", "label": "Go"}, {"source": "m", "target": "a", "action": "back", "label": "Back"}, {"source": "m", "target": "b", "action": "done", "label": "Done"}], "contains a loop"),
        ([{"code": "a", "name": "A", "kind": "initial"}, {"code": "b", "name": "B", "kind": "success"}], [{"source": "a", "target": "b", "action": "go", "label": "Go", "actor_rule": "assignee"}], "no assignee policy"),
        ([{"code": "a", "name": "A", "kind": "initial", "assignee_policy": {"type": "permission", "permission": "finding.view"}, "approval_mode": "all"}, {"code": "b", "name": "B", "kind": "success"}],
         [{"source": "a", "target": "b", "action": "go", "label": "Go"}], "specific users"),
    ])
    def test_invalid_versions_are_refused_with_clear_errors(self, w, states, transitions, expected):
        d = build(w, code="bad", states=states, transitions=transitions, publish=False)
        report = wf.validate_version(d.versions.get(), actor=w["designer"])
        assert not report.ok and any(expected in e for e in report.errors), report.errors
        with pytest.raises(wf.WorkflowValidationError) as exc:
            wf.publish(d.versions.get(), actor=w["designer"])
        assert not exc.value.report.ok and d.versions.get().status == "draft"

    def test_loops_need_to_be_allowed_explicitly(self, w):
        states = [{"code": "a", "name": "A", "kind": "initial"}, {"code": "m", "name": "M", "kind": "intermediate"}, {"code": "b", "name": "B", "kind": "success"}]
        transitions = [{"source": "a", "target": "m", "action": "go", "label": "Go", "actor_rule": "authorized"}, {"source": "m", "target": "a", "action": "back", "label": "Back", "actor_rule": "authorized"},
                       {"source": "m", "target": "b", "action": "done", "label": "Done", "actor_rule": "authorized"}]
        d = build(w, code="loop", states=states, transitions=transitions, allow_loops=True)
        assert d.versions.get().status == "published"

    def test_branch_rules_and_warnings(self, w):
        states = [{"code": "a", "name": "A", "kind": "initial"}, {"code": "b", "name": "B", "kind": "success"}, {"code": "c", "name": "C", "kind": "failure"}]
        cond = {"attribute": "priority", "operator": "EQUALS", "value": "high"}
        d = build(w, code="branch", states=states, publish=False, transitions=[
            {"source": "a", "target": "b", "action": "go", "label": "Go", "actor_rule": "authorized", "condition": cond, "sort_order": 10},
            {"source": "a", "target": "c", "action": "stop", "label": "Stop", "actor_rule": "authorized"}])
        report = wf.validate_version(d.versions.get(), actor=w["designer"])
        assert report.ok and any("no fallback branch" in x for x in report.warnings)
        d2 = build(w, code="branch2", states=states, publish=False, transitions=[
            {"source": "a", "target": "c", "action": "go", "label": "Go", "actor_rule": "authorized", "sort_order": 10},
            {"source": "a", "target": "b", "action": "go", "label": "Go", "actor_rule": "authorized", "condition": cond, "sort_order": 20}])
        report = wf.validate_version(d2.versions.get(), actor=w["designer"])
        assert any("must come last" in e for e in report.errors)

    def test_publishing_supersedes_the_previous_version_and_needs_permission(self, w):
        d = simple(w)
        draft = wf.create_draft(d, actor=w["designer"])
        with pytest.raises(PermissionDenied):
            wf.publish(draft, actor=w["wf_runner"])
        wf.publish(draft, actor=w["designer"])
        assert dict(d.versions.values_list("number", "status")) == {1: "superseded", 2: "published"}
        with pytest.raises(wf.WorkflowConflict):
            wf.publish(draft, actor=w["designer"])  # no longer a draft

    def test_archiving_versions(self, w):
        d = simple(w)
        wf.publish(wf.create_draft(d, actor=w["designer"]), actor=w["designer"])
        old = d.versions.get(number=1)
        assert wf.archive_version(old, actor=w["designer"]).status == "archived"
        with pytest.raises(wf.WorkflowConflict):
            wf.archive_version(d.versions.get(number=2), actor=w["designer"])  # the published one is replaced, not archived


@pytest.mark.django_db
class TestImmutability:
    """A published version cannot be changed by anything: services, ORM updates, child edits, deletes."""

    def test_the_services_refuse_a_published_version(self, w):
        d = simple(w)
        v = d.versions.get()
        s = v.states.first()
        for call in (lambda: wf.add_state(v, actor=w["admin"], code="z", name="Z"), lambda: wf.update_state(s, actor=w["admin"], name="Z"),
                     lambda: wf.delete_state(s, actor=w["admin"]), lambda: wf.update_version(v, actor=w["admin"], change_summary="x"),
                     lambda: wf.add_transition(v, actor=w["admin"], source="a", target="b", action="x", label="X"),
                     lambda: wf.delete_transition(v.transitions.first(), actor=w["admin"])):
            with pytest.raises(wf.WorkflowConflict) as exc:
                call()
            assert exc.value.workflow_code == "version_immutable"

    def test_the_database_refuses_everything_else(self, w):
        d = simple(w)
        v = d.versions.get()
        s, t = v.states.first(), v.transitions.first()
        for fn in (lambda: WorkflowState.objects.filter(pk=s.pk).update(name="hacked"), lambda: WorkflowTransition.objects.filter(pk=t.pk).update(label="hacked"),
                   lambda: WorkflowState.objects.filter(pk=s.pk).delete(), lambda: WorkflowTransition.objects.filter(pk=t.pk).delete(),
                   lambda: WorkflowState.objects.create(version=v, code="new", name="New"), lambda: WorkflowVersion.objects.filter(pk=v.pk).update(change_summary="hacked"),
                   lambda: WorkflowVersion.objects.filter(pk=v.pk).update(config_hash="f" * 64), lambda: WorkflowVersion.objects.filter(pk=v.pk).update(start_condition={"x": 1}),
                   lambda: WorkflowVersion.objects.filter(pk=v.pk).update(status="draft"), lambda: WorkflowVersion.objects.filter(pk=v.pk).delete()):
            with pytest.raises(IntegrityError), transaction.atomic():
                fn()
        s.refresh_from_db()
        assert s.name != "hacked" and WorkflowVersion.objects.get(pk=v.pk).status == "published"

    def test_a_transition_connects_states_of_its_own_version(self, w):
        d1, d2 = simple(w, code="one"), simple(w, code="two")
        v = wf.create_draft(d1, actor=w["admin"])
        foreign = d2.versions.get().states.first()
        with pytest.raises(IntegrityError), transaction.atomic():
            WorkflowTransition.objects.create(version=v, source=v.states.first(), target=foreign, action="x", label="X")

    def test_only_one_draft_one_published_and_one_initial_state(self, w):
        d = simple(w)
        v = d.versions.get()
        with pytest.raises(IntegrityError), transaction.atomic():
            WorkflowVersion.objects.create(definition=d, number=9, status="published", config_hash="a" * 64, published_at=v.published_at)
        draft = wf.create_draft(d, actor=w["admin"])
        with pytest.raises(IntegrityError), transaction.atomic():
            WorkflowVersion.objects.create(definition=d, number=10)  # a second draft
        with pytest.raises(IntegrityError), transaction.atomic():
            WorkflowState.objects.create(version=draft, code="init2", name="Init 2", kind="initial")

    def test_versions_and_definitions_are_never_deleted(self, w):
        d = simple(w)
        with pytest.raises(IntegrityError), transaction.atomic():
            WorkflowVersion.objects.filter(definition=d).delete()


class TestConditionsReusePhase6:
    def test_conditions_use_the_phase_6_operators_and_three_valued_logic(self, w):
        node = {"logic": "AND", "conditions": [{"attribute": "severity_rank", "operator": "GREATER_THAN_OR_EQUAL", "value": 30},
                                              {"attribute": "priority", "operator": "CONTAINS_ANY", "value": ["high", "critical"]}]}
        cleaned, problems = conditions.clean_condition("corrective_action", node)
        assert cleaned == node and not problems
        from apps.checklists.rules.tri import Tri

        assert conditions.evaluate("corrective_action", node, {"severity_rank": 30, "priority": "high"}) is Tri.TRUE
        assert conditions.evaluate("corrective_action", node, {"severity_rank": 20, "priority": "high"}) is Tri.FALSE
        assert conditions.evaluate("corrective_action", node, {"severity_rank": 30}) is Tri.UNKNOWN  # a missing value is unknown, never false
        assert conditions.evaluate("corrective_action", None, {}) is Tri.TRUE

    @pytest.mark.parametrize("node", [
        {"attribute": "nope", "operator": "EQUALS", "value": 1}, {"attribute": "priority", "operator": "GREATER_THAN", "value": "high"},
        {"attribute": "priority", "operator": "EQUALS", "value": "urgent"}, {"attribute": "severity_rank", "operator": "EQUALS", "value": "30"},
        {"attribute": "severity_rank", "operator": "BETWEEN", "value": [40, 10]}, {"attribute": "priority", "operator": "EQUALS", "value": "high", "extra": 1},
        {"logic": "XOR", "conditions": [{"attribute": "priority", "operator": "EQUALS", "value": "high"}]}, {"logic": "AND", "conditions": []},
        {"attribute": "priority", "operator": "EQUALS", "value": "__import__('os')"}, {"attribute": "has_owner", "operator": "EQUALS", "value": "yes"}, "priority == 'high'", 5,
    ])
    def test_malformed_or_executable_conditions_are_refused(self, node):
        cleaned, problems = conditions.clean_condition("corrective_action", node)
        assert cleaned is None and problems

    def test_depth_and_size_limits(self):
        leaf = {"attribute": "priority", "operator": "EQUALS", "value": "high"}
        deep = {"logic": "AND", "conditions": [{"logic": "OR", "conditions": [{"logic": "AND", "conditions": [{"logic": "OR", "conditions": [leaf]}]}]}]}
        assert conditions.clean_condition("corrective_action", deep)[1]
        wide = {"logic": "OR", "conditions": [leaf] * 21}
        assert conditions.clean_condition("corrective_action", wide)[1]

    def test_unknown_attributes_fail_closed_at_runtime(self):
        from apps.checklists.rules.tri import Tri

        stale = {"attribute": "deleted_attribute", "operator": "EQUALS", "value": "x"}
        assert conditions.evaluate("corrective_action", stale, {"deleted_attribute": "x"}) is Tri.UNKNOWN
        assert conditions.evaluate("unknown_type", stale, {}) is Tri.UNKNOWN

    def test_describe_reads_naturally(self):
        assert conditions.describe("finding", {"attribute": "severity_rank", "operator": "GREATER_THAN_OR_EQUAL", "value": 30}) == "Severity rank is at least 30"


def _publish_minimal(w, definition, actor):
    v = definition.versions.get(status="draft")
    wf.add_state(v, actor=actor, code="a", name="A", kind="initial")
    wf.add_state(v, actor=actor, code="b", name="B", kind="success")
    wf.add_transition(v, actor=actor, source="a", target="b", action="go", label="Go", actor_rule="authorized")
    wf.publish(v, actor=actor)


_ = (CA_STATES, CA_TRANSITIONS)
