"""Settings foundation assertions."""

from __future__ import annotations

from django.conf import settings


def test_core_security_defaults():
    assert settings.SECURE_CONTENT_TYPE_NOSNIFF is True
    assert settings.SESSION_COOKIE_HTTPONLY is True
    assert settings.X_FRAME_OPTIONS == "DENY"
    assert settings.SECURE_REFERRER_POLICY == "same-origin"


def test_csp_policy_is_complete():
    csp = settings.BASE_CSP
    for directive in ("default-src", "script-src", "style-src", "object-src", "base-uri"):
        assert directive in csp
    assert "'self'" in csp["default-src"]
    assert "'none'" in csp["object-src"]


def test_csp_is_enforced_in_tests():
    assert settings.SECURE_CSP == settings.BASE_CSP


def test_drf_response_contract_is_configured():
    rest = settings.REST_FRAMEWORK
    assert rest["EXCEPTION_HANDLER"] == "apps.api.exceptions.api_exception_handler"
    assert "apps.api.renderers.EnvelopeJSONRenderer" in rest["DEFAULT_RENDERER_CLASSES"]


def test_tasks_use_immediate_backend_in_tests():
    assert settings.TASKS["default"]["BACKEND"].endswith("ImmediateBackend")


def test_mailer_uses_memory_backend_in_tests():
    assert (
        settings.MAILERS["default"]["BACKEND"]
        == "django.core.mail.backends.locmem.EmailBackend"
    )


def test_tables2_template_is_custom():
    assert settings.DJANGO_TABLES2_TEMPLATE == "core/tables/table.html"


def test_crispy_uses_tailwind_pack():
    assert settings.CRISPY_TEMPLATE_PACK == "tailwind"


def test_notification_provider_seam_configured():
    assert settings.NOTIFICATION_EMAIL_PROVIDER.endswith("DjangoEmailProvider")


def test_guardian_anonymous_user_disabled():
    assert settings.ANONYMOUS_USER_NAME is None
    assert "guardian.backends.ObjectPermissionBackend" in settings.AUTHENTICATION_BACKENDS
