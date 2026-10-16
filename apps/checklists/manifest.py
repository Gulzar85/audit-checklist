"""The canonical *manifest* of a checklist version, its integrity hash, and verification.

A published version is the relational rows (sections, items, options, rules) - that is the source of truth and what
every later phase reads.  The manifest is a **derived, deterministic description** of those rows used for three things only:

* **integrity** - a SHA-256 of the canonical manifest is sealed on the version when it is published, and can be re-computed
  at any time to detect that the definition no longer matches what was published (``verify_version_integrity``);
* **optimistic concurrency** - the same hash of a *draft* is the token a publish request presents (``expected_hash``), so a
  draft that changed after it was validated is refused instead of published unreviewed;
* **comparison** - ``comparison.compare_manifests`` diffs two manifests by stable codes (never by primary key).

The hash is an integrity *aid*, not a substitute for access control or for the immutable rows (see
``docs/checklist-versioning.md``); a stored hash is never trusted - it is always re-derived before it is believed.

Canonical form (schema 1)
-------------------------
``{"schema": 1, "sections": [...], "rules": [...]}`` serialised as JSON with sorted keys, no insignificant whitespace and
UTF-8 (no ``\\u`` escaping).  Lists keep **definition order** (sections, items, options and rules by ``sort_order``), so a
reorder changes the hash; ``sort_order`` numbers themselves, primary keys, timestamps, authorship and history are excluded
(they are volatile or identify rows, not meaning).  Included per level:

* section: ``code, name, description, instructions, is_required``
* item: ``code, question, description, help_text, instructions, answer_type, config, metadata, is_required, is_active``
* option: ``value, label, is_active``
* rule: ``code, name, description, condition, actions, is_active``

Template-level descriptive metadata (name, description, general instructions, category, estimated duration) is *not* part of
the version definition and may be edited later; everything that changes how a question is asked, validated, shown or
required is.

Schema evolution: the hash is stored as ``sha256:v<schema>:<hex>``.  A new schema adds a builder to ``_BUILDERS`` and bumps
``CURRENT_SCHEMA``; hashes sealed under an older schema keep verifying with the old builder, and are re-sealed only by an
explicit maintenance command.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from . import selectors

CURRENT_SCHEMA = 1
ALGORITHM = "sha256"

_SECTION_FIELDS = ("code", "name", "description", "instructions", "is_required")
_ITEM_FIELDS = ("code", "question", "description", "help_text", "instructions", "answer_type", "config", "metadata",
                "is_required", "is_active")
_OPTION_FIELDS = ("value", "label", "is_active")
_RULE_FIELDS = ("code", "name", "description", "condition", "actions", "is_active")


def _pick(obj: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    return {f: getattr(obj, f) for f in fields}


def manifest_from_rows(sections: Any, rules: Any) -> dict[str, Any]:
    """The schema-1 manifest of already-loaded rows (``sections`` with ``items`` and ``options`` prefetched, in order)."""
    out_sections = []
    for section in sections:
        out = _pick(section, _SECTION_FIELDS)
        out["items"] = []
        for item in section.items.all():
            entry = _pick(item, _ITEM_FIELDS)
            entry["options"] = [_pick(o, _OPTION_FIELDS) for o in item.options.all()]
            out["items"].append(entry)
        out_sections.append(out)
    return {"schema": 1, "sections": out_sections, "rules": [_pick(r, _RULE_FIELDS) for r in rules]}


def build_manifest(version: Any) -> dict[str, Any]:
    """The current manifest of ``version`` (four queries, whatever its size)."""
    return manifest_from_rows(selectors.get_sections_for_version(version), version.rules.order_by("sort_order"))


def canonical_json(manifest: dict[str, Any]) -> str:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def hash_manifest(manifest: dict[str, Any]) -> str:
    digest = hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()
    return f"{ALGORITHM}:v{manifest['schema']}:{digest}"


def manifest_hash(version: Any) -> str:
    """The hash of the version's *current* rows (the optimistic-concurrency token of a draft)."""
    return hash_manifest(build_manifest(version))


_BUILDERS = {1: build_manifest}


@dataclass(frozen=True)
class IntegrityResult:
    """Outcome of verifying a version against the hash sealed at publication."""

    status: str  # verified | mismatch | unsealed | unverifiable | draft
    expected: str = ""
    actual: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "verified"

    @property
    def label(self) -> str:
        return {
            "verified": "Integrity verified",
            "mismatch": "Definition differs from what was published",
            "unsealed": "Not sealed (published before integrity hashes existed)",
            "unverifiable": "Sealed with an unknown manifest schema",
            "draft": "A draft has no seal yet",
        }[self.status]


def verify_version_integrity(version: Any) -> IntegrityResult:
    """Re-derive the manifest hash and compare it with the one sealed at publication (never trusts the stored value)."""
    if version.status == "draft":
        return IntegrityResult("draft")
    stored = version.integrity_hash
    if not stored:
        return IntegrityResult("unsealed")
    parts = stored.split(":")
    if len(parts) != 3 or parts[0] != ALGORITHM or not parts[1].startswith("v") or not parts[1][1:].isdigit():
        return IntegrityResult("unverifiable", expected=stored)
    builder = _BUILDERS.get(int(parts[1][1:]))
    if builder is None:
        return IntegrityResult("unverifiable", expected=stored)
    actual = hash_manifest(builder(version))
    return IntegrityResult("verified" if actual == stored else "mismatch", expected=stored, actual=actual)
