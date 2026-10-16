"""Validation of ``ChecklistItem.metadata`` - descriptive facts the answer engine ignores.

Allowed keys (anything else is rejected, so the field can never become an unvalidated dumping ground):

* ``points`` - reference points the source checklist assigns to the item (``0-10000``); *not* a score, only a fact
  the later scoring phase may use;
* ``critical`` - the source marks a failure of this item as critical (an automatic fail);
* ``applies_to`` - applicability tags such as country or business-unit codes (``["KSA", "EGP"]``); empty = everywhere;
* ``reference`` - a short source reference (clause, procedure number).
"""

from __future__ import annotations

import re
from typing import Any

from django.core.exceptions import ValidationError

_TAG = re.compile(r"^[A-Z0-9][A-Z0-9_.-]{0,15}$")
ALLOWED_KEYS = ("points", "critical", "applies_to", "reference")


def clean_metadata(raw: Any) -> dict[str, Any]:
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict):
        raise ValidationError({"metadata": "Metadata must be an object."})
    unknown = sorted(set(raw) - set(ALLOWED_KEYS))
    if unknown:
        raise ValidationError({"metadata": "Unknown metadata: " + ", ".join(unknown) + "."})
    out: dict[str, Any] = {}
    if raw.get("points") not in (None, ""):
        points = raw["points"]
        if isinstance(points, bool) or not isinstance(points, int) or not 0 <= points <= 10000:
            raise ValidationError({"metadata": "“points” must be a whole number from 0 to 10000."})
        out["points"] = points
    if raw.get("critical") not in (None, ""):
        if not isinstance(raw["critical"], bool):
            raise ValidationError({"metadata": "“critical” must be true or false."})
        if raw["critical"]:
            out["critical"] = True
    tags = raw.get("applies_to")
    if tags:
        if not isinstance(tags, list) or len(tags) > 30 or not all(isinstance(t, str) and _TAG.match(t.strip().upper()) for t in tags):
            raise ValidationError({"metadata": "“applies_to” must be a list of short codes (letters, digits, - _ .)."})
        out["applies_to"] = sorted({t.strip().upper() for t in tags})
    if raw.get("reference"):
        if not isinstance(raw["reference"], str) or len(raw["reference"].strip()) > 120:
            raise ValidationError({"metadata": "“reference” must be text of at most 120 characters."})
        out["reference"] = raw["reference"].strip()
    return out
