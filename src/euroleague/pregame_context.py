"""Pregame web/news context classification and leakage guards.

This layer is intentionally separate from the historical statistical model.
Until enough timestamped context has been archived, the structured signals are
used for forward role/minutes/FGA scenarios rather than retrofitted into old
blind tests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlparse

CONTEXT_WINDOW_HOURS = 72

_OUT = (
    "ruled out",
    "will not play",
    "won't play",
    "unavailable",
    "sidelined",
    "injured",
    "injury",
    "suspended",
    "descartado",
    "no jugará",
    "no jugara",
    "lesionado",
    "lesión",
    "lesion",
    "sancionado",
    "baja",
)
_DOUBT = (
    "questionable",
    "doubtful",
    "game-time decision",
    "uncertain",
    "duda",
    "dudoso",
    "entre algodones",
)
_RETURN = (
    "returns",
    "returning",
    "cleared to play",
    "available again",
    "back in the lineup",
    "recovered",
    "vuelve",
    "regresa",
    "disponible",
    "alta médica",
    "alta medica",
)
_ROLE_UP = (
    "will start",
    "starting lineup",
    "bigger role",
    "increased role",
    "more minutes",
    "starter",
    "titular",
    "más minutos",
    "mas minutos",
    "mayor protagonismo",
)
_ROLE_DOWN = (
    "minutes restriction",
    "limited minutes",
    "reduced role",
    "moved to the bench",
    "bench role",
    "suplente",
    "minutos limitados",
    "restricción de minutos",
    "restriccion de minutos",
)
_ROTATION = (
    "rotation",
    "lineup change",
    "rotation change",
    "rotación",
    "rotacion",
    "cambio de quinteto",
)
_ROSTER = (
    "signed",
    "signing",
    "released",
    "waived",
    "transfer",
    "fichaje",
    "ficha por",
    "rescinde",
)


@dataclass(frozen=True)
class ContextClassification:
    event_type: str
    role_direction: int
    severity: float
    source_confidence: float
    role_impact_score: float


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def classify_context_text(
    headline: str,
    summary: str = "",
    *,
    source_name: str = "",
    publisher_url: str = "",
    team_name: str = "",
) -> ContextClassification:
    """Convert one article snippet into a conservative structured role signal."""
    text = re.sub(r"\s+", " ", f"{headline} {summary}").casefold()

    if _contains_any(text, _OUT):
        event_type, direction, severity = "availability_out", -1, 1.0
    elif _contains_any(text, _DOUBT):
        event_type, direction, severity = "availability_doubt", -1, 0.60
    elif _contains_any(text, _RETURN):
        event_type, direction, severity = "return", 1, 0.70
    elif _contains_any(text, _ROLE_DOWN):
        event_type, direction, severity = "role_down", -1, 0.60
    elif _contains_any(text, _ROLE_UP):
        event_type, direction, severity = "role_up", 1, 0.60
    elif _contains_any(text, _ROTATION):
        event_type, direction, severity = "rotation_change", 0, 0.45
    elif _contains_any(text, _ROSTER):
        event_type, direction, severity = "roster_change", 0, 0.45
    else:
        event_type, direction, severity = "other", 0, 0.0

    confidence = source_confidence(
        source_name=source_name,
        publisher_url=publisher_url,
        team_name=team_name,
    )
    impact = round(direction * severity * confidence, 5)
    return ContextClassification(
        event_type=event_type,
        role_direction=direction,
        severity=severity,
        source_confidence=confidence,
        role_impact_score=impact,
    )


def source_confidence(
    *,
    source_name: str = "",
    publisher_url: str = "",
    team_name: str = "",
) -> float:
    """Score provenance, not whether the article's claim is ultimately true."""
    host = urlparse(publisher_url).netloc.casefold().removeprefix("www.")
    if host.endswith("euroleaguebasketball.net") or host.endswith("acb.com"):
        return 0.95

    source = re.sub(r"[^a-z0-9]+", " ", source_name.casefold()).strip()
    team = re.sub(r"[^a-z0-9]+", " ", team_name.casefold()).strip()
    if team and source and (team in source or source in team):
        return 0.90
    return 0.65


def in_pregame_window(
    published_at: datetime,
    tipoff_at: datetime,
    *,
    hours: int = CONTEXT_WINDOW_HOURS,
) -> bool:
    """True only for evidence known in the requested pre-tipoff window."""
    if published_at.tzinfo is None or tipoff_at.tzinfo is None:
        raise ValueError("published_at and tipoff_at must be timezone-aware.")
    return tipoff_at - timedelta(hours=hours) <= published_at < tipoff_at


def role_query(player_name: str, team_name: str) -> str:
    terms = (
        "injury OR injured OR out OR doubtful OR return OR role OR minutes OR "
        "baja OR lesionado OR duda OR vuelve OR titular OR minutos"
    )
    return f'"{player_name}" "{team_name}" ({terms})'


def team_query(team_name: str) -> str:
    terms = (
        "injury OR injured OR out OR doubtful OR rotation OR lineup OR "
        "baja OR lesionado OR duda OR rotación OR quinteto"
    )
    return f'"{team_name}" ({terms})'
