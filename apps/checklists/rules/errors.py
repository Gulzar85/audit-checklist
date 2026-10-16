"""Issue and error types shared by the rule validators."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Issue:
    """One validation finding.  ``level`` is ``error`` (blocks saving/publishing) or ``warning`` (informational)."""

    level: str
    code: str
    message: str
    rule: str = ""  # rule code, when the finding belongs to one
    path: str = ""  # e.g. ``condition.conditions[1].value`` or ``actions[0].target``

    def as_dict(self) -> dict[str, str]:
        out = {"level": self.level, "code": self.code, "message": self.message}
        if self.rule:
            out["rule"] = self.rule
        if self.path:
            out["path"] = self.path
        return out


class RuleSchemaError(Exception):
    """A rule's condition/actions are malformed; carries every issue found (not just the first)."""

    def __init__(self, issues: list[Issue]) -> None:
        super().__init__("; ".join(i.message for i in issues))
        self.issues = issues
