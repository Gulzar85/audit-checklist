"""Roles, permissions and member access screens (``/access/``).

Every view resolves the organization from the *validated active organization* (never a URL or
form id), fetches objects through organization-scoped selectors (out of scope -> 404) and then
asks a policy (visible but forbidden -> 403).  Writes go through ``apps.permissions.services``.
"""

from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views import View
from django.views.generic import TemplateView

from apps.core.forms import add_validation_error
from apps.organizations.models import OrganizationMembership

from . import policies, selectors, services
from .authorization import get_access, has_permission
from .forms import AssignRoleForm, MembershipStatusForm, RoleForm
from .mixins import OrganizationAccessMixin, PermissionRequiredMixin
from .models import MembershipRole, Role
from .registry import P


def _error_text(exc: ValidationError) -> str:
    return " ".join(exc.messages)


class RoleListView(PermissionRequiredMixin, TemplateView):
    required_permission = P.ROLE_VIEW
    template_name = "permissions/role_list.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        user, organization = self.request.user, self.organization
        context.update(
            organization=organization,
            roles=list(selectors.roles_for_display(organization)),
            can_create=policies.can_create_role(user, organization),
        )
        return context


class RoleFormMixin(PermissionRequiredMixin):
    template_name = "permissions/role_form.html"

    def build_context(self, form: RoleForm, role: Role | None) -> dict[str, Any]:
        selected = set(form["permissions"].value() or [])
        return {
            "form": form,
            "role": role,
            "organization": self.organization,
            "matrix": selectors.permission_matrix(selected),
            "editable": True,
        }


class RoleCreateView(RoleFormMixin, TemplateView):
    required_permission = P.ROLE_CREATE

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        form = RoleForm(actor=request.user, organization=self.organization)
        return self.render_to_response(self.build_context(form, None))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        form = RoleForm(request.POST, actor=request.user, organization=self.organization)
        if form.is_valid():
            codes = form.cleaned_data["permissions"]
            if not policies.can_create_role(request.user, self.organization, codes):
                raise PermissionDenied
            try:
                role = services.create_role(
                    organization=self.organization,
                    name=form.cleaned_data["name"],
                    description=form.cleaned_data["description"],
                    permissions=codes,
                    actor=request.user,
                )
            except ValidationError as exc:
                add_validation_error(form, exc)
            else:
                messages.success(request, f"Role “{role.name}” created.")
                return HttpResponseRedirect(reverse("access:role_detail", args=[role.pk]))
        return self.render_to_response(self.build_context(form, None))


class RoleObjectMixin(PermissionRequiredMixin):
    """Loads ``self.role`` from the roles visible in the active organization (else 404)."""

    required_permission = P.ROLE_VIEW

    def get_role(self) -> Role:
        role = selectors.roles_for_display(self.organization).filter(pk=self.kwargs["pk"]).first()
        if role is None:
            raise Http404
        return role

    @property
    def role(self) -> Role:
        if not hasattr(self, "_role"):
            self._role = self.get_role()
        return self._role


class RoleDetailView(RoleObjectMixin, TemplateView):
    template_name = "permissions/role_detail.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        role, user = self.role, self.request.user
        selected = set(policies.role_permission_codes(role))
        context.update(
            role=role,
            organization=self.organization,
            matrix=selectors.permission_matrix(selected),
            editable=False,
            can_edit=policies.can_edit_role(user, role),
            can_deactivate=policies.can_deactivate_role(user, role),
            permission_total=len(selected),
        )
        return context


class RoleUpdateView(RoleObjectMixin, RoleFormMixin, TemplateView):
    required_permission = P.ROLE_UPDATE

    def _guard(self) -> Role:
        role = self.role
        if not policies.can_edit_role(self.request.user, role):
            raise PermissionDenied
        return role

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        role = self._guard()
        form = RoleForm(role=role, actor=request.user, organization=self.organization)
        return self.render_to_response(self.build_context(form, role))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        role = self._guard()
        form = RoleForm(request.POST, role=role, actor=request.user, organization=self.organization)
        if form.is_valid():
            codes = form.cleaned_data["permissions"]
            if not policies.can_edit_role(request.user, role, codes):
                raise PermissionDenied
            try:
                services.update_role(
                    role, actor=request.user, name=form.cleaned_data["name"],
                    description=form.cleaned_data["description"],
                )
                services.set_role_permissions(role, codes, actor=request.user)
            except ValidationError as exc:
                add_validation_error(form, exc)
            else:
                messages.success(request, f"Role “{role.name}” updated.")
                return HttpResponseRedirect(reverse("access:role_detail", args=[role.pk]))
        return self.render_to_response(self.build_context(form, role))


class RoleStatusView(RoleObjectMixin, View):
    """Activate / deactivate a custom role (POST only)."""

    http_method_names = ["post"]

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        role = self.role
        activate = request.POST.get("action") == "activate"
        if not policies.can_deactivate_role(request.user, role):
            raise PermissionDenied
        try:
            (services.activate_role if activate else services.deactivate_role)(role, actor=request.user)
        except ValidationError as exc:
            messages.error(request, _error_text(exc))
        else:
            messages.success(request, f"Role “{role.name}” {'reactivated' if activate else 'deactivated'}.")
        return HttpResponseRedirect(reverse("access:role_detail", args=[role.pk]))


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------
class MemberListView(PermissionRequiredMixin, TemplateView):
    required_permission = P.USER_VIEW
    template_name = "permissions/member_list.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        members = list(selectors.members_for_display(self.organization))
        query = (self.request.GET.get("q") or "").strip().lower()
        if query:
            members = [m for m in members if query in m.user.email.lower() or query in (m.user.display_name or "").lower()]
        status = self.request.GET.get("status") or ""
        if status:
            members = [m for m in members if m.status == status]
        context.update(
            organization=self.organization, members=members, query=query, status=status,
            statuses=OrganizationMembership.Status.choices,
            can_view_access=has_permission(self.request.user, P.ROLE_ASSIGN, organization=self.organization)
            or has_permission(self.request.user, P.USER_VIEW, organization=self.organization),
        )
        return context


class MembershipObjectMixin(PermissionRequiredMixin):
    required_permission = P.USER_VIEW

    @property
    def membership(self) -> OrganizationMembership:
        if not hasattr(self, "_membership"):
            self._membership = get_object_or_404(
                OrganizationMembership.objects.select_related("user", "organization"),
                pk=self.kwargs["pk"],
                organization=self.organization,
            )
        return self._membership


class MemberAccessView(MembershipObjectMixin, TemplateView):
    template_name = "permissions/member_access.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        membership, actor = self.membership, self.request.user
        if not policies.can_view_access(actor, membership):
            raise PermissionDenied
        assignments = list(
            membership.role_assignments.filter(is_active=True).select_related("role", "location").order_by("role__name")
        )
        rows = [
            {"assignment": a, "role_active": a.role.is_active, **selectors.describe_assignment(a),
             "can_remove": policies.can_remove_role(actor, a)}
            for a in assignments
        ]
        can_assign = get_access(actor, self.organization).holds(P.ROLE_ASSIGN)  # any scope; the policy narrows it
        context.update(
            membership=membership,
            target=membership.user,
            organization=self.organization,
            rows=rows,
            summary=selectors.access_summary(membership),
            assign_form=kwargs.get("assign_form") or (
                AssignRoleForm(membership=membership, actor=actor) if can_assign else None
            ),
            can_assign=can_assign,
            can_change_status=policies.can_manage_membership_status(actor, membership),
            status_choices=OrganizationMembership.Status.choices,
            is_self=membership.user_id == actor.pk,
        )
        return context


class MemberAssignRoleView(MembershipObjectMixin, View):
    http_method_names = ["post"]

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        membership = self.membership
        if not policies.can_view_access(request.user, membership):
            raise PermissionDenied
        form = AssignRoleForm(request.POST, membership=membership, actor=request.user)
        if not form.is_valid():
            if form.denied:  # a forged role/scope the actor may not grant: 403, not a form error
                raise PermissionDenied
            for errors in form.errors.values():
                for error in errors:
                    messages.error(request, error)
        else:
            data = form.cleaned_data
            try:
                services.assign_role(
                    membership, data["role"], scope_type=data["scope_type"], location=data["location"],
                    actor=request.user,
                )
            except ValidationError as exc:
                messages.error(request, _error_text(exc))
            else:
                messages.success(request, f"Granted {data['role'].name} to {membership.user}.")
        return HttpResponseRedirect(reverse("access:member_access", args=[membership.pk]))


class MemberRemoveRoleView(PermissionRequiredMixin, View):
    http_method_names = ["post"]
    required_permission = P.USER_VIEW

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        assignment = get_object_or_404(
            MembershipRole.objects.select_related("membership__user", "role", "location"),
            pk=kwargs["pk"],
            membership__organization=self.organization,
        )
        if not policies.can_remove_role(request.user, assignment):
            raise PermissionDenied
        services.remove_role(assignment, actor=request.user)
        messages.success(request, f"Removed {assignment.role.name} from {assignment.membership.user}.")
        return HttpResponseRedirect(reverse("access:member_access", args=[assignment.membership_id]))


class MemberStatusView(MembershipObjectMixin, View):
    http_method_names = ["post"]

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        membership = self.membership
        if not policies.can_manage_membership_status(request.user, membership):
            raise PermissionDenied
        form = MembershipStatusForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Unknown membership status.")
        else:
            try:
                services.change_membership_status(membership, form.cleaned_data["status"], actor=request.user)
            except ValidationError as exc:
                messages.error(request, _error_text(exc))
            else:
                messages.success(request, f"Membership of {membership.user} is now {form.cleaned_data['status']}.")
        return HttpResponseRedirect(reverse("access:member_access", args=[membership.pk]))


class AccessSummaryView(OrganizationAccessMixin, TemplateView):
    """The signed-in user's own access (always allowed: you may see what *you* can do)."""

    template_name = "permissions/my_access.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        membership = OrganizationMembership.objects.filter(user=self.request.user, organization=self.organization).first()
        context.update(organization=self.organization, membership=membership)
        if membership is not None:
            context["summary"] = selectors.access_summary(membership)
        return context
