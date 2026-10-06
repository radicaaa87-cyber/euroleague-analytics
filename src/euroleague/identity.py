"""Cross-competition athlete identity matching.

Source identifiers remain untouched. This module only decides whether two
source-native person records are safe to link to the same canonical athlete.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class IdentityDecision(StrEnum):
    AUTO_LINK = "auto_link"
    REVIEW = "review"
    REJECT = "reject"


@dataclass(frozen=True)
class SourcePerson:
    source: str
    source_player_id: str
    display_name: str
    birth_date: date | None
    height_cm: int | None
    country_code: str | None
    team_code: str | None


@dataclass(frozen=True)
class IdentityEvidence:
    name_match: bool
    birth_date_match: bool | None
    height_difference_cm: int | None
    country_match: bool | None
    team_match: bool | None


@dataclass(frozen=True)
class IdentityMatch:
    left: SourcePerson
    right: SourcePerson
    status: IdentityDecision
    reason: str
    evidence: IdentityEvidence


def _ascii_upper(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return normalized.encode("ascii", "ignore").decode("ascii").upper()


def normalized_person_name(value: str) -> str:
    """Normalize display copy for comparison, never as an identifier."""
    text = _ascii_upper(value.strip())
    text = re.sub(r"[^A-Z0-9, ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if "," in text:
        surname, given = (part.strip() for part in text.split(",", 1))
        text = f"{given} {surname}".strip()
    return text


def _optional_equal(left: str | None, right: str | None) -> bool | None:
    if left is None or right is None:
        return None
    return left.strip().upper() == right.strip().upper()


def decide_identity_match(left: SourcePerson, right: SourcePerson) -> IdentityMatch:
    """Decide whether two source records may share one canonical athlete."""
    name_match = normalized_person_name(left.display_name) == normalized_person_name(
        right.display_name
    )
    birth_date_match = (
        None
        if left.birth_date is None or right.birth_date is None
        else left.birth_date == right.birth_date
    )
    height_difference_cm = (
        None
        if left.height_cm is None or right.height_cm is None
        else abs(left.height_cm - right.height_cm)
    )
    evidence = IdentityEvidence(
        name_match=name_match,
        birth_date_match=birth_date_match,
        height_difference_cm=height_difference_cm,
        country_match=_optional_equal(left.country_code, right.country_code),
        team_match=_optional_equal(left.team_code, right.team_code),
    )

    if birth_date_match is False:
        return IdentityMatch(
            left,
            right,
            IdentityDecision.REJECT,
            "Birth date conflicts between sources.",
            evidence,
        )
    if not name_match:
        return IdentityMatch(
            left,
            right,
            IdentityDecision.REVIEW,
            "Names do not normalize to the same value.",
            evidence,
        )
    if birth_date_match is None:
        return IdentityMatch(
            left,
            right,
            IdentityDecision.REVIEW,
            "Birth date is required for automatic cross-source linking.",
            evidence,
        )
    if height_difference_cm is not None and height_difference_cm > 3:
        return IdentityMatch(
            left,
            right,
            IdentityDecision.REVIEW,
            "Height differs by more than 3 cm between sources.",
            evidence,
        )
    return IdentityMatch(
        left,
        right,
        IdentityDecision.AUTO_LINK,
        "Name and birth date agree; available biography fields do not conflict.",
        evidence,
    )
