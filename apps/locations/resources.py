"""django-import-export resources for location types and locations.

Locations are a *tree*, so importing them safely needs more care than a flat
table.  An import is accepted only if the whole file is valid:

* everything runs in one transaction; any invalid row rolls the whole import back
  (the admin sets ``rollback_on_validation_errors``), so a bad file can never
  leave a partly-applied hierarchy;
* rows are re-ordered parents-first, so a file may list children before parents;
* duplicate ``(organization, code)`` rows and circular parent chains *inside the
  file* are rejected up front with a readable message;
* organization, location type and parent are referenced by **code** and are
  resolved *within the row's organization* - a parent or type that only exists in
  another organization simply is not found, so cross-organization links cannot be
  expressed at all (the model rules and database triggers still backstop this);
* every row is authorised through the policy layer for the importing user, and
  organization/type/parent look-ups share one neutral "not found or not allowed"
  message so an import cannot probe which other tenants exist;
* the lifecycle (``is_active``) may be *set* on a new row but never *changed* on
  an existing one - those transitions have their own audited operations;
* created/updated rows are logged with ``source=import`` and keep model history.
"""

from __future__ import annotations

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from import_export import fields, resources, widgets

from apps.core.policies import is_active_user
from apps.core.resources import FormulaSafeExportMixin
from apps.core.services import log_activity, stamp_history
from apps.organizations.models import Organization, normalize_organization_code

from .models import Location, LocationType, normalize_code
from .policies import (
    can_create_location,
    can_create_location_type,
    can_edit_location,
    can_edit_location_type,
    can_move_location,
)
from .services import as_validation_error, lock_organization, require_active_organization

_NOT_FOUND = "{what} “{value}” was not found, or you are not allowed to import into it."


# ---------------------------------------------------------------------------
# Widgets: reference related rows by code, scoped to the row's organization
# ---------------------------------------------------------------------------
class OrganizationCodeWidget(widgets.ForeignKeyWidget):
    """Resolve an organization by its (normalised) code; blank means "none"."""

    def __init__(self, *, required: bool = True) -> None:
        super().__init__(Organization, field="code")
        self.required = required

    def clean(self, value, row=None, **kwargs):  # noqa: ANN001, ANN003
        code = normalize_organization_code(value or "")
        if not code:
            if self.required:
                raise ValueError("Organization is required.")
            return None
        try:
            return Organization.objects.get(code=code)
        except Organization.DoesNotExist:
            raise ValueError(_NOT_FOUND.format(what="Organization", value=value)) from None


def _row_organization_code(row) -> str:  # noqa: ANN001
    return normalize_organization_code((row or {}).get("organization", "") or "")


class LocationTypeCodeWidget(widgets.ForeignKeyWidget):
    """Resolve a location type usable by the row's organization (its own or platform)."""

    def __init__(self) -> None:
        super().__init__(LocationType, field="code")

    def clean(self, value, row=None, **kwargs):  # noqa: ANN001, ANN003
        code = normalize_code(value or "")
        if not code:
            raise ValueError("Location type is required.")
        match = (
            LocationType.objects.filter(code=code)
            .filter(Q(organization__code=_row_organization_code(row)) | Q(organization__isnull=True))
            .order_by(F("organization").asc(nulls_last=True))  # own type wins over platform
            .first()
        )
        if match is None:
            raise ValueError(_NOT_FOUND.format(what="Location type", value=value))
        return match


class ParentCodeWidget(widgets.ForeignKeyWidget):
    """Resolve a parent location *within the row's organization*; blank means root."""

    def __init__(self) -> None:
        super().__init__(Location, field="code")

    def clean(self, value, row=None, **kwargs):  # noqa: ANN001, ANN003
        code = normalize_code(value or "")
        if not code:
            return None
        try:
            return Location.objects.get(organization__code=_row_organization_code(row), code=code)
        except Location.DoesNotExist:
            raise ValueError(_NOT_FOUND.format(what="Parent location", value=value)) from None


class _ActorMixin:
    """Common import plumbing: the acting user, and uniform "not found / not allowed" errors."""

    _actor = None
    _row_organization = ""

    def require_actor(self, kwargs: dict) -> None:
        self._actor = kwargs.get("user")
        if not is_active_user(self._actor):
            raise PermissionDenied("Imports require an authenticated, active user.")

    def before_import_row(self, row, **kwargs):  # noqa: ANN001, ANN003
        super().before_import_row(row, **kwargs)
        self._row_organization = (row.get("organization") or "").strip()

    def get_or_init_instance(self, instance_loader, row):  # noqa: ANN001
        """Treat an unresolvable key (e.g. an unknown organization) as "no existing row".

        The field-level error is then reported by ``import_instance`` as an ordinary
        validation error - the same *kind* of error as "exists but not allowed",
        so an importer cannot tell a foreign organization from a missing one.
        """
        try:
            return super().get_or_init_instance(instance_loader, row)
        except ValueError:
            return self.init_instance(row), True

    def not_allowed(self, what: str = "Organization") -> ValidationError:
        """The one error for 'not found' and 'not permitted' alike (no tenant probing)."""
        return ValidationError(
            {"organization": _NOT_FOUND.format(what=what, value=self._row_organization)},
            code="not_found",
        )


def _require_columns(dataset, required: set[str]) -> None:  # noqa: ANN001
    missing = required - set(dataset.headers or [])
    if missing:
        raise ValueError(f"Missing required column(s): {', '.join(sorted(missing))}.")


def _merge(import_validation_errors, field: str, message: str) -> dict:  # noqa: ANN001
    merged = {key: list(value) for key, value in (import_validation_errors or {}).items()}
    merged.setdefault(field, []).append(ValidationError(message))
    return merged


# ---------------------------------------------------------------------------
# Location types
# ---------------------------------------------------------------------------
TYPE_FIELDS = ("organization", "code", "name", "description", "sort_order", "is_active")


class LocationTypeResource(FormulaSafeExportMixin, _ActorMixin, resources.ModelResource):
    """Import / export location types keyed by ``(organization, code)``.

    A blank ``organization`` is a platform-defined type (platform staff only).
    """

    organization = fields.Field(
        attribute="organization",
        column_name="organization",
        widget=OrganizationCodeWidget(required=False),
    )

    class Meta:
        model = LocationType
        import_id_fields = ("organization", "code")
        fields = TYPE_FIELDS
        export_order = TYPE_FIELDS
        skip_unchanged = True
        report_skipped = True
        clean_model_instances = True
        use_transactions = True

    def get_queryset(self):  # noqa: ANN201
        return super().get_queryset().select_related("organization")

    def before_import(self, dataset, **kwargs):  # noqa: ANN001, ANN003
        super().before_import(dataset, **kwargs)
        self.require_actor(kwargs)
        _require_columns(dataset, {"code", "name"})

    def before_import_row(self, row, **kwargs):  # noqa: ANN001, ANN003
        super().before_import_row(row, **kwargs)  # records the raw organization for error messages
        if row.get("code"):
            row["code"] = normalize_code(row["code"])

    def after_init_instance(self, instance, new, row, **kwargs):  # noqa: ANN001, ANN003
        super().after_init_instance(instance, new, row, **kwargs)
        instance._import_is_new = new
        instance._import_original_active = instance.is_active

    def validate_instance(self, instance, import_validation_errors=None, validate_unique=True):  # noqa: ANN001
        merged = dict(import_validation_errors or {})
        if getattr(instance, "_import_is_new", True):
            allowed = can_create_location_type(self._actor, instance.organization)
        else:
            allowed = can_edit_location_type(self._actor, instance)
            if instance.is_active != instance._import_original_active:
                merged = _merge(
                    merged, "is_active",
                    "Lifecycle cannot be changed by import. Use activate / deactivate.",
                )
        if not allowed:
            if instance.organization_id:
                raise self.not_allowed()
            raise ValidationError({"organization": "Only platform staff may import platform-wide location types."})
        if instance.organization_id:
            require_active_organization(instance.organization)
        return super().validate_instance(instance, merged, validate_unique)

    def do_instance_save(self, instance, is_create):  # noqa: ANN001
        if instance.organization_id:
            lock_organization(instance.organization_id)
        stamp_history(instance, self._actor, "location_type.imported")
        try:
            with transaction.atomic():
                instance.save()
        except IntegrityError as exc:
            raise as_validation_error(exc) from exc
        log_activity(
            verb="location_type.created" if is_create else "location_type.updated",
            organization=instance.organization,
            actor=self._actor,
            description=f"Imported location type {instance.name} ({instance.code})",
            target=instance,
            metadata={"source": "import"},
        )


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------
LOCATION_FIELDS = (
    "organization",
    "code",
    "name",
    "location_type",
    "parent",
    "is_active",
    "description",
    "address_line1",
    "address_line2",
    "city",
    "state",
    "postal_code",
    "country",
    "latitude",
    "longitude",
    "timezone",
)


class LocationResource(FormulaSafeExportMixin, _ActorMixin, resources.ModelResource):
    """Import / export locations keyed by ``(organization, code)``."""

    organization = fields.Field(
        attribute="organization", column_name="organization", widget=OrganizationCodeWidget()
    )
    location_type = fields.Field(
        attribute="location_type", column_name="location_type", widget=LocationTypeCodeWidget()
    )
    parent = fields.Field(attribute="parent", column_name="parent", widget=ParentCodeWidget())

    class Meta:
        model = Location
        import_id_fields = ("organization", "code")
        fields = LOCATION_FIELDS
        export_order = LOCATION_FIELDS
        skip_unchanged = True
        report_skipped = True
        clean_model_instances = True
        use_transactions = True

    def get_queryset(self):  # noqa: ANN201
        return super().get_queryset().select_related("organization", "location_type", "parent")

    # -- file-level checks and ordering ------------------------------------
    def before_import(self, dataset, **kwargs):  # noqa: ANN001, ANN003
        super().before_import(dataset, **kwargs)
        self.require_actor(kwargs)
        _require_columns(dataset, {"organization", "code", "name", "location_type"})
        self._order_parents_first(dataset)

    @staticmethod
    def _order_parents_first(dataset) -> None:  # noqa: ANN001
        """Reject duplicate keys / cycles in the file, then sort parents before children."""
        headers = list(dataset.headers)
        rows = [dict(zip(headers, values, strict=True)) for values in dataset]

        def key(row) -> tuple[str, str]:  # noqa: ANN001
            return (
                normalize_organization_code(row.get("organization") or ""),
                normalize_code(row.get("code") or ""),
            )

        def parent_key(row) -> tuple[str, str] | None:  # noqa: ANN001
            parent = normalize_code(row.get("parent") or "")
            return (key(row)[0], parent) if parent else None

        by_key: dict[tuple[str, str], dict] = {}
        for number, row in enumerate(rows, start=1):
            row_key = key(row)
            if row_key in by_key:
                raise ValueError(
                    f"Row {number}: location “{row_key[1]}” appears more than once "
                    f"for organization “{row_key[0]}”."
                )
            by_key[row_key] = row

        depth: dict[tuple[str, str], int] = {}
        for start in by_key:
            chain: list[tuple[str, str]] = []
            current: tuple[str, str] | None = start
            while current is not None and current in by_key and current not in depth:
                if current in chain:
                    cycle = " → ".join(code for _org, code in chain[chain.index(current):] + [current])
                    raise ValueError(f"Circular hierarchy in the import file: {cycle}.")
                chain.append(current)
                current = parent_key(by_key[current])
            base = depth.get(current, -1) if current is not None else -1
            for offset, node in enumerate(reversed(chain), start=1):
                depth[node] = base + offset

        ordered = sorted(rows, key=lambda row: (key(row)[0], depth[key(row)]))
        dataset.wipe()
        dataset.headers = headers
        for row in ordered:
            dataset.append([row.get(header) for header in headers])

    # -- per-row hooks -------------------------------------------------------
    def before_import_row(self, row, **kwargs):  # noqa: ANN001, ANN003
        super().before_import_row(row, **kwargs)  # records the raw organization for error messages
        if row.get("code"):
            row["code"] = normalize_code(row["code"])

    def after_init_instance(self, instance, new, row, **kwargs):  # noqa: ANN001, ANN003
        super().after_init_instance(instance, new, row, **kwargs)
        instance._import_is_new = new
        instance._import_original_active = instance.is_active
        instance._import_original_parent_id = instance.parent_id
        instance._import_original_type_id = instance.location_type_id

    def validate_instance(self, instance, import_validation_errors=None, validate_unique=True):  # noqa: ANN001
        merged = dict(import_validation_errors or {})
        if instance.organization_id is None:
            return super().validate_instance(instance, merged, validate_unique)

        is_new = getattr(instance, "_import_is_new", True)
        if is_new:
            allowed = can_create_location(self._actor, instance.organization)
        else:
            allowed = can_edit_location(self._actor, instance) and (
                instance.parent_id == instance._import_original_parent_id
                or can_move_location(self._actor, instance, instance.parent)
            )
            if instance.is_active != instance._import_original_active:
                merged = _merge(
                    merged, "is_active",
                    "Lifecycle cannot be changed by import. Use activate / deactivate.",
                )
        if not allowed:
            raise self.not_allowed()
        require_active_organization(instance.organization)

        location_type = instance.location_type if instance.location_type_id else None
        if (
            location_type is not None
            and not location_type.is_active
            and (is_new or instance.location_type_id != instance._import_original_type_id)
        ):
            merged = _merge(merged, "location_type", "This location type is inactive and cannot be assigned.")
        return super().validate_instance(instance, merged, validate_unique)

    def do_instance_save(self, instance, is_create):  # noqa: ANN001
        # Validation ran before the lock; re-check inside it (the organization may have
        # been suspended, or a parent deactivated, in the meantime).
        require_active_organization(lock_organization(instance.organization_id))
        instance.full_clean()
        stamp_history(instance, self._actor, "location.imported")
        try:
            with transaction.atomic():
                instance.save()
        except IntegrityError as exc:
            raise as_validation_error(exc) from exc
        log_activity(
            verb="location.created" if is_create else "location.updated",
            organization=instance.organization,
            actor=self._actor,
            description=f"Imported location {instance.name} ({instance.code})",
            target=instance,
            metadata={"source": "import"},
        )
