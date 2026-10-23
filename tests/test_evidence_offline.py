"""Phase 10 x Phase 11: offline evidence on the server side - package capability, idempotent uploads, checksum verification, submit ordering."""

from __future__ import annotations

import hashlib
import uuid

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from apps.evidence import lifecycle
from apps.evidence.models import EvidenceAsset, EvidenceAttachment
from apps.evidence.scanning import ScanResult
from apps.executions import services as exec_services

from evidence_support import build_evidence_world, evidence_storage, jpeg_bytes, vissued  # noqa: F401
from executions_support import item_id
from test_evidence_web_api import Client, api  # noqa: F401

BASE = "/api/v1/"


@pytest.fixture
def w(org_a, org_b, make_user, assign, make_type):
    return build_evidence_world(org_a, org_b, make_user, assign, make_type)


def iid(w, code):
    return item_id(w["vversion"], code)


def upload(client, w, assignment, code="PHOTO", data=None, op=None, sha=None, **extra):
    data = data or jpeg_bytes()
    body = {"assignment_id": str(assignment.pk), "item_id": iid(w, code), "file": SimpleUploadedFile("a.jpg", data), **extra}
    if op:
        body["operation_id"] = op
    if sha is not False:
        body["client_sha256"] = sha or hashlib.sha256(data).hexdigest()
    return client.post(BASE + "evidence/uploads/", body, format="multipart")


def sync(client, *ops):
    return client.post(BASE + "offline/sync/", {"schema_version": 1, "operations": list(ops)}, format="json").json()["results"]


def op(w, assignment, code=None, value=None, type="upsert_response", seq=0, base=0):
    out = {"operation_id": str(uuid.uuid4()), "type": type, "seq": seq, "assignment_id": str(assignment.pk), "version_id": str(w["vversion"].pk), "schema_version": 1}
    if type != "submit":
        out.update(item_id=iid(w, code), value=value, base_revision=base, not_applicable=False)
    return out


@pytest.mark.django_db
class TestCapabilityAndProtocol:
    def test_evidence_checklists_are_now_offline_capable(self, w):
        a = vissued(w)
        c = api(w["runner"])
        listing = {r["id"]: r for r in c.get(BASE + "offline/assignments/").json()["results"]}
        assert listing[str(a.pk)]["offline"] == {"capable": True, "reasons": []}
        package = c.get(f"{BASE}offline/assignments/{a.pk}/package/").json()
        photo = next(i for i in package["items"] if i["code"] == "PHOTO")
        assert photo["answer_type"] == "photo" and photo["config"]["max_files"] == 2 and package["evaluator_version"] == 3
        assert package["rules"][0]["actions"][0]["type"] == "REQUIRE_EVIDENCE"

    def test_the_manifest_publishes_the_upload_contract(self, w):
        data = api(w["runner"]).get(BASE + "offline/manifest/").json()
        assert data["evidence"] == "supported" and data["evidence_limits"]["extensions"] == ["jpeg", "jpg", "pdf", "png", "webp"] and data["evaluator_version"] == 3

    def test_media_answers_still_cannot_be_synced_as_values(self, w):
        a = vissued(w)
        out = sync(api(w["runner"]), op(w, a, "PHOTO", [{"file_id": str(uuid.uuid4()), "name": "a.jpg", "size": 5, "content_type": "image/jpeg"}]))[0]
        assert out["status"] == "rejected" and out["errors"][0]["code"] == "use_evidence_upload"


@pytest.mark.django_db
class TestOfflineUploads:
    def test_an_upload_by_assignment_starts_the_execution_and_is_idempotent(self, w):
        a = vissued(w)
        c = api(w["runner"])
        operation = str(uuid.uuid4())
        data = jpeg_bytes()
        first, again = upload(c, w, a, data=data, op=operation), upload(c, w, a, data=data, op=operation)
        assert (first.status_code, again.status_code) == (201, 200) and again.json()["duplicate"] is True and first.json()["execution"]
        assert EvidenceAsset.objects.count() == 1 and EvidenceAttachment.objects.count() == 1
        a.refresh_from_db()
        assert a.status == "in_progress"

    def test_the_checksum_of_the_received_bytes_is_verified(self, w):
        a = vissued(w)
        c = api(w["runner"])
        data = jpeg_bytes()
        r = upload(c, w, a, data=data[: len(data) // 2], sha=hashlib.sha256(data).hexdigest())  # a truncated transfer
        assert r.status_code == 400 and r.json()["error"]["details"]["code"] == "checksum_mismatch"
        assert not EvidenceAsset.objects.exists()
        assert upload(c, w, a, data=data, sha="not-hex").status_code == 400
        assert upload(c, w, a, data=data, sha=False).status_code == 201  # the hint is optional

    def test_tampered_identifiers_cannot_reach_another_execution_or_organization(self, w):
        a, foreign_scope = vissued(w), vissued(w, w["r3"])
        assert upload(api(w["runner"]), w, foreign_scope).status_code == 404
        assert upload(api(w["other"]), w, a).status_code == 404
        assert upload(api(w["plain"]), w, a).status_code == 404
        ex, _ = exec_services.start_or_resume(a, actor=w["runner"])
        r = api(w["other"]).post(BASE + "evidence/uploads/", {"execution_id": str(ex.pk), "item_id": iid(w, "PHOTO"), "file": SimpleUploadedFile("a.jpg", jpeg_bytes())}, format="multipart")
        assert r.status_code == 404
        wrong_item = api(w["runner"]).post(BASE + "evidence/uploads/", {"assignment_id": str(a.pk), "item_id": item_id(w["eversion"], "FIRE-EXIT"), "file": SimpleUploadedFile("a.jpg", jpeg_bytes())},
                                           format="multipart")
        assert wrong_item.status_code == 404
        assert not EvidenceAsset.objects.exists()

    def test_revoked_permissions_are_honoured_at_upload_time(self, w):
        from apps.permissions.authorization import invalidate_all
        from apps.permissions.models import MembershipRole

        a = vissued(w)
        MembershipRole.objects.filter(membership__user=w["runner"]).delete()
        invalidate_all()
        assert upload(api(w["runner"]), w, a).status_code in (403, 404)
        assert not EvidenceAsset.objects.exists()

    def test_a_submitted_execution_refuses_late_offline_uploads(self, w):
        a = vissued(w)
        c = api(w["runner"])
        upload(c, w, a)
        results = sync(c, op(w, a, "OK", "yes", seq=1), op(w, a, type="submit", seq=2))
        assert [r["status"] for r in results] == ["applied", "applied"]
        late = upload(c, w, a, data=jpeg_bytes(size=(90, 90)))
        assert late.status_code == 409 and late.json()["error"]["details"]["code"] == "already_submitted"

    def test_client_timestamps_are_only_diagnostic(self, w):
        a = vissued(w)
        r = upload(api(w["runner"]), w, a, captured_at="1999-01-01T00:00:00Z")
        asset = EvidenceAsset.objects.get()
        assert r.status_code == 201 and asset.captured_at.year == 1999 and asset.created_at.year >= 2026  # recorded, never authoritative


@pytest.mark.django_db
class TestSubmitOrdering:
    def test_submit_is_refused_until_the_evidence_is_verified_then_succeeds(self, w, monkeypatch):
        a = vissued(w)
        c = api(w["runner"])
        monkeypatch.setattr(lifecycle, "get_scanner", lambda: type("S", (), {"scan": lambda self, d: ScanResult("error", "down")})())
        upload(c, w, a)  # received, security check pending
        asset = EvidenceAsset.objects.get()
        assert asset.status == "pending_scan"
        status = c.get(f"{BASE}evidence/{asset.pk}/status/").json()
        assert status["status"] == "pending_scan" and status["available"] is False and status["retry"] is True
        out = sync(c, op(w, a, "OK", "yes", seq=1), op(w, a, type="submit", seq=2))
        assert out[0]["status"] == "applied" and out[1]["status"] == "rejected" and out[1]["code"] == "validation_failed"
        assert "waiting for the security check" in str(out[1]["problems"])
        monkeypatch.setattr(lifecycle, "get_scanner", lambda: type("S", (), {"scan": lambda self, d: ScanResult("clean")})())
        lifecycle.scan_asset(asset)
        assert c.get(f"{BASE}evidence/{asset.pk}/status/").json()["available"] is True
        done = sync(c, op(w, a, type="submit", seq=3))[0]
        assert done["status"] == "applied" and done["execution_status"] == "submitted"

    def test_a_missing_required_photo_blocks_the_offline_submit(self, w):
        a = vissued(w)
        c = api(w["runner"])
        out = sync(c, op(w, a, "OK", "yes", seq=1), op(w, a, type="submit", seq=2))
        assert out[1]["status"] == "rejected" and "photo" in str(out[1]["problems"]).lower()

    def test_a_quarantined_file_never_satisfies_the_requirement(self, w, monkeypatch):
        a = vissued(w)
        c = api(w["runner"])
        monkeypatch.setattr(lifecycle, "get_scanner", lambda: type("S", (), {"scan": lambda self, d: ScanResult("infected", "x")})())
        upload(c, w, a)
        out = sync(c, op(w, a, "OK", "yes", seq=1), op(w, a, type="submit", seq=2))
        assert out[1]["status"] == "rejected" and "quarantined" in str(out[1]["problems"])
        assert c.get(f"{BASE}evidence/{EvidenceAsset.objects.get().pk}/status/").json()["status"] == "quarantined"


_ = APIClient
