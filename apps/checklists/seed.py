"""Load a checklist definition from a plain data file (JSON) through the service layer.

Used by the ``seed_audit_checklists`` command; the same function can load any organization's exported or
authored checklist.  Everything goes through ``services`` - so answer types, configuration, options and metadata are
validated exactly as in the builder, and a bad file stops with a clear error instead of leaving a half-built checklist
(the whole load runs in one transaction).

File shape::

    {"code", "name", "category", "minutes", "description", "instructions",
     "sections": [{"code", "name", "description", "instructions", "items": [
         {"code", "question", "answer_type", "config", "required", "options", "metadata", "help_text"}]}],
     "rules": [{"name", "description", "condition", "actions", "enabled"}]}

``rules`` are optional conditional rules (Phase 6): the condition tree and actions reference questions and sections by
code, exactly as the API and the rule builder do.  They are created after the structure and validated as a set (cycles,
conflicts ...) before the version is published.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.db import transaction

from . import services
from .models import ChecklistTemplate

DATA_DIR = Path(__file__).resolve().parent / "seed_data"


def load_data_file(name: str) -> dict[str, Any]:
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


@transaction.atomic
def load_template(
    organization: Any, data: dict[str, Any], *, actor: Any = None, publish: bool = True
) -> tuple[ChecklistTemplate, bool]:
    """Create the template described by ``data``; returns ``(template, created)``.  Idempotent on ``code``."""
    existing = ChecklistTemplate.objects.filter(organization=organization, code=data["code"].upper()).first()
    if existing is not None:
        return existing, False
    category = services.create_category(organization=organization, name=data["category"], actor=actor) if data.get("category") else None
    template = services.create_checklist_template(
        organization=organization, name=data["name"], code=data["code"], actor=actor,
        description=data.get("description", ""), instructions=data.get("instructions", ""),
        estimated_duration_minutes=data.get("minutes"), category=category,
    )
    draft = template.versions.get()
    for section_data in data["sections"]:
        section = services.create_section(
            draft, name=section_data["name"], actor=actor, description=section_data.get("description", ""),
            instructions=section_data.get("instructions", ""), code=section_data.get("code", ""),
        )
        for item in section_data["items"]:
            services.create_item(
                section, question=item["question"], code=item.get("code", ""), actor=actor,
                answer_type=item.get("answer_type", "yes_no"), config=item.get("config"),
                is_required=item.get("required", True), help_text=item.get("help_text", ""),
                options=item.get("options", ()), metadata=item.get("metadata"),
            )
    for rule in data.get("rules", ()):
        services.create_rule(
            draft, name=rule["name"], description=rule.get("description", ""), condition=rule.get("condition"),
            actions=rule["actions"], is_active=rule.get("enabled", True), actor=actor,
        )
    if publish:
        services.publish_checklist_version(draft, actor=actor, change_summary=data.get("change_summary", "Initial version"))
        services.activate_checklist_template(template, actor=actor)
    return template, True
