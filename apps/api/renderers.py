"""API renderers."""

from __future__ import annotations

from typing import Any

from rest_framework.renderers import JSONRenderer


class EnvelopeJSONRenderer(JSONRenderer):
    """Wrap successful responses in the standard ``{"data": ..., "meta": ...}``.

    Error responses are already shaped by the exception handler and pass
    through untouched. Views may opt out by setting ``response.use_envelope =
    False``.
    """

    def render(
        self,
        data: Any,
        accepted_media_type: str | None = None,
        renderer_context: dict[str, Any] | None = None,
    ) -> bytes:
        renderer_context = renderer_context or {}
        response = renderer_context.get("response")

        if response is None or not getattr(response, "use_envelope", True):
            return super().render(data, accepted_media_type, renderer_context)

        if response.status_code >= 400:
            payload = data
        else:
            payload = self._envelope(data)
        return super().render(payload, accepted_media_type, renderer_context)

    @staticmethod
    def _envelope(data: Any) -> dict[str, Any]:
        if isinstance(data, dict) and {"count", "results"} <= set(data.keys()):
            meta = {key: data.get(key) for key in ("count", "next", "previous")}
            return {"data": data.get("results", []), "meta": meta}
        return {"data": data, "meta": {}}
