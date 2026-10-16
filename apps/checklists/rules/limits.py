"""Configurable limits of the rules engine (override any key in ``settings.CHECKLIST_RULE_LIMITS``).

The limits bound the *work* a rule set can cause - a rule can never make evaluation unbounded - and the size of what
an API caller may submit for simulation.
"""

from __future__ import annotations

from django.conf import settings

DEFAULTS: dict[str, int] = {
    "max_rules_per_version": 200,
    "max_conditions_per_rule": 25,
    "max_group_depth": 3,  # the root group is depth 1
    "max_actions_per_rule": 10,
    "max_operand_items": 50,
    "max_text_operand_length": 200,
    "max_message_length": 300,
    "max_instruction_length": 500,
    "max_simulation_answers": 500,
    "max_simulation_value_chars": 5000,
    "max_simulation_payload_bytes": 200_000,
}


def limit(name: str) -> int:
    return int(getattr(settings, "CHECKLIST_RULE_LIMITS", {}).get(name, DEFAULTS[name]))


def all_limits() -> dict[str, int]:
    return {name: limit(name) for name in DEFAULTS}
