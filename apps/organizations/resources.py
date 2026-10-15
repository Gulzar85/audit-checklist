"""django-import-export resources for organizations.

Imports are staff operations that run through the admin.  They are safe by
construction:

* the whole file is one transaction - any invalid row rolls everything back
  (``rollback_on_validation_errors`` in the admin), so a bad file can never
  half-apply;
* every row is validated with the model's ``full_clean`` (duplicate codes, bad
  status, malformed regional values ...);
* rows are authorised through the policy layer for the importing user;
* the lifecycle (``status``) can be *set* on a new organization but never
  *changed* on an existing one - transitions have their own audited operations;
* each created/updated row is recorded in the activity log with ``source=import``.
"""

from __future__ import annotations

from django.core.exceptions import PermissionDenied, ValidationError
from import_export import resources

from apps.core.policies import is_active_user
from apps.core.resources import FormulaSafeExportMixin
from apps.core.services import log_activity, stamp_history

from .models import Organization, normalize_organization_code
from .policies import can_create_organization, can_manage_organization

IMPORT_FIELDS = (
    "code",
    "name",
    "slug",
    "legal_name",
    "description",
    "status",
    "website",
    "email",
    "phone",
    "address_line1",
    "address_line2",
    "city",
    "state",
    "postal_code",
    "country",
    "timezone",
    "currency",
    "language",
    "date_format",
    "primary_color",
)


class OrganizationResource(FormulaSafeExportMixin, resources.ModelResource):
    """Import / export organizations keyed by their stable ``code``."""

    class Meta:
        model = Organization
        import_id_fields = ("code",)
        fields = IMPORT_FIELDS
        export_order = IMPORT_FIELDS
        skip_unchanged = True
        report_skipped = True
        clean_model_instances = True
        use_transactions = True

    _actor = None

    def before_import(self, dataset, **kwargs):  # noqa: ANN001, ANN003
        super().before_import(dataset, **kwargs)
        self._actor = kwargs.get("user")
        if not is_active_user(self._actor):
            raise PermissionDenied("Imports require an authenticated, active user.")
        missing = {"code", "name"} - set(dataset.headers or [])
        if missing:
            raise ValueError(f"Missing required column(s): {', '.join(sorted(missing))}.")

    def before_import_row(self, row, **kwargs):  # noqa: ANN001, ANN003
        super().before_import_row(row, **kwargs)
        # Look rows up by the canonical code so "acme-co" finds ACME_CO.
        if row.get("code"):
            row["code"] = normalize_organization_code(row["code"])

    def after_init_instance(self, instance, new, row, **kwargs):  # noqa: ANN001, ANN003
        super().after_init_instance(instance, new, row, **kwargs)
        instance._import_is_new = new
        instance._import_original_status = instance.status

    def validate_instance(self, instance, import_validation_errors=None, validate_unique=True):  # noqa: ANN001
        merged = {key: list(value) for key, value in (import_validation_errors or {}).items()}
        if getattr(instance, "_import_is_new", True):
            allowed = can_create_organization(self._actor)
        else:
            allowed = can_manage_organization(self._actor, instance)
            if instance.status != instance._import_original_status:
                merged.setdefault("status", []).append(
                    ValidationError(
                        "Status cannot be changed by import. Use the activate / deactivate / "
                        "suspend operations."
                    )
                )
        if not allowed:
            raise ValidationError({"code": "You are not allowed to import this organization."})
        return super().validate_instance(instance, merged, validate_unique)

    def do_instance_save(self, instance, is_create):  # noqa: ANN001
        stamp_history(instance, self._actor, "organization.imported")
        instance.save()
        log_activity(
            verb="organization.created" if is_create else "organization.updated",
            organization=instance,
            actor=self._actor,
            description=f"Imported organization {instance.name} ({instance.code})",
            target=instance,
            metadata={"source": "import"},
        )
