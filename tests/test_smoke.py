"""Startup, settings and database smoke tests."""

from __future__ import annotations

import io

import pytest
from django.conf import settings
from django.core.management import call_command
from django.db import connection


def test_system_checks_pass():
    """Django's system check framework reports no configuration issues."""
    call_command("check")


@pytest.mark.django_db
def test_database_connection_works():
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        assert cursor.fetchone() == (1,)


def test_custom_user_model_is_configured():
    assert settings.AUTH_USER_MODEL == "accounts.User"


def test_all_local_apps_are_installed():
    local_apps = [app for app in settings.INSTALLED_APPS if app.startswith("apps.")]
    assert set(local_apps) == {
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
        "apps.dashboards",
        "apps.api",
    }


@pytest.mark.django_db
def test_migrations_are_complete():
    """No model changes are outstanding (makemigrations --check)."""
    out = io.StringIO()
    call_command("makemigrations", "--check", "--dry-run", stdout=out, verbosity=0)


@pytest.mark.django_db
def test_can_create_activity_log(user):
    from apps.core.models import ActivityLog

    entry = ActivityLog.objects.create(actor=user, verb="platform.created")
    assert entry.pk is not None
    assert entry.metadata_dict == {}
