"""Evidence web views and REST API: upload, status, authorized download/preview, detach, scoping, headers, errors, UI panel."""

from __future__ import annotations

import uuid

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework.test import APIClient

from apps.evidence import lifecycle, services
from apps.evidence.models import EvidenceAsset, EvidenceAttachment
from apps.evidence.scanning import ScanResult
from apps.executions import services as exec_services

from evidence_support import build_evidence_world, evidence_storage, jpeg_bytes, pdf_bytes, png_bytes, vissued  # noqa: F401
from executions_support import item_id

API = "/api/v1/"


class Client(APIClient):
    def request(self, **kw):  # noqa: ANN003, ANN201
        response = super().request(**kw)
        original = getattr(response, "json", None)
        if original is None:
            return response

        def unwrapped():  # noqa: ANN202
            body = original()
            if isinstance(body, dict) and "data" in body and "error" not in body:
                if "meta" in body and isinstance(body["data"], list):
                    return {"results": body["data"], **body["meta"]}
                return body["data"]
            return body

        response.json = unwrapped
        return response


def api(user) -> Client:
    c = Client()
    if user is not None:
        c.force_authenticate(user)
    return c


@pytest.fixture
def w(org_a, org_b, make_user, assign, make_type):
    return build_evidence_world(org_a, org_b, make_user, assign, make_type)


def iid(w, code):
    return item_id(w["vversion"], code)


def started(w, who="runner", **kw):
    ex, _ = exec_services.start_or_resume(vissued(w, **kw), actor=w[who])
    return ex


def post_file(client, ex, w, code="PHOTO", name="a.jpg", data=None, **extra):
    return client.post(API + "evidence/uploads/", {"execution_id": str(ex.pk), "item_id": iid(w, code), "file": SimpleUploadedFile(name, data or jpeg_bytes()), **extra}, format="multipart")


def seed(w, ex, **kw):
    return services.upload(w["runner"], target_type="checklist_response", ref={"execution_id": str(ex.pk), "item_id": iid(w, "PHOTO")}, filename="a.jpg", data=jpeg_bytes(), **kw)


@pytest.mark.django_db
class TestUploadApi:
    def test_upload_returns_the_attachment_without_storage_paths(self, w):
        ex = started(w)
        r = post_file(api(w["runner"]), ex, w, caption="north wall")
        body = r.json()
        assert r.status_code == 201 and body["asset"]["status"] == "available" and body["asset"]["available"] is True and body["caption"] == "north wall"
        assert body["asset"]["kind"] == "photo" and body["asset"]["content_type"] == "image/jpeg" and len(body["asset"]["sha256"]) == 64
        assert "storage" not in str(body).lower() and "/" not in body["asset"]["name"] and body["duplicate"] is False

    def test_a_repeat_with_the_same_operation_id_is_200_and_creates_nothing(self, w):
        ex = started(w)
        c = api(w["runner"])
        op = str(uuid.uuid4())
        data = jpeg_bytes()
        first, second = post_file(c, ex, w, data=data, operation_id=op), post_file(c, ex, w, data=data, operation_id=op)
        assert (first.status_code, second.status_code) == (201, 200) and second.json()["duplicate"] is True and first.json()["id"] == second.json()["id"]
        assert EvidenceAsset.objects.count() == 1
        clash = post_file(c, ex, w, data=jpeg_bytes(size=(90, 90)), operation_id=op)
        assert clash.status_code == 409 and clash.json()["error"]["details"]["code"] == "operation_id_reused"

    def test_by_assignment_id_starts_the_execution_server_side(self, w):
        a = vissued(w)
        r = api(w["runner"]).post(API + "evidence/uploads/", {"assignment_id": str(a.pk), "item_id": iid(w, "PHOTO"), "file": SimpleUploadedFile("a.jpg", jpeg_bytes())}, format="multipart")
        assert r.status_code == 201 and r.json()["execution"]

    def test_validation_errors_are_structured_and_safe(self, w):
        ex = started(w)
        c = api(w["runner"])
        r = post_file(c, ex, w, name="evil.exe", data=b"MZ" + b"\0" * 50)
        assert r.status_code == 400 and r.json()["error"]["details"]["code"] == "type_not_allowed"
        assert "Traceback" not in str(r.json()) and "/" not in r.json()["error"]["details"]["file"][0]
        assert post_file(c, ex, w, name="a.jpg", data=b"<html>").json()["error"]["details"]["code"] == "content_mismatch"
        assert c.post(API + "evidence/uploads/", {"execution_id": str(ex.pk), "item_id": iid(w, "PHOTO")}, format="multipart").status_code == 400  # no file
        assert not EvidenceAsset.objects.exists()

    def test_protected_and_unknown_fields_are_rejected(self, w, org_b):
        ex = started(w)
        for field, value in (("organization", str(org_b.pk)), ("uploaded_by", str(w["admin"].pk)), ("status", "available"), ("storage_key", "ab/cd/" + "0" * 32), ("sha256", "0" * 64), ("content_type", "application/pdf")):
            r = post_file(api(w["runner"]), ex, w, **{field: value})
            assert r.status_code == 400 and field in r.json()["error"]["details"], field
        assert not EvidenceAsset.objects.exists()

    def test_target_resolution_is_server_side_and_generic(self, w):
        ex = started(w)
        for who, body in (("other", {"execution_id": str(ex.pk), "item_id": iid(w, "PHOTO")}), ("plain", {"execution_id": str(ex.pk), "item_id": iid(w, "PHOTO")}),
                          ("runner", {"execution_id": str(ex.pk), "item_id": iid(w, "OK")}), ("runner", {"execution_id": str(uuid.uuid4()), "item_id": iid(w, "PHOTO")}),
                          ("runner", {"target_type": "auth.user", "execution_id": str(ex.pk), "item_id": iid(w, "PHOTO")})):
            r = api(w[who]).post(API + "evidence/uploads/", {**body, "file": SimpleUploadedFile("a.jpg", jpeg_bytes())}, format="multipart")
            assert r.status_code == 404, (who, body)
        assert api(None).post(API + "evidence/uploads/", {"file": SimpleUploadedFile("a.jpg", jpeg_bytes())}, format="multipart").status_code in (401, 403)
        r = post_file(api(w["manager"]), ex, w)
        assert r.status_code == 403
        assert not EvidenceAsset.objects.exists()

    def test_oversized_requests_are_refused(self, w, settings):
        ex = started(w)
        settings.EVIDENCE_MAX_FILE_BYTES = 2000
        r = post_file(api(w["runner"]), ex, w, data=jpeg_bytes(size=(400, 400)))
        assert r.status_code == 400 and r.json()["error"]["details"]["code"] == "file_too_large"
        settings.EVIDENCE_MAX_REQUEST_BYTES = 10
        assert post_file(api(w["runner"]), ex, w).status_code == 400

    def test_submitted_executions_refuse_uploads(self, w):
        ex = started(w)
        seed(w, ex)
        exec_services.save_answers(ex, actor=w["runner"], inputs=[exec_services.AnswerInput(iid(w, "OK"), "yes")])
        exec_services.submit(ex, actor=w["runner"])
        r = post_file(api(w["runner"]), ex, w, name="late.jpg")
        assert r.status_code == 409 and r.json()["error"]["details"]["code"] == "already_submitted"

    def test_limits_endpoint(self, w):
        body = api(w["runner"]).get(API + "evidence/limits/").json()
        assert body["extensions"] == ["jpeg", "jpg", "pdf", "png", "webp"] and "scan_required" in body


@pytest.mark.django_db
class TestReadAndServe:
    def test_asset_status_and_detail_follow_target_visibility(self, w):
        ex = started(w)
        asset = seed(w, ex).asset
        for who, expected in (("runner", 200), ("manager", 200), ("admin", 200), ("other", 404), ("plain", 404), ("viewer", 404)):
            assert api(w[who]).get(f"{API}evidence/{asset.pk}/").status_code == expected, who
            assert api(w[who]).get(f"{API}evidence/{asset.pk}/status/").status_code == expected, who
        assert api(w["runner"]).get(f"{API}evidence/{asset.pk}/status/").json() == {"id": str(asset.pk), "status": "available", "scan_status": "clean", "available": True, "retry": False}
        assert api(w["runner"]).get(f"{API}evidence/{uuid.uuid4()}/").status_code == 404
        assert api(None).get(f"{API}evidence/{asset.pk}/").status_code in (401, 403)

    def test_download_headers_and_bytes(self, w):
        ex = started(w)
        asset = seed(w, ex).asset
        r = api(w["runner"]).get(f"{API}evidence/{asset.pk}/download/")
        assert r.status_code == 200 and r["Content-Type"] == "image/jpeg" and r["X-Content-Type-Options"] == "nosniff"
        assert r["Content-Disposition"].startswith("attachment;") and "private" in r["Cache-Control"] and "no-store" in r["Cache-Control"] and "sandbox" in r["Content-Security-Policy"]
        assert r.content == services.read_bytes(asset)

    def test_preview_is_inline_for_images_only_and_pdfs_are_always_downloads(self, w):
        ex = started(w)
        img = seed(w, ex).asset
        r = api(w["runner"]).get(f"{API}evidence/{img.pk}/preview/")
        assert r.status_code == 200 and r["Content-Disposition"].startswith("inline;") and r["X-Content-Type-Options"] == "nosniff"
        pdf = services.upload(w["runner"], target_type="checklist_response", ref={"execution_id": str(ex.pk), "item_id": iid(w, "DOC")}, filename="d.pdf", data=pdf_bytes()).asset
        assert api(w["runner"]).get(f"{API}evidence/{pdf.pk}/preview/").status_code == 404
        d = api(w["runner"]).get(f"{API}evidence/{pdf.pk}/download/")
        assert d.status_code == 200 and d["Content-Disposition"].startswith("attachment;") and d["Content-Type"] == "application/pdf"

    def test_unauthorized_and_unsafe_states_are_not_served(self, w, monkeypatch):
        ex = started(w)
        asset = seed(w, ex).asset
        for who in ("other", "plain", "viewer"):
            assert api(w[who]).get(f"{API}evidence/{asset.pk}/download/").status_code == 404
        monkeypatch.setattr(lifecycle, "get_scanner", lambda: type("S", (), {"scan": lambda self, d: ScanResult("infected", "x")})())
        bad = services.upload(w["runner"], target_type="checklist_response", ref={"execution_id": str(ex.pk), "item_id": iid(w, "PHOTO")}, filename="b.jpg", data=jpeg_bytes(size=(70, 70))).asset
        assert api(w["runner"]).get(f"{API}evidence/{bad.pk}/download/").status_code == 404 and api(w["admin"]).get(f"{API}evidence/{bad.pk}/download/").status_code == 404
        monkeypatch.setattr(lifecycle, "get_scanner", lambda: type("S", (), {"scan": lambda self, d: ScanResult("error", "down")})())
        pending = services.upload(w["runner"], target_type="checklist_response", ref={"execution_id": str(ex.pk), "item_id": iid(w, "DOC")}, filename="p.png", data=png_bytes()).asset
        assert pending.status == "pending_scan" and api(w["runner"]).get(f"{API}evidence/{pending.pk}/download/").status_code == 404

    def test_the_policy_can_allow_viewing_pending_files(self, w, settings, monkeypatch):
        monkeypatch.setattr(lifecycle, "get_scanner", lambda: type("S", (), {"scan": lambda self, d: ScanResult("error", "down")})())
        ex = started(w)
        pending = seed(w, ex).asset
        settings.EVIDENCE_ALLOW_VIEW_PENDING = True
        assert api(w["runner"]).get(f"{API}evidence/{pending.pk}/download/").status_code == 200

    def test_a_revoked_user_loses_download_immediately(self, w):
        from apps.permissions.authorization import invalidate_all
        from apps.permissions.models import MembershipRole

        ex = started(w)
        asset = seed(w, ex).asset
        assert api(w["runner"]).get(f"{API}evidence/{asset.pk}/download/").status_code == 200
        MembershipRole.objects.filter(membership__user=w["runner"]).delete()
        invalidate_all()
        assert api(w["runner"]).get(f"{API}evidence/{asset.pk}/download/").status_code == 404

    def test_access_is_audited(self, w):
        from apps.core.models import ActivityLog

        ex = started(w)
        asset = seed(w, ex).asset
        api(w["runner"]).get(f"{API}evidence/{asset.pk}/download/")
        api(w["runner"]).get(f"{API}evidence/{asset.pk}/preview/")
        assert ActivityLog.objects.filter(verb="evidence.downloaded").count() == 1 and ActivityLog.objects.filter(verb="evidence.previewed").count() == 1


@pytest.mark.django_db
class TestAttachmentApi:
    def test_list_pagination_and_target_listing(self, w):
        ex = started(w)
        seed(w, ex)
        services.upload(w["runner"], target_type="checklist_response", ref={"execution_id": str(ex.pk), "item_id": iid(w, "PHOTO")}, filename="b.jpg", data=jpeg_bytes(size=(70, 70)))
        c = api(w["runner"])
        page = c.get(f"{API}attachments/", {"execution": str(ex.pk), "page_size": 1}).json()
        assert page["count"] == 2 and len(page["results"]) == 1
        assert len(c.get(f"{API}attachments/", {"execution": str(ex.pk), "item": iid(w, "DOC")}).json()["results"]) == 0
        assert c.get(f"{API}attachments/").status_code == 400
        assert c.get(f"{API}targets/checklist_response/{ex.pk}/attachments/").json()["count"] == 2
        assert c.get(f"{API}targets/finding/{ex.pk}/attachments/").status_code == 404
        for who in ("other", "plain"):
            assert api(w[who]).get(f"{API}attachments/", {"execution": str(ex.pk)}).json()["count"] == 0
            assert api(w[who]).get(f"{API}targets/checklist_response/{ex.pk}/attachments/").json()["count"] == 0

    def test_retrieve_detach_and_history(self, w):
        ex = started(w)
        att = seed(w, ex).attachment
        c = api(w["runner"])
        assert c.get(f"{API}attachments/{att.pk}/").json()["asset"]["name"] == "a.jpg"
        assert api(w["manager"]).delete(f"{API}attachments/{att.pk}/").status_code == 403
        assert api(w["other"]).delete(f"{API}attachments/{att.pk}/").status_code == 404
        assert c.delete(f"{API}attachments/{att.pk}/", {"reason": "wrong"}).status_code == 204
        assert c.delete(f"{API}attachments/{att.pk}/").status_code == 204  # idempotent
        assert EvidenceAttachment.objects.get().detached_at and EvidenceAsset.objects.count() == 1
        assert c.get(f"{API}attachments/", {"execution": str(ex.pk)}).json()["count"] == 0
        assert c.get(f"{API}attachments/", {"execution": str(ex.pk), "include_detached": "1"}).json()["count"] == 1
        assert c.get(f"{API}attachments/{att.pk}/").json()["detached_at"]

    def test_attaching_an_existing_file(self, w):
        ex = started(w)
        r = seed(w, ex)
        c = api(w["runner"])
        body = {"asset_id": str(r.asset.pk), "execution_id": str(ex.pk), "item_id": iid(w, "DOC")}
        ok = c.post(API + "attachments/", body, format="json")
        assert ok.status_code == 201 and ok.json()["asset"]["id"] == str(r.asset.pk) and EvidenceAsset.objects.count() == 1
        assert c.post(API + "attachments/", body, format="json").status_code == 409
        assert api(w["other"]).post(API + "attachments/", body, format="json").status_code == 404
        assert c.post(API + "attachments/", {**body, "organization": "x"}, format="json").status_code == 400
        assert c.post(API + "attachments/", {**body, "target_type": "auth.user"}, format="json").status_code in (404, 400)


@pytest.mark.django_db
class TestWebUi:
    def test_the_workspace_shows_the_evidence_panel_with_accessible_controls(self, client, w):
        ex = started(w)
        client.force_login(w["runner"])
        html = client.get(reverse("executions:workspace", args=[ex.pk])).content.decode()
        for needle in ("Photo of the area", "Evidence files", "No files attached yet.", "At least 1 verified file required", "Add a file", 'type="file"', 'capture="environment"',
                       'aria-describedby="ev-hint-', f'id="ev-form-{iid(w, "PHOTO")}"', 'enctype="multipart/form-data"', "JPG, JPEG, PNG, WEBP", "up to 10 MB"):
            assert needle in html, needle
        assert html.count("<form") == html.count("</form>")  # forms are never nested: the upload forms sit beside the autosave form

    def test_upload_via_htmx_returns_the_panel_with_status(self, client, w):
        ex = started(w)
        client.force_login(w["runner"])
        r = client.post(reverse("evidence:upload"), {"execution": str(ex.pk), "item": iid(w, "PHOTO"), "file": SimpleUploadedFile("site.jpg", jpeg_bytes())}, headers={"HX-Request": "true"})
        html = r.content.decode()
        assert r.status_code == 200 and "<html" not in html and "site.jpg" in html and "Available and verified" in html and "Download" in html and "Remove" in html
        assert 'alt="Preview of site.jpg"' in html and "At least 1 verified file required" in html and "1 verified so far" in html

    def test_upload_errors_are_shown_in_the_panel(self, client, w):
        ex = started(w)
        client.force_login(w["runner"])
        r = client.post(reverse("evidence:upload"), {"execution": str(ex.pk), "item": iid(w, "PHOTO"), "file": SimpleUploadedFile("evil.exe", b"MZ" + b"\0" * 40)}, headers={"HX-Request": "true"})
        assert "This file type is not allowed" in r.content.decode() and not EvidenceAsset.objects.exists()
        assert "Choose a file" in client.post(reverse("evidence:upload"), {"execution": str(ex.pk), "item": iid(w, "PHOTO")}, headers={"HX-Request": "true"}).content.decode()

    def test_plain_form_posts_fall_back_to_a_redirect_with_a_message(self, client, w):
        ex = started(w)
        client.force_login(w["runner"])
        r = client.post(reverse("evidence:upload"), {"execution": str(ex.pk), "item": iid(w, "PHOTO"), "file": SimpleUploadedFile("a.jpg", jpeg_bytes())})
        assert r.status_code == 302 and r["Location"] == reverse("executions:workspace", args=[ex.pk]) and EvidenceAsset.objects.count() == 1

    def test_pending_files_are_labelled_and_not_downloadable(self, client, w, monkeypatch):
        monkeypatch.setattr(lifecycle, "get_scanner", lambda: type("S", (), {"scan": lambda self, d: ScanResult("error", "down")})())
        ex = started(w)
        seed(w, ex)
        client.force_login(w["runner"])
        html = client.get(reverse("executions:workspace", args=[ex.pk])).content.decode()
        assert "Security check pending" in html and "waiting for the security check" in html and "/download/" not in html and "Preview of" not in html

    def test_download_and_detach_views(self, client, w):
        ex = started(w)
        att = seed(w, ex).attachment
        client.force_login(w["runner"])
        d = client.get(reverse("evidence:download", args=[att.pk]))
        assert d.status_code == 200 and d["X-Content-Type-Options"] == "nosniff" and d["Content-Disposition"].startswith("attachment")
        assert client.get(reverse("evidence:preview", args=[att.pk]))["Content-Disposition"].startswith("inline")
        r = client.post(reverse("evidence:detach", args=[att.pk]), headers={"HX-Request": "true"})
        assert r.status_code == 200 and "Remove" not in r.content.decode().split("No files attached")[0] and EvidenceAttachment.objects.get().detached_at
        assert client.get(reverse("evidence:download", args=[att.pk])).status_code == 200  # history stays viewable to those who may see the target

    def test_access_control_on_the_views(self, client, w):
        ex = started(w)
        att = seed(w, ex).attachment
        client.force_login(w["other"])
        assert client.get(reverse("evidence:download", args=[att.pk])).status_code == 404
        assert client.post(reverse("evidence:detach", args=[att.pk])).status_code == 404
        assert client.post(reverse("evidence:upload"), {"execution": str(ex.pk), "item": iid(w, "PHOTO"), "file": SimpleUploadedFile("a.jpg", jpeg_bytes())}).status_code == 404
        client.force_login(w["manager"])
        assert client.post(reverse("evidence:detach", args=[att.pk])).status_code == 403
        client.logout()
        assert client.get(reverse("evidence:download", args=[att.pk])).status_code == 302
        assert client.get(reverse("evidence:upload")).status_code in (302, 405)

    def test_csrf_is_enforced(self, w):
        from django.test import Client

        ex = started(w)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(w["runner"])
        assert strict.post(reverse("evidence:upload"), {"execution": str(ex.pk), "item": iid(w, "PHOTO"), "file": SimpleUploadedFile("a.jpg", jpeg_bytes())}).status_code == 403
        assert not EvidenceAsset.objects.exists()

    def test_the_detail_page_lists_evidence_read_only(self, client, w):
        ex = started(w)
        seed(w, ex)
        exec_services.save_answers(ex, actor=w["runner"], inputs=[exec_services.AnswerInput(iid(w, "OK"), "yes")])
        exec_services.submit(ex, actor=w["runner"])
        client.force_login(w["runner"])
        html = client.get(reverse("executions:detail", args=[ex.pk])).content.decode()
        assert "a.jpg" in html and "Available and verified" in html and "Download" in html and "Remove" not in html and 'type="file"' not in html

    def test_the_review_page_explains_missing_evidence(self, client, w):
        ex = started(w)
        client.force_login(w["runner"])
        html = client.get(reverse("executions:review", args=[ex.pk])).content.decode()
        assert "Add at least 1 photo" in html and "verified evidence" in html

    def test_the_admin_is_read_only_and_hides_storage_keys(self, client, w, django_user_model):
        ex = started(w)
        r = seed(w, ex)
        su = django_user_model.objects.create_superuser(email="su-ev@example.com", password="x-Strong-pass-123")
        client.force_login(su)
        page = client.get(reverse("admin:evidence_evidenceasset_change", args=[r.asset.pk])).content.decode()
        assert r.asset.storage_key not in page and 'name="_save"' not in page and "a.jpg" in page
        assert client.get(reverse("admin:evidence_evidenceasset_changelist")).status_code == 200
        assert client.get(reverse("admin:evidence_evidenceasset_add")).status_code == 403
        assert client.post(reverse("admin:evidence_evidenceasset_delete", args=[r.asset.pk]), {"post": "yes"}).status_code == 403
        assert client.post(reverse("admin:evidence_evidenceasset_change", args=[r.asset.pk]), {"status": "available", "scan_status": "clean"}).status_code in (302, 403)
        assert client.get(reverse("admin:evidence_evidenceattachment_changelist")).status_code == 200
