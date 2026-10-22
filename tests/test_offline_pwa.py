"""Phase 10 PWA foundation: the public offline shell, the service-worker cache policy, identity hooks, and the JavaScript test suite."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
from django.contrib.staticfiles import finders
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from apps.executions.models import SyncReceipt

from offline_support import build_offline_world, oissued, op

ROOT = Path(__file__).resolve().parent.parent
SW = (ROOT / "static" / "js" / "service-worker.js").read_text(encoding="utf-8")
OFFLINE_JS = ROOT / "static" / "js" / "offline"


def precache_urls() -> list[str]:
    block = re.search(r"const PRECACHE_URLS = \[(.*?)\];", SW, re.S).group(1)
    return re.findall(r'"(/[^"]+)"', block)


class TestShellPage:
    def test_the_shell_is_public_and_carries_no_user_data_or_tokens(self, client, django_user_model, db):
        anonymous = client.get(reverse("core:offline-app"))
        html = anonymous.content.decode()
        assert anonymous.status_code == 200 and anonymous["Cache-Control"] == "no-cache"
        assert 'id="offline-app"' in html and 'type="module"' in html and "/static/js/offline/offline-app.js" in html
        user = django_user_model.objects.create_user(email="shell-user@example.com", password="x-Strong-pass-123")
        client.force_login(user)
        signed_in = client.get(reverse("core:offline-app")).content.decode()
        for page in (html, signed_in):
            assert "csrf" not in page.lower() and "shell-user@example.com" not in page and "data-account-user" not in page
            assert "<script>" not in page and "onclick=" not in page  # strict CSP: no inline script or handlers

    def test_the_shell_links_the_manifest_and_theme(self, client, db):
        html = client.get(reverse("core:offline-app")).content.decode()
        assert reverse("core:manifest") in html and "/themes/tokens.css" in html and "theme-color" in html

    def test_authenticated_pages_record_who_is_signed_in_and_anonymous_ones_do_not(self, client, db, django_user_model, org_a, make_user, assign):
        user = make_user(email="hint@example.com", organization=org_a, role=None)
        assign(user, org_a, "administrator")
        client.force_login(user)
        html = client.get("/executions/").content.decode()
        assert f'data-account-user="{user.pk}"' in html and f'data-account-org="{org_a.pk}"' in html
        client.logout()
        assert "data-account-user" not in client.get(reverse("core:offline-app")).content.decode()


class TestManifest:
    def test_manifest_is_complete_and_its_icons_resolve(self, client, db):
        data = json.loads(client.get(reverse("core:manifest")).content)
        assert data["start_url"] == "/" and data["scope"] == "/" and data["display"] == "standalone"
        assert data["theme_color"] and data["background_color"] and data["short_name"] and data["name"]
        for icon in data["icons"]:
            relative = icon["src"].removeprefix("/static/")
            assert finders.find(relative), f"icon does not resolve through staticfiles: {icon['src']}"
        assert "McDonald" not in json.dumps(data)


class TestServiceWorkerPolicy:
    def test_the_worker_is_served_from_the_root_with_a_new_cache_version(self, client, db):
        response = client.get("/service-worker.js")
        assert response["Service-Worker-Allowed"] == "/" and response["Cache-Control"] == "no-cache"
        assert 'CACHE_VERSION = "cp-static-v6"' in response.content.decode()

    def test_every_offline_module_is_precached_and_exists(self):
        urls = precache_urls()
        modules = sorted(p.name for p in OFFLINE_JS.glob("*.js"))
        assert modules, "no offline modules found"
        for name in modules:
            assert f"/static/js/offline/{name}" in urls, f"{name} is not precached: the shell would not open offline"
        for url in urls:
            if url.startswith("/static/"):
                assert finders.find(url.removeprefix("/static/")), f"precached asset does not exist: {url}"
        block = re.search(r"const PRECACHE_URLS = \[(.*?)\];", SW, re.S).group(1)
        assert "OFFLINE_APP_URL" in block and "OFFLINE_URL" in block and 'OFFLINE_APP_URL = "/offline/app/"' in SW

    def test_sensitive_routes_are_never_intercepted(self):
        for prefix in ("/auth/", "/api/", "/admin/", "/organizations/switch/"):
            assert f'"{prefix}"' in SW
        assert re.search(r"if \(isBypassed\(url\.pathname\)\) \{\s*return;", SW)

    def test_authenticated_pages_are_never_written_to_a_cache(self):
        navigation = SW[SW.index("async function navigation"):SW.index("// Static assets are fetched")]
        puts = re.findall(r"cache\.put\(([^,]+),", navigation)
        assert puts == ["url.pathname"], puts  # one put, and only behind the public-page guard
        guard = navigation[: navigation.index("cache.put")]
        assert "OFFLINE_APP_URL" in guard and "OFFLINE_URL" in guard and "isCacheable(response)" in guard

    def test_no_store_and_private_responses_are_not_cached(self):
        assert "no-store" in SW and "private" in SW and 'response.type !== "basic"' in SW
        assert "networkFirstAsset" in SW and "isCacheable(response)" in SW

    def test_there_is_no_catch_all_caching_of_api_or_cross_origin_requests(self):
        assert "url.origin !== self.location.origin" in SW
        assert "cache.addAll(" in SW and SW.count("cache.put(") == 2  # navigation (guarded) + static assets
        assert "fetch(request).then" not in SW  # no generic pass-through cache wrapper

    def test_old_application_caches_are_removed_on_activation_and_only_ours(self):
        assert 'CACHE_PREFIX = "cp-static-"' in SW and "key.indexOf(CACHE_PREFIX) === 0 && key !== CACHE_VERSION" in SW

    def test_background_sync_only_wakes_a_page(self):
        sync = SW[SW.index('addEventListener("sync"'):]
        assert 'type: "sync-requested"' in sync and "indexedDB" not in SW and "/api/" not in sync


def strip_comments(code: str) -> str:
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    return re.sub(r"(^|\s)//[^\n]*", "", code)


class TestClientCodeHygiene:
    def test_no_dynamic_html_or_code_execution_in_the_offline_client(self):
        for path in OFFLINE_JS.glob("*.js"):
            code = strip_comments(path.read_text(encoding="utf-8"))
            for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
                assert banned not in code, f"{path.name} uses {banned}"

    def test_local_storage_holds_only_the_account_hint_flags_and_retention(self):
        keys = set()
        for path in OFFLINE_JS.glob("*.js"):
            keys |= set(re.findall(r'(?:localStorage|storage)\.(?:setItem|getItem)\(\s*([A-Z_]+|"[^"]+")', path.read_text(encoding="utf-8")))
        assert keys <= {"ACCOUNT_KEY", "ENDED_KEY", "RETENTION_KEY", '"cp-offline-account"', '"cp-offline-session-ended"'}, keys

    def test_secrets_are_never_stored_or_logged(self):
        text = "\n".join(strip_comments(p.read_text(encoding="utf-8")) for p in OFFLINE_JS.glob("*.js"))
        assert "console.log" not in text and 'type: "password"' not in text and "refresh_token" not in text and "access_token" not in text
        assert "csrfToken" in text and "setItem(\"csrf" not in text  # the CSRF token lives in memory only


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_the_javascript_suite_passes():
    result = subprocess.run(["node", "--test", "--test-reporter=tap", *[str(p) for p in sorted((OFFLINE_JS / "tests").glob("*.test.js"))]],
                            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-1000:]
    assert re.search(r"^# fail 0$", result.stdout, re.M)
    assert int(re.search(r"^# tests (\d+)$", result.stdout, re.M).group(1)) >= 70


@pytest.mark.django_db
class TestSessionEndpointAndOperations:
    def test_session_asserts_identity_and_is_never_cacheable(self, org_a, org_b, make_user, assign, make_type):
        from rest_framework.test import APIClient

        w = build_offline_world(org_a, org_b, make_user, assign, make_type)
        client = APIClient()
        assert client.get("/api/v1/offline/session/").status_code in (401, 403)
        client.force_authenticate(w["runner"])
        r = client.get("/api/v1/offline/session/")
        data = r.json()["data"]
        assert r.status_code == 200 and r["Cache-Control"] == "no-store" and data["user_id"] == str(w["runner"].pk) and data["csrf_token"]
        assert set(data) == {"user_id", "organization_id", "organization_name", "csrf_token"}

    def test_receipts_are_pruned_by_age_and_the_admin_is_read_only(self, org_a, org_b, make_user, assign, make_type, client, django_user_model):
        from rest_framework.test import APIClient

        w = build_offline_world(org_a, org_b, make_user, assign, make_type)
        a = oissued(w)
        api = APIClient()
        api.force_authenticate(w["runner"])
        api.post("/api/v1/offline/sync/", {"operations": [op(w, a, "FRIDGE", "4")]}, format="json")
        receipt = SyncReceipt.objects.get()
        SyncReceipt.objects.filter(pk=receipt.pk).update(created_at=timezone.now() - timedelta(days=120))
        call_command("prune_sync_receipts", days=90, dry_run=True)
        assert SyncReceipt.objects.count() == 1
        call_command("prune_sync_receipts", days=90)
        assert SyncReceipt.objects.count() == 0
        su = django_user_model.objects.create_superuser(email="su-offline@example.com", password="x-Strong-pass-123")
        client.force_login(su)
        api.post("/api/v1/offline/sync/", {"operations": [op(w, a, "FRIDGE", "5")]}, format="json")
        r = SyncReceipt.objects.get()
        assert client.get(reverse("admin:executions_syncreceipt_changelist")).status_code == 200
        assert client.get(reverse("admin:executions_syncreceipt_add")).status_code == 403
        assert client.post(reverse("admin:executions_syncreceipt_delete", args=[r.pk]), {"post": "yes"}).status_code == 403

    def test_demo_command_prepares_an_offline_capable_assignment(self, org_a, org_b, make_user, assign, make_type):
        from apps.assignments.models import ChecklistAssignment
        from assignments_support import build_world
        from rest_framework.test import APIClient

        build_world(org_a, org_b, make_user, assign, make_type)
        call_command("seed_demo_offline", organization=org_a.slug)
        a = ChecklistAssignment.objects.get(location__code="DEMO-R-3", organization=org_a)
        call_command("seed_demo_offline", organization=org_a.slug)  # idempotent
        assert ChecklistAssignment.objects.filter(location__code="DEMO-R-3", organization=org_a).count() == 1
        from apps.permissions.authorization import has_permission  # noqa: F401

        admin = a.created_by
        client = APIClient()
        client.force_authenticate(admin)
        r = client.get(f"/api/v1/offline/assignments/{a.pk}/package/")
        assert r.status_code == 200, r.json()
