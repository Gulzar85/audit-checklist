"""Evidence services: upload pipeline, lifecycle, scanning policy, targets, idempotency, failure cleanup, DB integrity, Phase 5/6/9 integration."""

from __future__ import annotations

import uuid

import pytest
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.db import IntegrityError, transaction

from apps.core.models import ActivityLog
from apps.evidence import lifecycle, selectors, services
from apps.evidence.models import EvidenceAsset, EvidenceAttachment
from apps.evidence.scanning import EICAR, ScanResult
from apps.evidence.storage import get_storage
from apps.executions import services as exec_services
from apps.executions.models import ChecklistExecution

from evidence_support import build_evidence_world, evidence_storage, jpeg_bytes, pdf_bytes, png_bytes, vissued  # noqa: F401
from executions_support import item_id

PHOTO = "PHOTO"


@pytest.fixture
def w(org_a, org_b, make_user, assign, make_type):
    return build_evidence_world(org_a, org_b, make_user, assign, make_type)


def iid(w, code):
    return item_id(w["vversion"], code)


def started(w, who="runner", **kw):
    ex, _ = exec_services.start_or_resume(vissued(w, **kw), actor=w[who])
    return ex


def up(w, ex, code=PHOTO, who="runner", name="a.jpg", data=None, **kw):
    return services.upload(w[who], target_type="checklist_response", ref={"execution_id": str(ex.pk), "item_id": iid(w, code)},
                           filename=name, data=data if data is not None else jpeg_bytes(), **kw)


def code_of(call):
    with pytest.raises(services.EvidenceError) as exc:
        call()
    return exc.value.evidence_code


def eicar():
    return jpeg_bytes(trailer=b"")  # (an image cannot carry EICAR after re-encoding; infected paths use a scanner double instead)


class ForceScanner:
    def __init__(self, status, detail=""):
        self.result = ScanResult(status, detail)

    def scan(self, data):
        return self.result


@pytest.mark.django_db
class TestUploadPipeline:
    def test_a_valid_upload_is_stored_privately_attached_audited_and_available(self, w, settings):
        settings.EVIDENCE_SCANNER = "apps.evidence.scanning.EicarTestScanner"
        ex = started(w)
        r = up(w, ex)
        asset, att = r.asset, r.attachment
        assert asset.status == "available" and asset.scan_status == "clean" and asset.kind == "photo" and asset.content_type == "image/jpeg"
        assert asset.organization_id == w["org"].pk and asset.uploaded_by_id == w["runner"].pk and att.execution_id == ex.pk and att.item.code == PHOTO
        assert len(asset.sha256) == 64 and asset.size > 0 and (asset.width, asset.height) == (64, 48)
        assert get_storage().exists(asset.storage_key) and asset.storage_key != asset.original_name
        verbs = set(ActivityLog.objects.values_list("verb", flat=True))
        assert {"evidence.uploaded", "evidence.available"} <= verbs

    def test_stored_bytes_are_the_reencoded_image_without_metadata(self, w):
        r = up(w, started(w), data=jpeg_bytes(exif=True, trailer=b"PAYLOAD"))
        stored = services.read_bytes(r.asset)
        assert b"PAYLOAD" not in stored and b"SecretCameraMaker" not in stored

    def test_rejections_are_audited_and_store_nothing(self, w):
        ex = started(w)
        for name, data, expected in (("a.exe", b"MZ" + b"\0" * 50, "type_not_allowed"), ("a.jpg", b"<html>", "content_mismatch"), ("a.jpg", b"", "empty_file")):
            assert code_of(lambda: up(w, ex, name=name, data=data)) == expected
        assert not EvidenceAsset.objects.exists() and not list(get_storage().keys())
        assert ActivityLog.objects.filter(verb="evidence.rejected").count() == 3

    def test_the_per_target_limit_comes_from_the_question_configuration(self, w):
        ex = started(w)
        up(w, ex)
        up(w, ex, name="b.jpg")
        assert code_of(lambda: up(w, ex, name="c.jpg")) == "too_many_files"  # the photo question allows 2
        assert EvidenceAttachment.objects.count() == 2

    def test_question_types_are_enforced(self, w):
        ex = started(w)
        assert code_of(lambda: up(w, ex, name="d.pdf", data=pdf_bytes())) == "type_not_allowed"  # a photo question refuses a pdf
        assert up(w, ex, code="DOC", name="d.pdf", data=pdf_bytes()).asset.kind == "file"
        assert up(w, ex, code="SIGN", name="s.png", data=png_bytes(), caption="A. Khan").asset.kind == "signature"

    def test_items_that_take_no_evidence_are_not_targets(self, w):
        ex = started(w)
        assert code_of(lambda: up(w, ex, code="OK", name="a.jpg")) == "target_not_found"
        assert code_of(lambda: up(w, ex, code="TEMP")) == "target_not_found"  # TEMP only takes evidence when the rule fires

    def test_a_rule_can_make_an_item_take_evidence(self, w):
        ex = started(w)
        exec_services.save_answers(ex, actor=w["runner"], inputs=[exec_services.AnswerInput(iid(w, "TEMP"), "9.5")])
        assert up(w, ex, code="TEMP").asset.kind == "photo"


@pytest.mark.django_db
class TestScanPolicy:
    def test_infected_files_are_quarantined_never_available(self, w, settings, monkeypatch):
        monkeypatch.setattr(lifecycle, "get_scanner", lambda: ForceScanner("infected", "Eicar-Test"))
        r = up(w, started(w))
        assert r.asset.status == "quarantined" and r.asset.scan_status == "infected" and not selectors.is_servable(r.asset)
        assert ActivityLog.objects.filter(verb="evidence.quarantined").exists()
        assert lifecycle.apply_scan(r.asset, ScanResult("clean")).status == "quarantined"  # final: no way back
        with pytest.raises(lifecycle.InvalidTransition):
            lifecycle.assert_transition("quarantined", "available")

    def test_scanner_errors_leave_the_file_pending_when_a_scan_is_required(self, w, monkeypatch):
        monkeypatch.setattr(lifecycle, "get_scanner", lambda: ForceScanner("error", "down"))
        r = up(w, started(w))
        assert r.asset.status == "pending_scan" and r.asset.scan_status == "error" and not selectors.is_servable(r.asset)

    def test_no_configured_scanner_never_means_clean(self, w, settings):
        settings.EVIDENCE_SCANNER = "apps.evidence.scanning.UnconfiguredScanner"
        r = up(w, started(w))
        assert r.asset.status == "pending_scan" and r.asset.scan_status == "error"
        settings.EVIDENCE_SCAN_REQUIRED = False  # the explicit development policy: recorded as NOT REQUIRED, never as clean
        again = lifecycle.scan_asset(r.asset)
        assert again.status == "available" and again.scan_status == "not_required"

    def test_the_retry_command_completes_pending_scans(self, w, settings, monkeypatch):
        monkeypatch.setattr(lifecycle, "get_scanner", lambda: ForceScanner("error"))
        r = up(w, started(w))
        assert r.asset.status == "pending_scan"
        monkeypatch.setattr(lifecycle, "get_scanner", lambda: ForceScanner("clean"))
        call_command("scan_evidence")
        r.asset.refresh_from_db()
        assert r.asset.status == "available" and r.asset.scan_status == "clean"

    def test_a_broken_scanner_cannot_make_a_file_available(self, w, monkeypatch):
        class Boom:
            def scan(self, data):
                raise RuntimeError("boom")

        monkeypatch.setattr(lifecycle, "get_scanner", lambda: Boom())
        assert up(w, started(w)).asset.status == "pending_scan"

    def test_the_database_refuses_an_available_file_without_a_safe_scan(self, w):
        r = up(w, started(w))
        with pytest.raises(IntegrityError), transaction.atomic():
            EvidenceAsset.objects.filter(pk=r.asset.pk).update(scan_status="pending")


@pytest.mark.django_db
class TestTargetsAndAuthorization:
    def test_only_registered_targets_resolve(self, w):
        ex = started(w)
        for target in ("auth.User", "finding", "../x", "", "checklist_response "):
            assert code_of(lambda: services.upload(w["runner"], target_type=target, ref={"execution_id": str(ex.pk), "item_id": iid(w, PHOTO)}, filename="a.jpg", data=jpeg_bytes())) == "target_not_found"

    def test_foreign_scope_and_unknown_references_are_all_not_found(self, w):
        ex = started(w)
        other_item = item_id(w["eversion"], "FIRE-EXIT")  # an item of another version
        for who, ref in (("other", {"execution_id": str(ex.pk), "item_id": iid(w, PHOTO)}), ("plain", {"execution_id": str(ex.pk), "item_id": iid(w, PHOTO)}),
                         ("runner", {"execution_id": str(ex.pk), "item_id": other_item}), ("runner", {"execution_id": "nope", "item_id": iid(w, PHOTO)}),
                         ("runner", {"execution_id": str(uuid.uuid4()), "item_id": iid(w, PHOTO)}), ("runner", {"execution_id": str(ex.pk), "item_id": "nope"})):
            assert code_of(lambda: services.upload(w[who], target_type="checklist_response", ref=ref, filename="a.jpg", data=jpeg_bytes())) == "target_not_found", (who, ref)
        assert not EvidenceAsset.objects.exists()

    def test_cross_organization_attachment_is_impossible(self, w):
        ex = started(w)
        up(w, ex)
        asset = EvidenceAsset.objects.get()
        assert selectors.get_asset(w["other"], asset.pk) is None  # another organization cannot reach the file by id
        assert selectors.get_attachment(w["other"], EvidenceAttachment.objects.get().pk) is None

    def test_view_only_users_see_but_cannot_attach_or_detach(self, w):
        ex = started(w)
        att = up(w, ex).attachment
        assert selectors.get_attachment(w["manager"], att.pk) is not None  # execution.view at Area 1
        with pytest.raises(PermissionDenied):
            up(w, ex, who="manager", name="b.jpg")
        with pytest.raises(PermissionDenied):
            services.detach(w["manager"], att)
        assert selectors.get_attachment(w["viewer"], att.pk) is None  # assignment.view only: not execution.view

    def test_a_designated_assignment_is_not_open_to_others(self, w):
        ex = started(w, assigned_to=w["runner"])
        with pytest.raises(PermissionDenied):
            up(w, ex, who="runner2")

    def test_revoked_permissions_stop_access_immediately(self, w):
        from apps.permissions.authorization import invalidate_all
        from apps.permissions.models import MembershipRole

        ex = started(w)
        att = up(w, ex).attachment
        MembershipRole.objects.filter(membership__user=w["runner"]).delete()
        invalidate_all()
        assert selectors.get_attachment(w["runner"], att.pk) is None  # the same scope rule that grants viewing also revokes it
        with pytest.raises((PermissionDenied, services.EvidenceError)):
            up(w, ex, name="x.jpg")
        assert EvidenceAttachment.objects.count() == 1

    def test_submitted_executions_are_locked(self, w):
        ex = started(w)
        up(w, ex)
        exec_services.save_answers(ex, actor=w["runner"], inputs=[exec_services.AnswerInput(iid(w, "OK"), "yes")])
        exec_services.submit(ex, actor=w["runner"])
        assert code_of(lambda: up(w, ex, name="late.jpg")) == "already_submitted"
        att = EvidenceAttachment.objects.get()
        assert code_of(lambda: services.detach(w["runner"], att)) == "already_submitted"
        assert EvidenceAttachment.objects.get().detached_at is None

    def test_cancelled_executions_are_locked(self, w):
        ex = started(w)
        exec_services.cancel_execution(ex, actor=w["admin"], reason="x")
        assert code_of(lambda: up(w, ex)) in ("not_editable", "target_not_found")


@pytest.mark.django_db
class TestAttachDetach:
    def test_detach_keeps_the_file_and_history_and_is_idempotent(self, w):
        ex = started(w)
        r = up(w, ex)
        row = services.detach(w["runner"], r.attachment, reason="wrong photo")
        again = services.detach(w["runner"], r.attachment)
        assert row.detached_at and row.detach_reason == "wrong photo" and again.pk == row.pk
        assert EvidenceAsset.objects.count() == 1 and get_storage().exists(r.asset.storage_key)
        assert not selectors.attachments_for_execution(ex).exists() and selectors.attachments_for_execution(ex, include_detached=True).count() == 1
        assert ActivityLog.objects.filter(verb="evidence.detached").count() == 1

    def test_a_detached_attachment_cannot_be_reattached_but_the_asset_can(self, w):
        ex = started(w)
        r = up(w, ex)
        services.detach(w["runner"], r.attachment)
        with pytest.raises(IntegrityError), transaction.atomic():
            EvidenceAttachment.objects.filter(pk=r.attachment.pk).update(detached_at=None, detach_reason="")
        again = services.attach_existing(w["runner"], r.asset, target_type="checklist_response", ref={"execution_id": str(ex.pk), "item_id": iid(w, PHOTO)})
        assert again.pk != r.attachment.pk and EvidenceAsset.objects.count() == 1  # no second physical file

    def test_attach_existing_requires_visibility_and_availability(self, w, monkeypatch):
        ex = started(w)
        r = up(w, ex)
        ref = {"execution_id": str(ex.pk), "item_id": iid(w, "DOC")}
        assert code_of(lambda: services.attach_existing(w["other"], r.asset, target_type="checklist_response", ref=ref)) == "asset_not_found"
        with pytest.raises(PermissionDenied):
            services.attach_existing(w["manager"], r.asset, target_type="checklist_response", ref=ref)
        att = services.attach_existing(w["runner"], r.asset, target_type="checklist_response", ref=ref)
        assert att.item.code == "DOC" and code_of(lambda: services.attach_existing(w["runner"], r.asset, target_type="checklist_response", ref=ref)) == "already_attached"
        monkeypatch.setattr(lifecycle, "get_scanner", lambda: ForceScanner("error"))
        pending = up(w, ex, name="p.jpg").asset
        assert code_of(lambda: services.attach_existing(w["runner"], pending, target_type="checklist_response", ref={"execution_id": str(ex.pk), "item_id": iid(w, "DOC")})) == "asset_not_available"


@pytest.mark.django_db
class TestIdempotencyAndFailures:
    def test_a_retried_operation_returns_the_original_and_creates_nothing(self, w):
        ex = started(w)
        op = uuid.uuid4()
        data = jpeg_bytes()
        first = up(w, ex, operation_id=op, data=data)
        second = up(w, ex, operation_id=op, data=data)
        assert not first.duplicate and second.duplicate and second.asset.pk == first.asset.pk
        assert EvidenceAsset.objects.count() == 1 and EvidenceAttachment.objects.count() == 1 and len(list(get_storage().keys())) == 1

    def test_an_operation_id_cannot_be_reused_for_a_different_file(self, w):
        ex = started(w)
        op = uuid.uuid4()
        up(w, ex, operation_id=op, data=jpeg_bytes(size=(64, 48)))
        assert code_of(lambda: up(w, ex, operation_id=op, name="b.jpg", data=jpeg_bytes(size=(80, 60)))) == "operation_id_reused"
        assert EvidenceAsset.objects.count() == 1

    def test_operation_ids_are_per_user(self, w):
        ex = started(w)
        op = uuid.uuid4()
        a = up(w, ex, operation_id=op)
        b = up(w, ex, who="runner2", operation_id=op, name="b.jpg", data=jpeg_bytes(size=(70, 70)))
        assert a.asset.pk != b.asset.pk

    def test_a_storage_write_failure_creates_no_rows(self, w, monkeypatch):
        from apps.evidence.storage import FileSystemEvidenceStorage, StorageError

        def fail(self, key, data):
            raise StorageError("disk full")

        monkeypatch.setattr(FileSystemEvidenceStorage, "put", fail)
        assert code_of(lambda: up(w, started(w))) == "storage_failed"
        assert not EvidenceAsset.objects.exists() and not EvidenceAttachment.objects.exists()

    def test_a_database_failure_after_the_file_was_stored_removes_the_file(self, w, monkeypatch):
        ex = started(w)

        def boom(self, *a, **k):
            raise RuntimeError("database went away")

        monkeypatch.setattr(EvidenceAttachment, "save", boom)
        with pytest.raises(RuntimeError):
            up(w, ex)
        assert not EvidenceAsset.objects.exists() and not list(get_storage().keys())  # compensation: no orphan, nothing public

    def test_reconciliation_removes_orphans_and_reports_missing_files(self, w, capsys):
        from apps.evidence.storage import new_key

        ex = started(w)
        r = up(w, ex)
        orphan = new_key()
        get_storage().put(orphan, b"left behind by a crash")
        import os
        import time

        path = get_storage()._path(orphan)  # noqa: SLF001
        os.utime(path, (time.time() - 7200, time.time() - 7200))
        call_command("reconcile_evidence", dry_run=True)
        assert get_storage().exists(orphan)
        call_command("reconcile_evidence")
        assert not get_storage().exists(orphan) and get_storage().exists(r.asset.storage_key)
        get_storage().delete(r.asset.storage_key)
        with pytest.raises(SystemExit):
            call_command("reconcile_evidence")


@pytest.mark.django_db
class TestDatabaseIntegrity:
    def test_evidence_is_never_deleted_and_facts_never_change(self, w):
        r = up(w, started(w))
        for change in (lambda: EvidenceAsset.objects.filter(pk=r.asset.pk).delete(), lambda: EvidenceAttachment.objects.filter(pk=r.attachment.pk).delete(),
                       lambda: EvidenceAsset.objects.filter(pk=r.asset.pk).update(sha256="0" * 64), lambda: EvidenceAsset.objects.filter(pk=r.asset.pk).update(size=1),
                       lambda: EvidenceAsset.objects.filter(pk=r.asset.pk).update(storage_key="ab/cd/" + "0" * 32),
                       lambda: EvidenceAttachment.objects.filter(pk=r.attachment.pk).update(item=r.attachment.execution.version.sections.first().items.exclude(pk=r.attachment.item_id).first())):
            with pytest.raises(IntegrityError), transaction.atomic():
                change()
        assert EvidenceAsset.objects.count() == 1

    def test_cross_organization_rows_are_refused_by_the_database(self, w, org_b):
        ex = started(w)
        r = up(w, ex)
        with pytest.raises(IntegrityError), transaction.atomic():
            EvidenceAttachment.objects.create(asset=r.asset, organization=org_b, target_type="checklist_response", execution=ex, item=r.attachment.item)

    def test_one_live_attachment_per_asset_and_target(self, w):
        ex = started(w)
        r = up(w, ex)
        with pytest.raises(IntegrityError), transaction.atomic():
            EvidenceAttachment.objects.create(asset=r.asset, organization=r.asset.organization, target_type="checklist_response", execution=ex, item=r.attachment.item)

    def test_a_response_target_needs_execution_and_item(self, w):
        r = up(w, started(w))
        with pytest.raises(IntegrityError), transaction.atomic():
            EvidenceAttachment.objects.create(asset=r.asset, organization=r.asset.organization, target_type="checklist_response")


@pytest.mark.django_db
class TestPhaseIntegration:
    def fill(self, w, ex):
        exec_services.save_answers(ex, actor=w["runner"], inputs=[exec_services.AnswerInput(iid(w, "OK"), "yes")])

    def test_phase_5_required_photo_blocks_submission_until_verified_evidence_exists(self, w):
        ex = started(w)
        self.fill(w, ex)
        problems = exec_services.submission_problems(exec_services.build_state(ex))
        assert problems[iid(w, PHOTO)][0]["code"] == "required"
        with pytest.raises(exec_services.ExecutionValidationError):
            exec_services.submit(ex, actor=w["runner"])
        up(w, ex)
        state = exec_services.build_state(ex)
        assert state.evaluation.items[PHOTO].answer.status == "answered"
        assert not exec_services.submission_problems(state)
        assert exec_services.submit(ex, actor=w["runner"]).execution.status == "submitted"

    def test_unverified_and_quarantined_files_do_not_count(self, w, monkeypatch):
        ex = started(w)
        self.fill(w, ex)
        monkeypatch.setattr(lifecycle, "get_scanner", lambda: ForceScanner("error"))
        up(w, ex)
        problems = exec_services.submission_problems(exec_services.build_state(ex))
        assert "waiting for the security check" in problems[iid(w, PHOTO)][0]["message"]
        monkeypatch.setattr(lifecycle, "get_scanner", lambda: ForceScanner("infected", "x"))
        up(w, ex, name="b.jpg")
        problems = exec_services.submission_problems(exec_services.build_state(ex))
        assert "quarantined" in problems[iid(w, PHOTO)][0]["message"]
        assert exec_services.compute_progress(exec_services.build_state(ex))["answered"] == 1  # only the yes/no answer counts

    def test_phase_6_rules_consume_server_verified_evidence(self, w):
        ex = started(w)
        self.fill(w, ex)
        up(w, ex)
        exec_services.save_answers(ex, actor=w["runner"], inputs=[exec_services.AnswerInput(iid(w, "TEMP"), "9.5")])
        state = exec_services.build_state(ex)
        assert state.evaluation.items["TEMP"].evidence == {"kinds": ["photo"], "min_count": 1}
        assert state.evaluation.items["TEMP"].required is False
        problems = exec_services.submission_problems(state)
        assert problems[iid(w, "TEMP")][0]["code"] == "evidence_required"  # the PHOTO file belongs to another question
        up(w, ex, code="TEMP", name="t.jpg")
        assert not exec_services.submission_problems(exec_services.build_state(ex))

    def test_evidence_is_tied_to_the_exact_execution_and_version(self, w):
        ex = started(w)
        r = up(w, ex)
        assert r.attachment.execution.version_id == w["vversion"].pk and r.attachment.item.section.version_id == w["vversion"].pk

    def test_a_newer_version_does_not_move_evidence(self, w):
        from apps.checklists import lifecycle as checklist_lifecycle

        ex = started(w)
        r = up(w, ex)
        draft = checklist_lifecycle.create_draft(w["vtemplate"], change_summary="v2")
        checklist_lifecycle.publish_version(draft, expected_hash=checklist_lifecycle.run_validation(draft).draft_hash)
        r.attachment.refresh_from_db()
        assert r.attachment.execution.version.version_number == 1

    def test_signature_evidence_counts_with_the_signers_name(self, w):
        ex = started(w)
        up(w, ex, code="SIGN", name="s.png", data=png_bytes(), caption="A. Khan")
        answer = exec_services.build_state(ex).evaluation.items["SIGN"].answer
        assert answer.status == "answered" and answer.value == {"signer_name": "A. Khan", "file_id": str(EvidenceAsset.objects.get().pk)}

    def test_the_hidden_item_retention_policy_still_applies_to_evidence(self, w):
        ex = started(w)
        up(w, ex)
        self.fill(w, ex)
        exec_services.submit(ex, actor=w["runner"])
        assert EvidenceAttachment.objects.get().detached_at is None  # evidence stays part of the submitted record
        _ = ChecklistExecution
