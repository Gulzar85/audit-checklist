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
    "apps.accounts",
    "apps.permissions",
    "apps.checklists",
    "apps.executions",
    "apps.findings",
    "apps.corrective_actions",
    "apps.workflows",
    "apps.scoring",
    "apps.notifications",
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
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "simple_history.middleware.HistoryRequestMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
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
    "django.contrib.auth.backends.ModelBackend",
    "guardian.backends.ObjectPermissionBackend",
]

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
LOGOUT_REDIRECT_URL = "core:home"

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
}

# ---------------------------------------------------------------------------
# Third-party app configuration
# ---------------------------------------------------------------------------
CRISPY_ALLOWED_TEMPLATE_PACKS = "tailwind"
CRISPY_TEMPLATE_PACK = "tailwind"

DJANGO_TABLES2_TEMPLATE = "core/tables/table.html"

IMPORT_EXPORT_USE_TRANSACTIONS = True
IMPORT_EXPORT_SKIP_ADMIN_LOG = False

SIMPLE_HISTORY_HISTORY_ID_USE_UUID = True
SIMPLE_HISTORY_REVERSION_LIMIT = 20

# ---------------------------------------------------------------------------
# Jazzmin (admin theme) — branding foundation only in Phase 0
# ---------------------------------------------------------------------------
JAZZMIN_SETTINGS = {
    "site_title": "Checklist Platform",
    "site_header": "Checklist Platform",
    "site_brand": "Checklist Platform",
    "welcome_sign": "Enterprise Checklist & Inspection Platform",
    "copyright": "Checklist Platform",
    "search_model": ["accounts.User", "organizations.Organization"],
    "show_ui_builder": False,
    "topmenu_links": [
        {"name": "Home", "url": "admin:index", "permissions": ["auth.view_user"]},
        {"name": "API", "url": "/api/v1/", "new_window": True},
        {"name": "Docs", "url": "/api/docs/", "new_window": True},
    ],
    "usermenu_links": [
        {"name": "API Schema", "url": "/api/schema/", "new_window": True},
    ],
    # Icon keys map to Font Awesome (bundled with Jazzmin); Lucide is used in
    # the application UI. Kept minimal on purpose — populated further in later
    # phases as modules are delivered.
    "icons": {
        "accounts.User": "fas fa-users",
        "auth.Group": "fas fa-user-shield",
        "organizations.Organization": "fas fa-building",
        "organizations.OrganizationMembership": "fas fa-id-badge",
        "locations.LocationType": "fas fa-sitemap",
        "locations.Location": "fas fa-location-dot",
        "permissions.Role": "fas fa-user-tag",
        "checklists": "fas fa-clipboard-check",
        "executions": "fas fa-play-circle",
        "findings": "fas fa-triangle-exclamation",
        "corrective_actions": "fas fa-screwdriver-wrench",
        "workflows": "fas fa-diagram-project",
        "scoring": "fas fa-percent",
        "notifications": "fas fa-bell",
        "reporting": "fas fa-chart-line",
    },
}

JAZZMIN_UI_TWEAKS = {
    "navbar_small_text": False,
    "footer_small_text": False,
    "body_small_text": False,
    "brand_small_text": False,
    "brand_colour": False,
    "accent": "accent-primary",
    "navbar": "navbar-dark",
    "no_navbar_border": False,
    "navbar_fixed": True,
    "layout_boxed": False,
    "footer_fixed": False,
    "sidebar_fixed": True,
    "sidebar": "sidebar-dark-primary",
    "sidebar_nav_small_text": False,
    "sidebar_disable_expand": False,
    "sidebar_nav_child_indent": False,
    "sidebar_nav_compact_style": False,
    "sidebar_nav_legacy_style": False,
    "sidebar_nav_flat_style": True,
    "theme": "default",
    # Jazzmin 3.x controls light/dark via ``data-bs-theme``; the old
    # ``dark_mode_theme`` key is deprecated and ignored.
    "default_theme_mode": "auto",
    "button_classes": {
        "primary": "btn-primary",
        "secondary": "btn-secondary",
        "info": "btn-info",
        "warning": "btn-warning",
        "danger": "btn-danger",
        "success": "btn-success",
    },
}

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
