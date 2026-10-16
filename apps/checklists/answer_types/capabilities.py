"""What an answer type can do.  Features ask the registry; nothing else hard-codes these facts."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Capabilities:
    #: The author must define options (single / multiple choice).
    requires_options: bool = False
    #: The type uses option rows at all (fixed or author-defined).
    supports_options: bool = False
    #: Options are created by the platform and cannot be edited (yes/no, yes/no/n-a).
    has_fixed_options: bool = False
    supports_comment: bool = True
    supports_evidence: bool = True
    #: The answer *can* feed a scoring engine (Phase 15 decides how).  It never scores by itself.
    supports_score: bool = False
    supports_rules: bool = True
    #: The normalized value is one scalar (not a list, mapping or file reference).
    produces_scalar: bool = True
    supports_numeric_validation: bool = False
    #: The answer references uploaded files (stored by the later evidence phase).
    is_media: bool = False

    def as_dict(self) -> dict[str, bool]:
        return asdict(self)
