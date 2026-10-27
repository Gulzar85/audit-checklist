"""Base settings shared by every environment.

Nothing in this file contains a secret.  All sensitive or environment-specific
values are read from environment variables (see ``.env.example``) via
:mod:`config.settings.env`.
"""

from __future__ import annotations

from pathlib import Path

from django.utils.csp import CSP

from .env import (
    database_from_url,
    env,
    env_bool,
    env_int,
    env_list,
    load_dotenv,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Seed the process environment from a local .env file (never committed).
load_dotenv(BASE_DIR / ".env")

APPS_DIR = BASE_DIR / "apps"

# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
SECRET_KEY = env("SECRET_KEY", required=True)
DEBUG = env_bool("DJANGO_DEBUG", False)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", ["localhost", "127.0.0.1"])
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS", [])
SITE_ID = 1

# Platform identity (overridable per deployment).
APP_NAME = env("APP_NAME", "Checklist Platform")
APP_VERSION = env("APP_VERSION", "0.1.0")
ENVIRONMENT = env("DJANGO_ENVIRONMENT", "development")

# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------
DJANGO_APPS = [
    # Jazzmin must precede django.contrib.admin.
    "jazzmin",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    # Lets custom form widgets live in the project's templates/ directory
    # (see FORM_RENDERER below).
    "django.forms",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "drf_spectacular",
    "django_filters",
    "django_tables2",
    "import_export",
    "guardian",
    "simple_history",
    "crispy_forms",
    "crispy_tailwind",
]

LOCAL_APPS = [
    "apps.core",
    "apps.organizations",
    "apps.locations",
    "apps.themes",
    "apps.accounts",
    "apps.permissions",
    "apps.checklists",
    "apps.assignments",
    "apps.executions",
    "apps.evidence",
    "apps.findings",
    "apps.corrective_actions",
    "apps.workflows",
    "apps.scoring",
    "apps.evaluation",
    "apps.notifications",
    "apps.jobs",
    "apps.reporting",
    "apps.api",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
    # Must come after the CSP middleware: responses unwind in reverse order, so this
    # attaches the admin policy before Django turns it into a header.
    "apps.core.middleware.AdminContentSecurityPolicyMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "simple_history.middleware.HistoryRequestMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    # Enforces the user lifecycle on every request (force sign-out when an
    # account is suspended/deactivated mid-session).  After auth + messages.
    "apps.accounts.middleware.IdentityMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.template.context_processors.i18n",
                "django.template.context_processors.static",
                "django.template.context_processors.media",
                "django.template.context_processors.csp",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.site_context",
                "apps.core.context_processors.shell_context",
                "apps.themes.context_processors.theme_context",
                "apps.accounts.context_processors.identity_context",
            ],
        },
    },
]

# ---------------------------------------------------------------------------
# Database (PostgreSQL-first, environment driven)
# ---------------------------------------------------------------------------
DATABASES = {
    "default": database_from_url(
        env("DATABASE_URL"),
        conn_max_age=env_int("DB_CONN_MAX_AGE", 0),
        conn_health_checks=env_bool("DB_CONN_HEALTH_CHECKS", True),
        ssl_require=env_bool("DB_SSL_REQUIRE", False),
    )
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Authentication / authorization
# ---------------------------------------------------------------------------
AUTH_USER_MODEL = "accounts.User"

AUTHENTICATION_BACKENDS = [
    # Microsoft Entra ID (MSAL authorization-code flow) - the primary provider.
    "apps.accounts.backends.EntraOIDCBackend",
    # django-guardian object-level permissions.
    "guardian.backends.ObjectPermissionBackend",
]
# The local email/password backend is an administrative fallback.  It is listed
# only when explicitly enabled (development/test by default; production should
# disable it - see ENABLE_PASSWORD_LOGIN below).
_ENABLE_PASSWORD_LOGIN = env_bool("ENABLE_PASSWORD_LOGIN", True)
if _ENABLE_PASSWORD_LOGIN:
    AUTHENTICATION_BACKENDS.insert(1, "django.contrib.auth.backends.ModelBackend")

# Object-level permissions are enabled via django-guardian, but the anonymous
# user account is not required for a generic platform. Disable auto-creation to
# avoid coupling guardian to the custom user model.
ANONYMOUS_USER_NAME = None

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "core:home"
LOGOUT_REDIRECT_URL = "accounts:login"

# ---------------------------------------------------------------------------
# Identity: Microsoft Entra ID (OpenID Connect / OAuth 2.0 via MSAL)
# ---------------------------------------------------------------------------
# Every value comes from the environment; no tenant id, client id or secret is
# ever hard-coded (ADR 0029).  ``.env.example`` carries placeholders only.
MICROSOFT_CLIENT_ID = env("MICROSOFT_CLIENT_ID", "")
MICROSOFT_CLIENT_SECRET = env("MICROSOFT_CLIENT_SECRET", "")
MICROSOFT_TENANT_ID = env("MICROSOFT_TENANT_ID", "")
# Absolute callback registered in the Entra app registration, e.g.
# https://app.example.com/auth/microsoft/callback/
MICROSOFT_REDIRECT_URI = env("MICROSOFT_REDIRECT_URI", "")
MICROSOFT_POST_LOGOUT_REDIRECT_URI = env("MICROSOFT_POST_LOGOUT_REDIRECT_URI", "")
MICROSOFT_AUTHORITY_HOST = env("MICROSOFT_AUTHORITY_HOST", "https://login.microsoftonline.com")
# Scopes requested at sign-in.  Deliberately narrow: we need identity claims,
# not Graph access, and we never store the resulting tokens.
MICROSOFT_SCOPES = env_list("MICROSOFT_SCOPES", ["openid", "profile", "email"])
# An allow-list of directory (tenant) ids permitted to sign in.  Empty means
# "whatever the configured authority resolves to" (single-tenant deployments).
MICROSOFT_ALLOWED_TENANTS = env_list("MICROSOFT_ALLOWED_TENANTS", [])

#: The OIDC authority MSAL talks to (tenant-specific unless left blank).
MICROSOFT_AUTHORITY = (
    f"{MICROSOFT_AUTHORITY_HOST}/{MICROSOFT_TENANT_ID}" if MICROSOFT_TENANT_ID else ""
)
MICROSOFT_END_SESSION_ENDPOINT = (
    f"{MICROSOFT_AUTHORITY}/oauth2/v2.0/logout" if MICROSOFT_AUTHORITY else ""
)

#: First-login behaviour for an unknown-but-valid Entra identity: "pending"
#: (create a user awaiting approval - the secure enterprise default) or "active".
AUTH_PROVISIONING_MODE = env("AUTH_PROVISIONING_MODE", "pending")

#: Whether a first-time Entra sign-in may be linked to an existing local account
#: by matching email.  "non_staff" (default) never links a privileged account, so
#: a matching email can never inherit administrator rights; "all" opts in;
#: "disabled" refuses (spec Â§13 / Â§32).
AUTH_EMAIL_LINKING = env("AUTH_EMAIL_LINKING", "non_staff")

#: Whether logout also signs the browser out of Microsoft (federated sign-out).
#: Off by default: local sign-out is sufficient and less surprising.
MICROSOFT_FEDERATED_LOGOUT = env_bool("MICROSOFT_FEDERATED_LOGOUT", False)

#: Whether the email/password fallback backend and its form are available.
ENABLE_PASSWORD_LOGIN = _ENABLE_PASSWORD_LOGIN

#: Whether Entra sign-in is fully configured (all required values present).
MICROSOFT_AUTH_CONFIGURED = bool(
    MICROSOFT_CLIENT_ID and MICROSOFT_CLIENT_SECRET and MICROSOFT_TENANT_ID
)

#: Sign-in throttling (spec Â§31).  Failed attempts per client address per window;
#: the counters live in the configured cache (no new infrastructure).
AUTH_THROTTLE_MAX_ATTEMPTS = env_int("AUTH_THROTTLE_MAX_ATTEMPTS", 10)
AUTH_THROTTLE_WINDOW_SECONDS = env_int("AUTH_THROTTLE_WINDOW_SECONDS", 300)
#: How many reverse proxies sit in front of the app.  The throttle then reads the client
#: address that many hops from the *right* of X-Forwarded-For (the part a client cannot
#: forge).  0 = no trusted proxy: only REMOTE_ADDR is used and the header is ignored.
TRUSTED_PROXY_COUNT = env_int("TRUSTED_PROXY_COUNT", 0)

#: Session lifetime (ADR 0038).  Absolute: a sign-in never outlives this, however active.
#: Idle: no request for this long ends the session (0 disables).  Both are enforced by
#: IdentityMiddleware; SESSION_COOKIE_AGE is kept aligned so the browser cookie expires too.
SESSION_ABSOLUTE_TIMEOUT_SECONDS = env_int("SESSION_ABSOLUTE_TIMEOUT_SECONDS", 8 * 60 * 60)
SESSION_IDLE_TIMEOUT_SECONDS = env_int("SESSION_IDLE_TIMEOUT_SECONDS", 30 * 60)
SESSION_COOKIE_AGE = SESSION_ABSOLUTE_TIMEOUT_SECONDS

# ---------------------------------------------------------------------------
# Authorization (RBAC; ADR 0042-0044)
# ---------------------------------------------------------------------------
#: Resolved grants are cached per (user, organization); every access change invalidates them
#: through signals, the TTL only bounds staleness across worker processes.  The default
#: in-process cache is per worker - point CACHES at a shared backend (Redis, database) before
#: running several workers (ADR 0044).
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "audit-default"}}
RBAC_CACHE_TTL = env_int("RBAC_CACHE_TTL", 300)
#: Honour pre-RBAC Django model permissions as organization-wide grants (see registry ``legacy``).
RBAC_LEGACY_MODEL_PERMISSIONS = env_bool("RBAC_LEGACY_MODEL_PERMISSIONS", True)

# ---------------------------------------------------------------------------
# Internationalization
# ---------------------------------------------------------------------------
LANGUAGE_CODE = env("DJANGO_LANGUAGE_CODE", "en-us")
TIME_ZONE = env("DJANGO_TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------------------
# Static & media
# ---------------------------------------------------------------------------
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

# ---------------------------------------------------------------------------
# Evidence & attachments (Phase 11) - PRIVATE storage, never under MEDIA_ROOT / STATIC_ROOT / a web-served directory.
# ---------------------------------------------------------------------------
EVIDENCE_ROOT = Path(env("EVIDENCE_ROOT", str(BASE_DIR / "private_evidence")))
#: Storage backend (dotted path of an ``apps.evidence.storage.EvidenceStorage`` subclass).  Only the private filesystem backend ships;
#: an object-storage adapter implements the same four methods (put / open / delete / exists).
EVIDENCE_STORAGE = env("EVIDENCE_STORAGE", "apps.evidence.storage.FileSystemEvidenceStorage")
EVIDENCE_MAX_FILE_BYTES = env_int("EVIDENCE_MAX_FILE_BYTES", 10 * 1024 * 1024)
EVIDENCE_MAX_REQUEST_BYTES = env_int("EVIDENCE_MAX_REQUEST_BYTES", 12 * 1024 * 1024)
EVIDENCE_MAX_PIXELS = env_int("EVIDENCE_MAX_PIXELS", 40_000_000)
EVIDENCE_MAX_PER_TARGET = env_int("EVIDENCE_MAX_PER_TARGET", 10)
#: Malware scanning (apps/evidence/scanning.py).  When REQUIRED (the default) a file is never served or counted as evidence until a
#: scanner answered "clean"; with no scanner configured it simply stays pending.  Development turns this off explicitly.
EVIDENCE_SCANNER = env("EVIDENCE_SCANNER", "apps.evidence.scanning.UnconfiguredScanner")
EVIDENCE_SCAN_REQUIRED = env_bool("EVIDENCE_SCAN_REQUIRED", True)
EVIDENCE_CLAMD_HOST = env("EVIDENCE_CLAMD_HOST", "127.0.0.1")
EVIDENCE_CLAMD_PORT = env_int("EVIDENCE_CLAMD_PORT", 3310)
EVIDENCE_CLAMD_TIMEOUT = env_int("EVIDENCE_CLAMD_TIMEOUT", 10)
#: May users see files that are still waiting for the security scan?  (Quarantined files are never served.)
EVIDENCE_ALLOW_VIEW_PENDING = env_bool("EVIDENCE_ALLOW_VIEW_PENDING", False)

# --- Findings (Phase 12) --------------------------------------------------------------------------
#: When true a finding can only be closed after an independent verification (a gate registered by a later phase); until one exists
#: closing is refused rather than pretending the finding was verified.  Off by default: closing is then an authorized human decision.
FINDINGS_REQUIRE_VERIFICATION = env_bool("FINDINGS_REQUIRE_VERIFICATION", False)

# --- Corrective actions (Phase 13) ----------------------------------------------------------------
#: New actions require independent verification: the verifier is neither the owner nor the submitter (snapshotted on each action).
CORRECTIVE_ACTION_INDEPENDENT_VERIFICATION = env_bool("CORRECTIVE_ACTION_INDEPENDENT_VERIFICATION", True)
#: Additionally forbid the *creator* of an action from verifying it.
CORRECTIVE_ACTION_VERIFIER_NOT_CREATOR = env_bool("CORRECTIVE_ACTION_VERIFIER_NOT_CREATOR", False)
#: Verified completion files a new action needs before it can be submitted (0 = none; overridable per action).
CORRECTIVE_ACTION_DEFAULT_REQUIRED_EVIDENCE = env_int("CORRECTIVE_ACTION_DEFAULT_REQUIRED_EVIDENCE", 0)
#: A finding cannot be resolved while any of its corrective actions is outstanding (Phase 12 hook; see docs/corrective-action-finding-policy.md).
CORRECTIVE_ACTIONS_BLOCK_FINDING_RESOLUTION = env_bool("CORRECTIVE_ACTIONS_BLOCK_FINDING_RESOLUTION", True)
#: The finding-verification gate needs at least one verified/closed action (not merely "no outstanding actions").
CORRECTIVE_ACTIONS_REQUIRED_FOR_VERIFICATION = env_bool("CORRECTIVE_ACTIONS_REQUIRED_FOR_VERIFICATION", True)

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
    },
}

# ---------------------------------------------------------------------------
# Email (Django 6.1 MAILERS API)
# ---------------------------------------------------------------------------
# Business code must never talk to SMTP directly; it goes through
# apps.notifications.  These settings only describe *how* mail leaves the app.
MAILERS = {
    "default": {
        "BACKEND": "django.core.mail.backends.smtp.EmailBackend",
        "OPTIONS": {
            "host": env("EMAIL_HOST", "localhost"),
            "port": env_int("EMAIL_PORT", 587),
            "username": env("EMAIL_HOST_USER", ""),
            "password": env("EMAIL_HOST_PASSWORD", ""),
            "use_tls": env_bool("EMAIL_USE_TLS", True),
            "timeout": env_int("EMAIL_TIMEOUT", 30),
        },
    },
}
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "no-reply@checklist-platform.local")
SERVER_EMAIL = env("SERVER_EMAIL", DEFAULT_FROM_EMAIL)
EMAIL_SUBJECT_PREFIX = env("EMAIL_SUBJECT_PREFIX", "[Checklist Platform] ")

# Provider seam used by apps.notifications.services. Swap to the Microsoft 365
# provider in a later phase without touching business code.
NOTIFICATION_EMAIL_PROVIDER = env(
    "NOTIFICATION_EMAIL_PROVIDER",
    "apps.notifications.providers.django_smtp.DjangoEmailProvider",
)
#: Phase 17 notifications engine (see docs/notifications-architecture.md).  Delivery is attempted right after a business transaction commits when
#: ``NOTIFICATION_DISPATCH_INLINE`` is on; with it off, deliveries wait for ``manage.py dispatch_notifications`` (the Phase 18 scheduler's entry point).
NOTIFICATION_DISPATCH_INLINE = env_bool("NOTIFICATION_DISPATCH_INLINE", True)
NOTIFICATION_MAX_ATTEMPTS = env_int("NOTIFICATION_MAX_ATTEMPTS", 5)
NOTIFICATION_BATCH_LIMIT = env_int("NOTIFICATION_BATCH_LIMIT", 50)
NOTIFICATION_RETENTION_DAYS = env_int("NOTIFICATION_RETENTION_DAYS", 180)
#: Scheme + host used to build absolute links in emails (an internal *path* is always appended; empty = emails carry the path only).
# --- Scheduling (Phase 18): see docs/scheduling-architecture.md. Defaults are safe; every limit is also capped in apps/jobs/constants.py.
#: "poll" (default): the runner command (``manage.py process_scheduled_jobs``) picks jobs up.  "django_tasks": a manual run-now is additionally handed to the configured Django Tasks backend (needs a real worker; see the docs).
JOBS_EXECUTION_BACKEND = env("JOBS_EXECUTION_BACKEND", "poll")
JOBS_LEASE_SECONDS = env_int("JOBS_LEASE_SECONDS", 600)
JOBS_MISFIRE_GRACE_SECONDS = env_int("JOBS_MISFIRE_GRACE_SECONDS", 300)
JOBS_RETRY_JITTER = float(env("JOBS_RETRY_JITTER", "0.1"))
JOBS_HISTORY_RETENTION_DAYS = env_int("JOBS_HISTORY_RETENTION_DAYS", 90)
JOBS_RUNNER_ID = env("JOBS_RUNNER_ID", "")

NOTIFICATION_BASE_URL = env("NOTIFICATION_BASE_URL", "")
#: Microsoft Graph sender (see providers/microsoft365.py): the mailbox that sends, used only when that provider is selected.
NOTIFICATION_GRAPH_SENDER = env("NOTIFICATION_GRAPH_SENDER", "")
NOTIFICATION_GRAPH_TIMEOUT = env_int("NOTIFICATION_GRAPH_TIMEOUT", 15)

# ---------------------------------------------------------------------------
# Background tasks (native Django Tasks framework)
# ---------------------------------------------------------------------------
# ImmediateBackend executes work inline and is intended for development and
# tests only. Production swaps in a backend with a real worker; definitions stay
# behind the service boundary in apps/*/tasks so nothing else has to change.
TASKS = {
    "default": {
        "BACKEND": "django.tasks.backends.immediate.ImmediateBackend",
    },
}

# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
X_FRAME_OPTIONS = "DENY"
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
DATA_UPLOAD_MAX_MEMORY_SIZE = env_int("DATA_UPLOAD_MAX_MEMORY_SIZE", 10 * 1024 * 1024)
FILE_UPLOAD_MAX_MEMORY_SIZE = env_int("FILE_UPLOAD_MAX_MEMORY_SIZE", 10 * 1024 * 1024)

# ---------------------------------------------------------------------------
# Content Security Policy (native Django 6.1)
# ---------------------------------------------------------------------------
# A single strict policy is shared by environments; development starts in
# report-only mode so violations are visible without breaking the UI.
BASE_CSP: dict[str, list[str]] = {
    "default-src": [CSP.SELF],
    "base-uri": [CSP.SELF],
    "object-src": [CSP.NONE],
    "frame-ancestors": [CSP.NONE],
    "form-action": [CSP.SELF],
    "script-src": [CSP.SELF, CSP.NONCE],
    "style-src": [CSP.SELF, CSP.NONCE],
    "img-src": [CSP.SELF, "data:", "blob:"],
    "font-src": [CSP.SELF, "data:"],
    "connect-src": [CSP.SELF],
    "manifest-src": [CSP.SELF],
    "worker-src": [CSP.SELF],
}

# ---------------------------------------------------------------------------
# REST framework
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_VERSIONING_CLASS": "rest_framework.versioning.NamespaceVersioning",
    "DEFAULT_VERSION": "v1",
    "ALLOWED_VERSIONS": ["v1"],
    "DEFAULT_PAGINATION_CLASS": "apps.api.pagination.StandardResultsSetPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": env("API_THROTTLE_ANON", "60/min"),
        "user": env("API_THROTTLE_USER", "1000/hour"),
        "offline_sync": env("API_THROTTLE_OFFLINE_SYNC", "120/min"),
        "evidence_upload": env("API_THROTTLE_EVIDENCE_UPLOAD", "60/min"),
    },
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_RENDERER_CLASSES": [
        "apps.api.renderers.EnvelopeJSONRenderer",
    ],
    "EXCEPTION_HANDLER": "apps.api.exceptions.api_exception_handler",
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Checklist Platform API",
    "DESCRIPTION": (
        "Generic enterprise checklist & inspection platform. "
        "API version 1 foundation."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": "/api/v1",
    "COMPONENT_SPLIT_REQUEST": True,
    "SORT_OPERATIONS": False,
    # Several resources share field names (``status``, ``mode`` ...) with different choice
    # sets; naming them keeps the generated schema stable and readable.
    "ENUM_NAME_OVERRIDES": {
        "AppearanceEnum": "apps.themes.constants.Appearance",
        "PreviewModeEnum": "apps.themes.constants.PREVIEW_MODES",
        "ThemeStatusEnum": "apps.themes.constants.ThemeStatus",
        "ThemeScopeEnum": "apps.themes.constants.ThemeScope",
        "OrganizationStatusEnum": "apps.organizations.models.Organization.Status",
        "UserStatusEnum": "apps.accounts.constants.UserStatus",
    },
}

# ---------------------------------------------------------------------------
# Third-party app configuration
# ---------------------------------------------------------------------------
# Render form widgets with the project's template engine so custom widgets can
# use templates under ``templates/`` and share the design system.
FORM_RENDERER = "django.forms.renderers.TemplatesSetting"

CRISPY_ALLOWED_TEMPLATE_PACKS = "tailwind"
CRISPY_TEMPLATE_PACK = "tailwind"

DJANGO_TABLES2_TEMPLATE = "core/tables/table.html"

IMPORT_EXPORT_USE_TRANSACTIONS = True
IMPORT_EXPORT_SKIP_ADMIN_LOG = False

SIMPLE_HISTORY_HISTORY_ID_USE_UUID = True
SIMPLE_HISTORY_REVERSION_LIMIT = 20

# ---------------------------------------------------------------------------
# Admin (Jazzmin, AdminLTE 4 / Bootstrap 5.3)
# ---------------------------------------------------------------------------
# See docs/architecture/admin.md and ADR 0020.
#
# Principles
#   * Nothing is fetched from a third party: Google Fonts are off, every asset is
#     served from our own static files (strict CSP, privacy, offline-friendly).
#   * The admin is the *platform operator's* console. Day-to-day users work in the
#     application UI, so the admin links back to it and vice-versa.
#   * Branding comes from CSS variables (static/css/admin-brand.css) because the
#     bundled AdminLTE 4 ignores the old AdminLTE 3 colour classes.
JAZZMIN_SETTINGS = {
    # --- Identity ---------------------------------------------------------
    "site_title": "Checklist Platform Admin",
    "site_header": "Checklist Platform",
    "site_brand": "Checklist Platform",
    # One image drives the sidebar logo, the login logo and the favicon.
    "site_logo": "icons/icon-192.png",
    "site_logo_classes": "rounded",
    "site_icon": "icons/icon-192.png",
    "welcome_sign": "Platform administration",
    "copyright": "Checklist Platform",
    # --- Third-party requests: none ---------------------------------------
    "use_google_fonts_cdn": False,
    # The related-object popup renders an <iframe>; X-Frame-Options is DENY.
    "related_modal_active": False,
    "show_ui_builder": False,
    "show_theme_chooser": False,
    "language_chooser": False,
    "custom_css": "css/admin-brand.css",
    "custom_js": "js/admin-compat.js",  # works around a Jazzmin import-page bug; see the file
    # --- Search & navigation ---------------------------------------------
    # One global search box (one per model would crowd the navbar): locations are the
    # largest, most-searched table. Organizations/users have list search + filters.
    "search_model": ["locations.Location"],
    # Deliberately no ``user_avatar``: for users without an uploaded picture Jazzmin
    # falls back to a stock *photograph*; its neutral user icon is the right default.
    "show_sidebar": True,
    "navigation_expanded": True,
    "order_with_respect_to": [
        "organizations",
        "organizations.organization",
        "organizations.organizationmembership",
        "locations",
        "locations.location",
        "locations.locationtype",
        "locations.membershiplocationscope",
        "themes",
        "themes.theme",
        "themes.themeactivation",
        "themes.userappearance",
        "accounts",
        "permissions",
        "permissions.role",
        "permissions.permission",
        "permissions.membershiprole",
        "permissions.rolescope",
        "permissions.userrole",
        "auth",
        "core",
    ],
    "topmenu_links": [
        {"name": "Dashboard", "url": "admin:index"},
        {"name": "Open application", "url": "core:home"},
        {
            "name": "Organizations",
            "url": "organizations:list",
            "permissions": ["organizations.view_organization"],
        },
        {
            "name": "Location tree",
            "url": "locations:tree",
            "permissions": ["locations.view_location"],
        },
        {
            "name": "Themes",
            "url": "themes:list",
            "permissions": ["themes.view_theme"],
        },
        {"name": "API docs", "url": "/api/docs/", "new_window": True},
    ],
    "usermenu_links": [
        {"name": "My profile", "url": "accounts:profile", "icon": "fas fa-id-badge"},
        {"name": "API schema", "url": "/api/schema/", "new_window": True, "icon": "fas fa-code"},
    ],
    # --- Icons (Font Awesome 6 Free, bundled) -----------------------------
    "default_icon_parents": "fas fa-folder",
    "default_icon_children": "fas fa-circle",
    "icons": {
        "organizations": "fas fa-building",
        "organizations.Organization": "fas fa-building",
        "organizations.OrganizationMembership": "fas fa-id-badge",
        "locations": "fas fa-location-dot",
        "locations.Location": "fas fa-location-dot",
        "locations.LocationType": "fas fa-tags",
        "locations.MembershipLocationScope": "fas fa-bullseye",
        "themes": "fas fa-palette",
        "themes.Theme": "fas fa-palette",
        "themes.ThemeActivation": "fas fa-clock-rotate-left",
        "themes.UserAppearance": "fas fa-circle-half-stroke",
        "accounts": "fas fa-users",
        "accounts.User": "fas fa-user",
        "permissions": "fas fa-user-shield",
        "permissions.Role": "fas fa-user-tag",
        "permissions.Permission": "fas fa-key",
        "permissions.MembershipRole": "fas fa-id-badge",
        "permissions.RoleScope": "fas fa-sitemap",
        "permissions.UserRole": "fas fa-user-lock",
        "auth": "fas fa-users-cog",
        "auth.Group": "fas fa-users",
        "core": "fas fa-gear",
        "core.ActivityLog": "fas fa-clock-rotate-left",
        "checklists": "fas fa-clipboard-check",
        "checklists.ChecklistCategory": "fas fa-tags",
        "checklists.ChecklistTemplate": "fas fa-clipboard-list",
        "checklists.ChecklistVersion": "fas fa-code-branch",
        "checklists.ChecklistSection": "fas fa-layer-group",
        "checklists.ChecklistItem": "fas fa-list-check",
        "checklists.ChecklistOption": "fas fa-dot-circle",
        "checklists.ChecklistRule": "fas fa-project-diagram",
        "assignments": "fas fa-user-check",
        "assignments.ChecklistAssignment": "fas fa-user-check",
        "executions.ChecklistExecution": "fas fa-play-circle",
        "executions.ChecklistResponse": "fas fa-reply",
        "executions.SyncReceipt": "fas fa-sync",
        "evidence.EvidenceAsset": "fas fa-paperclip",
        "evidence.EvidenceAttachment": "fas fa-link",
        "executions": "fas fa-play-circle",
        "findings": "fas fa-exclamation-triangle",
        "findings.Finding": "fas fa-exclamation-triangle",
        "findings.FindingCategory": "fas fa-tags",
        "findings.FindingSeverity": "fas fa-sort-amount-up",
        "evidence": "fas fa-paperclip",
        "evaluation.EvaluationRuleSet": "fas fa-gavel",
        "evaluation.EvaluationRun": "fas fa-clipboard-check",
        "evaluation.EvaluationActionRequest": "fas fa-bolt",
        "notifications.NotificationEvent": "fas fa-inbox",
        "notifications.NotificationTemplate": "fas fa-file-lines",
        "notifications.NotificationRule": "fas fa-sliders",
        "notifications.UserNotificationPreference": "fas fa-user-gear",
        "notifications.Notification": "fas fa-bell",
        "notifications.NotificationDelivery": "fas fa-paper-plane",
        "notifications.DeliveryAttempt": "fas fa-clock-rotate-left",
        "jobs.ScheduleDefinition": "fas fa-calendar-days",
        "jobs.ScheduledJob": "fas fa-list-check",
        "jobs.JobExecution": "fas fa-clock-rotate-left",
        "jobs.RunnerHeartbeat": "fas fa-heart-pulse",
        "scoring.ScoringConfig": "fas fa-calculator",
        "scoring.ScoreResult": "fas fa-percentage",
        "workflows.WorkflowDefinition": "fas fa-project-diagram",
        "workflows.WorkflowVersion": "fas fa-code-branch",
        "workflows.WorkflowState": "fas fa-circle",
        "workflows.WorkflowTransition": "fas fa-exchange-alt",
        "workflows.WorkflowInstance": "fas fa-play-circle",
        "workflows.WorkflowTask": "fas fa-tasks",
        "workflows.WorkflowEvent": "fas fa-history",
        "corrective_actions.CorrectiveAction": "fas fa-wrench",
        "corrective_actions.CorrectiveActionUpdate": "fas fa-clipboard-list",
        "corrective_actions.CorrectiveActionVerification": "fas fa-check-double",
        "findings": "fas fa-triangle-exclamation",
        "corrective_actions": "fas fa-screwdriver-wrench",
        "workflows": "fas fa-diagram-project",
        "scoring": "fas fa-percent",
    "evaluation": "fas fa-gavel",
        "notifications": "fas fa-bell",
        "jobs": "fas fa-calendar-days",
        "reporting": "fas fa-chart-line",
    },
    # --- Change forms -----------------------------------------------------
    "changeform_format": "horizontal_tabs",
    "changeform_format_overrides": {
        "accounts.user": "collapsible",
        "auth.group": "vertical_tabs",
    },
}

# Only the options that exist in AdminLTE 4 / Bootstrap 5.3.  The old AdminLTE 3
# colour classes (``accent-*``, ``navbar-*``, ``sidebar-dark-*``) are not in the
# bundled stylesheet and had no effect; brand colours live in admin-brand.css.
JAZZMIN_UI_TWEAKS = {
    "theme": "default",
    # Follow the operating-system preference (set before first paint by Jazzmin).
    "default_theme_mode": "auto",
    "navbar_fixed": True,
    "sidebar_fixed": True,
    "sidebar_nav_flat_style": True,
    "actions_sticky_top": True,
    # Button roles on the dashboard and forms: "Add" is the primary action and "Change" a
    # quiet outline, so the admin reads as one product with the application (brand indigo).
    "button_classes": {
        "primary": "btn-primary",
        "secondary": "btn-secondary",
        "info": "btn-outline-primary",
        "warning": "btn-warning",
        "danger": "btn-danger",
        "success": "btn-primary",
    },
}

# ---------------------------------------------------------------------------
# Admin Content-Security-Policy
# ---------------------------------------------------------------------------
# Every page keeps the strict BASE_CSP; /admin/ gets this narrowly-widened variant
# (apps.core.middleware.AdminContentSecurityPolicyMiddleware):
#
#   * script-src  - Jazzmin renders a few small *static* inline scripts (theme
#                   bootstrap, changelist filter config, password forms).  They are
#                   allowed by SHA-256 hash, not by 'unsafe-inline'.  A Jazzmin upgrade
#                   that changes one makes tests/test_admin_theme.py fail and names the
#                   new hash to review and add here.
#   * style-src-attr - Django admin/Jazzmin emit a handful of ``style="..."``
#                   attributes (margins, widths).  Only *attributes* are allowed
#                   inline; <style> blocks and external CSS stay under the strict rule.
#
# Nothing else is relaxed: no third-party origins, no eval, no inline <style>.
ADMIN_CSP_SCRIPT_HASHES: list[str] = [
    # admin/base.html + registration/base.html: sets data-bs-theme before first paint.
    "sha256-oufyiLq9heVNX0RFs532lnKzrs2pHgVyLVsyRlTCfQg=",
    # admin/change_list.html: window.filterInputLength defaults.
    "sha256-F7GVayJMLy0KHikNUhjbBcBE0ax/V4knxuE9lJsuA6A=",
    # registration/password_change_form.html: marks required fields.
    "sha256-DT/ci/z/ofx36DBKYq8ZBDQ2sgAnvO+SmuKGfcZ95UU=",
    # admin/auth/user/change_password.html: marks required fields.
    "sha256-OQlLsGYyiAVVwdHnkEraaS+8yfFm7rTeYMfelN7RwuE=",
]
ADMIN_CSP_STYLE_ATTR = [CSP.UNSAFE_INLINE]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {name} {process:d} {thread:d} {message}",
            "style": "{",
        },
        "simple": {
            "format": "{levelname} {asctime} {name} {message}",
            "style": "{",
        },
    },
    "filters": {
        "require_debug_false": {"()": "django.utils.log.RequireDebugFalse"},
        "require_debug_true": {"()": "django.utils.log.RequireDebugTrue"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "simple",
        },
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(LOG_DIR / "app.log"),
            "maxBytes": 10 * 1024 * 1024,
            "backupCount": 5,
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console", "file"],
        "level": env("DJANGO_LOG_LEVEL", "INFO"),
    },
    "loggers": {
        "django": {
            "handlers": ["console", "file"],
            "level": env("DJANGO_LOG_LEVEL", "INFO"),
            "propagate": False,
        },
        "django.security": {
            "handlers": ["console", "file"],
            "level": "INFO",
            "propagate": False,
        },
        "apps": {
            "handlers": ["console", "file"],
            "level": env("APP_LOG_LEVEL", "INFO"),
            "propagate": False,
        },
    },
}

