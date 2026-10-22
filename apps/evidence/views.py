"""Class-based views for evidence: upload, detach, authorized download and image preview.

The pattern of every other module: an object is fetched through a selector that applies the *target's* authorization (invisible ->
404), the action goes through the service (forbidden -> 403, wrong state -> 409), and nothing is served without re-checking both.
Files are only ever read through ``FileView``; no URL maps to a storage path.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.views import View

from apps.core.utils import parse_uuid
from apps.executions import policies as exec_policies
from apps.executions import selectors as exec_selectors
from apps.executions.services.evaluation import build_state

from . import selectors, services
from .presentation import panel_for
from .validation import IMAGE_TYPES

INLINE_TYPES = IMAGE_TYPES  # PDFs are always downloaded (never rendered inline in the application's origin)


def _panel_response(request: HttpRequest, execution: Any, item_id: Any, *, error: str = "", status: int = 200) -> HttpResponse:
    execution = exec_selectors.get_execution(request.user, execution.pk)
    if execution is None:
        raise Http404
    state = build_state(execution)
    spec = state.item_for(item_id)
    if spec is None:
        raise Http404
    result = state.evaluation.items[spec.code]
    panel = panel_for(execution, spec, result, state.evidence.get(str(item_id), []), editable=exec_policies.can_work(request.user, execution))
    return render(request, "evidence/_panel.html", {"panel": panel, "error": error}, status=status)


def _is_htmx(request: HttpRequest) -> bool:
    return request.headers.get("HX-Request") == "true"


class EvidenceUploadView(LoginRequiredMixin, View):
    """POST (multipart): ``execution``, ``item``, ``file`` and an optional ``caption`` - the target is *resolved*, never trusted."""

    http_method_names = ["post"]

    def post(self, request: HttpRequest) -> HttpResponse:
        execution = exec_selectors.get_execution(request.user, request.POST.get("execution"))
        item_id = parse_uuid(request.POST.get("item"))
        if execution is None or item_id is None:
            raise Http404
        error, status = "", 200
        upload = request.FILES.get("file")
        too_big = int(request.META.get("CONTENT_LENGTH") or 0) > int(settings.EVIDENCE_MAX_REQUEST_BYTES)
        if upload is None or too_big:
            error, status = ("The file is too large." if too_big else "Choose a file to upload."), 400
        else:
            try:
                services.upload(request.user, target_type="checklist_response", ref={"execution_id": str(execution.pk), "item_id": str(item_id)},
                                filename=upload.name, data=upload.read(int(settings.EVIDENCE_MAX_FILE_BYTES) + 1), caption=request.POST.get("caption", ""))
            except services.EvidenceError as exc:
                error, status = " ".join(exc.messages), exc.http_status
        if _is_htmx(request):
            return _panel_response(request, execution, item_id, error=error, status=200 if not error else 200)
        (messages.error if error else messages.success)(request, error or "File uploaded.")
        return HttpResponseRedirect(reverse("executions:workspace", args=[execution.pk]))


class EvidenceDetachView(LoginRequiredMixin, View):
    http_method_names = ["post"]

    def post(self, request: HttpRequest, pk: Any) -> HttpResponse:
        attachment = selectors.get_attachment(request.user, pk)
        if attachment is None:
            raise Http404
        error = ""
        try:
            services.detach(request.user, attachment, reason=request.POST.get("reason", "Removed by the user"))
        except services.EvidenceError as exc:
            error = " ".join(exc.messages)
        except PermissionDenied:
            if not _is_htmx(request):
                raise
            error = "You do not have permission to remove this file."
        if _is_htmx(request):
            return _panel_response(request, attachment.execution, attachment.item_id, error=error)
        (messages.error if error else messages.success)(request, error or "File removed.")
        return HttpResponseRedirect(reverse("executions:detail", args=[attachment.execution_id]))


class EvidenceFileView(LoginRequiredMixin, View):
    """Serve the bytes of one attachment after re-checking authorization and the file's state.  ``inline`` is for images only."""

    http_method_names = ["get"]
    inline = False

    def get(self, request: HttpRequest, pk: Any) -> HttpResponse:
        attachment = selectors.get_attachment(request.user, pk)
        if attachment is None or not selectors.is_servable(attachment.asset):
            raise Http404  # not visible, quarantined, or still unchecked: indistinguishable
        asset = attachment.asset
        if self.inline and asset.content_type not in INLINE_TYPES:
            raise Http404
        try:
            data = services.read_bytes(asset)
        except services.EvidenceError:
            raise Http404 from None
        services.record_access(request.user, asset, "preview" if self.inline else "download")
        response = HttpResponse(data, content_type=asset.content_type)
        disposition = "inline" if self.inline else "attachment"
        response["Content-Disposition"] = f"{disposition}; filename*=UTF-8''{quote(asset.original_name)}"
        response["X-Content-Type-Options"] = "nosniff"
        response["Cache-Control"] = "private, no-store"
        response["Content-Security-Policy"] = "sandbox; default-src 'none'; style-src 'none'; script-src 'none'"
        response["Cross-Origin-Resource-Policy"] = "same-origin"
        response["Content-Length"] = str(len(data))
        return response


class EvidencePreviewView(EvidenceFileView):
    inline = True
