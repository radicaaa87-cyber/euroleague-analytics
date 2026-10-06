"""Bookmaker player-points parsing and conservative identity resolution.

The parser intentionally separates source extraction from identity linking. A row
is valuable even when the athlete cannot be linked safely: raw source text,
bookmaker player spelling, team label, line, odds and provenance are retained.

Historical collection time must never become a model feature. Document/offer
dates are inferred from the source itself or archive capture metadata.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time
from difflib import SequenceMatcher

SPACE_RE = re.compile(r"\s+")
DATE_RE = re.compile(r"(?<!\d)(\d{1,2})[._/-](\d{1,2})(?:[._/-](\d{2,4}))?(?!\d)")
COMPACT_DATE_RE = re.compile(r"(?:dopuna|ponuda)[^0-9]{0,4}(\d{2})(\d{2})(?!\d)", re.I)
ROW_RE = re.compile(
    r"^(?:(?P<day>Pon|Uto|Sre|Čet|Cet|Pet|Sub|Ned)\s+)?"
    r"(?P<clock>\d{1,2}:\d{2})\s+"
    r"(?P<code>\d{3,6})\s+"
    r"(?P<body>.+?)\s+"
    r"(?P<line>\d{1,2}[.,]\d)\s+"
    r"(?P<odd1>\d{1,2}[.,]\d{2})\s+"
    r"(?P<odd2>\d{1,2}[.,]\d{2})(?:\s|$)"
)

ROW_NO_CODE_RE = re.compile(
    r"^(?:(?P<day>Pon|Uto|Sre|Čet|Cet|Pet|Sub|Ned)\s+)?"
    r"(?P<clock>\d{1,2}:\d{2})\s+"
    r"(?P<body>.+?)\s+"
    r"(?P<line>\d{1,2}[.,]\d)\s+"
    r"(?P<odd1>\d{1,2}[.,]\d{2})\s+"
    r"(?P<odd2>\d{1,2}[.,]\d{2})(?:\s|$)"
)

STARBET_POINTS_MARKERS = (
    "euroleague player points",
    "euroleague players",
)
STARBET_OTHER_MARKERS = (
    "player three",
    "player rebound",
    "player assist",
    "player blocks",
    "player steals",
)
MOZZART_SECTION_MARKERS = ("kosarka igraci",)
MOZZART_POINTS_MARKER = "broj poena igraca na mecu"
MOZZART_EUROLEAGUE_TEAM_TAGS = {
    "Alb",
    "Arm",
    "Asv",
    "Baj",
    "Bar",
    "Bas",
    "Bes",
    "Crv",
    "Dub",
    "Efe",
    "Fen",
    "Hap",
    "Mak",
    "Mon",
    "Oli",
    "Pan",
    "Par",
    "Prz",
    "Rea",
    "Val",
    "Vir",
    "Žal",
}
GENERIC_EUROLEAGUE_MARKERS = ("evroliga", "euroleague")
GENERIC_PLAYER_POINTS_MARKERS = (
    "poeni igraca",
    "broj poena igraca",
    "player points",
    "players points",
)
GENERIC_OTHER_PROP_MARKERS = (
    "skok",
    "asist",
    "trojk",
    "three",
    "rebound",
    "assist",
    "blok",
    "steal",
)


@dataclass(frozen=True)
class AthleteCandidate:
    athlete_id: str
    display_name: str


@dataclass(frozen=True)
class IdentityMatch:
    athlete_id: str | None
    player_name_raw: str | None
    player_name_normalized: str | None
    team_name_raw: str | None
    method: str | None
    confidence: float | None


@dataclass(frozen=True)
class ParsedOffer:
    bookmaker: str
    page_number: int
    event_time_local: time
    source_event_code: str | None
    participant_text: str
    points_line: float
    under_odds: float
    over_odds: float
    row_text: str

    @property
    def row_sha256(self) -> str:
        payload = f"{self.bookmaker}|{self.page_number}|{self.source_event_code}|{self.row_text}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _ascii(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_name(value: str) -> str:
    value = _ascii(value).lower().replace("-", " ")
    value = re.sub(r"[^a-z0-9.' ]+", " ", value)
    value = value.replace("\u2019", "'")
    return SPACE_RE.sub(" ", value).strip()


def _heading_key(value: str) -> str:
    return normalize_name(value).replace(".", "")


def _decimal(value: str) -> float:
    return float(value.replace(",", "."))


def _parse_clock(value: str) -> time:
    hour, minute = (int(part) for part in value.split(":", 1))
    return time(hour=hour, minute=minute)


def infer_document_date(
    source_url: str,
    *,
    title: str | None = None,
    capture_at: datetime | None = None,
) -> date | None:
    """Infer a bookmaker document date without using collection time."""
    text = urllib.parse.unquote(source_url)
    if title:
        text = f"{title} {text}"

    for match in DATE_RE.finditer(text):
        day, month, year_raw = match.groups()
        year = int(year_raw) if year_raw else None
        if year is not None and year < 100:
            year += 2000
        if year is None and capture_at is not None:
            year = capture_at.year
        if year is None:
            continue
        try:
            return date(year, int(month), int(day))
        except ValueError:
            continue

    compact = COMPACT_DATE_RE.search(text)
    if compact and capture_at is not None:
        day, month = compact.groups()
        try:
            return date(capture_at.year, int(month), int(day))
        except ValueError:
            return None
    return None


def _candidate_variants(display_name: str) -> set[str]:
    normalized = normalize_name(display_name)
    tokens = normalized.split()
    if not tokens:
        return set()

    variants = {normalized}
    if len(tokens) >= 2:
        variants.add(" ".join([tokens[-1], *tokens[:-1]]))
        variants.add(" ".join(reversed(tokens)))

        first = tokens[0]
        surname = " ".join(tokens[1:])
        compact_surname = surname.replace(" ", "")
        for width in range(1, min(3, len(first)) + 1):
            prefix = first[:width]
            variants.add(f"{prefix}.{surname}")
            variants.add(f"{prefix}.{compact_surname}")
    return {item.strip() for item in variants if item.strip()}


def _best_prefix_split(
    participant_text: str,
    candidates: Iterable[AthleteCandidate],
) -> tuple[AthleteCandidate, int, str, float] | None:
    raw_tokens = participant_text.split()
    if len(raw_tokens) < 2:
        return None

    exact_matches: list[tuple[AthleteCandidate, int, str, float]] = []
    fuzzy_matches: list[tuple[AthleteCandidate, int, str, float]] = []

    for split in range(1, min(6, len(raw_tokens)) + 1):
        prefix = normalize_name(" ".join(raw_tokens[:split]))
        for candidate in candidates:
            variants = _candidate_variants(candidate.display_name)
            if prefix in variants:
                exact_matches.append((candidate, split, prefix, 1.0))
                continue
            best = max(
                (SequenceMatcher(None, prefix, variant).ratio() for variant in variants),
                default=0.0,
            )
            if best >= 0.92:
                fuzzy_matches.append((candidate, split, prefix, best))

    if exact_matches:
        exact_matches.sort(key=lambda item: (item[1], len(item[2])), reverse=True)
        return exact_matches[0]

    if not fuzzy_matches:
        return None

    fuzzy_matches.sort(key=lambda item: item[3], reverse=True)
    best = fuzzy_matches[0]
    second = fuzzy_matches[1][3] if len(fuzzy_matches) > 1 else 0.0
    if best[3] - second < 0.03:
        return None
    return best


def resolve_participant(
    participant_text: str,
    candidates: Iterable[AthleteCandidate],
    *,
    bookmaker: str,
) -> IdentityMatch:
    """Resolve source player text conservatively and keep unresolved rows usable."""
    candidates = list(candidates)

    if bookmaker == "mozzart":
        raw_tokens = participant_text.split()
        if len(raw_tokens) >= 2:
            team_name = raw_tokens[-1]
            player_text = " ".join(raw_tokens[:-1])
            match = _best_prefix_split(player_text, candidates)
            if match is not None:
                candidate, _, _, score = match
                return IdentityMatch(
                    athlete_id=candidate.athlete_id,
                    player_name_raw=player_text,
                    player_name_normalized=normalize_name(player_text),
                    team_name_raw=team_name,
                    method="name_abbreviation" if score < 1 else "name_variant_exact",
                    confidence=round(score, 4),
                )
            return IdentityMatch(
                athlete_id=None,
                player_name_raw=player_text,
                player_name_normalized=normalize_name(player_text),
                team_name_raw=team_name,
                method=None,
                confidence=None,
            )

    match = _best_prefix_split(participant_text, candidates)
    if match is None:
        return IdentityMatch(
            athlete_id=None,
            player_name_raw=None,
            player_name_normalized=None,
            team_name_raw=None,
            method=None,
            confidence=None,
        )

    candidate, split, _, score = match
    raw_tokens = participant_text.split()
    player_text = " ".join(raw_tokens[:split])
    team_text = " ".join(raw_tokens[split:]).strip() or None
    return IdentityMatch(
        athlete_id=candidate.athlete_id,
        player_name_raw=player_text,
        player_name_normalized=normalize_name(player_text),
        team_name_raw=team_text,
        method="name_fuzzy" if score < 1 else "name_variant_exact",
        confidence=round(score, 4),
    )


def _parse_row(
    line: str,
    *,
    bookmaker: str,
    page_number: int,
    odds_order: str | None = None,
) -> ParsedOffer | None:
    compact = SPACE_RE.sub(" ", line).strip()
    match = ROW_RE.match(compact)
    if match is None:
        match = ROW_NO_CODE_RE.match(compact)
    if match is None:
        return None

    odd1 = _decimal(match.group("odd1"))
    odd2 = _decimal(match.group("odd2"))
    if odds_order == "over_under" or (odds_order is None and bookmaker == "mozzart"):
        over_odds, under_odds = odd1, odd2
    else:
        under_odds, over_odds = odd1, odd2

    code = match.groupdict().get("code")
    return ParsedOffer(
        bookmaker=bookmaker,
        page_number=page_number,
        event_time_local=_parse_clock(match.group("clock")),
        source_event_code=code,
        participant_text=match.group("body").strip(),
        points_line=_decimal(match.group("line")),
        under_odds=under_odds,
        over_odds=over_odds,
        row_text=compact,
    )

def parse_mozzart_player_points_pages(pages: Iterable[str]) -> list[ParsedOffer]:
    """Parse only the central EuroLeague player-points table from Mozzart PDFs.

    State is reset on every page so a later NBA/ABA page cannot inherit an
    EuroLeague header. Rows with a non-EuroLeague team tag are rejected; after
    two consecutive foreign-team rows the current table is considered finished.
    """
    offers: list[ParsedOffer] = []

    for page_number, page_text in enumerate(pages, start=1):
        in_players = False
        in_euroleague = False
        points_header_seen = False
        foreign_rows = 0

        for raw_line in page_text.splitlines():
            line = SPACE_RE.sub(" ", raw_line).strip()
            key = _heading_key(line)
            if not line:
                continue

            if any(marker in key for marker in MOZZART_SECTION_MARKERS):
                in_players = True
                in_euroleague = False
                points_header_seen = False
                foreign_rows = 0
                continue

            if in_players and key in {"evroliga", "euroleague"}:
                in_euroleague = True
                points_header_seen = False
                foreign_rows = 0
                continue

            if in_players and in_euroleague and MOZZART_POINTS_MARKER in key:
                points_header_seen = True
                foreign_rows = 0
                continue

            if in_players and in_euroleague and points_header_seen:
                offer = _parse_row(line, bookmaker="mozzart", page_number=page_number)
                if offer is not None:
                    tokens = offer.participant_text.split()
                    team_tag = tokens[-1] if tokens else ""
                    if team_tag in MOZZART_EUROLEAGUE_TEAM_TAGS:
                        offers.append(offer)
                        foreign_rows = 0
                    else:
                        foreign_rows += 1
                        if foreign_rows >= 2:
                            in_euroleague = False
                            points_header_seen = False
                    continue

            if in_players and key in {
                "tenis",
                "fudbal",
                "kosarka",
                "nba",
                "aba liga",
                "evrokup",
                "eurocup",
            }:
                in_players = False
                in_euroleague = False
                points_header_seen = False
                foreign_rows = 0

    return offers


def parse_starbet_player_points_pages(pages: Iterable[str]) -> list[ParsedOffer]:
    """Parse StarBet EuroLeague player-points rows from text-layout PDFs."""
    offers: list[ParsedOffer] = []
    in_points = False

    for page_number, page_text in enumerate(pages, start=1):
        for raw_line in page_text.splitlines():
            line = SPACE_RE.sub(" ", raw_line).strip()
            key = _heading_key(line)
            if not line:
                continue

            if "euroleague" in key and "player" in key:
                if any(marker in key for marker in STARBET_OTHER_MARKERS):
                    in_points = False
                elif any(marker in key for marker in STARBET_POINTS_MARKERS):
                    in_points = True
                continue

            if not in_points:
                continue

            offer = _parse_row(line, bookmaker="starbet", page_number=page_number)
            if offer is not None:
                offers.append(offer)

    return offers


def _generic_player_points_odds_order(key: str) -> str:
    """Infer whether a table prints under or over odds first from its header."""
    under_positions = [pos for token in ("manje", "under") if (pos := key.find(token)) >= 0]
    over_positions = [pos for token in ("vise", "over") if (pos := key.find(token)) >= 0]
    if under_positions and over_positions:
        return "under_over" if min(under_positions) < min(over_positions) else "over_under"
    return "under_over"


def parse_generic_euroleague_player_points_pages(
    pages: Iterable[str],
    *,
    bookmaker: str,
) -> list[ParsedOffer]:
    """Parse EuroLeague player-points rows only after strict section markers."""
    offers: list[ParsedOffer] = []
    euroleague_seen = False
    in_points = False
    odds_order = "under_over"
    lines_since_header = 0

    for page_number, page_text in enumerate(pages, start=1):
        # Require the EuroLeague marker on each PDF page. This is intentionally
        # strict so a later ABA/NBA page cannot inherit EuroLeague state.
        euroleague_seen = False
        in_points = False
        lines_since_header = 0
        for raw_line in page_text.splitlines():
            line = SPACE_RE.sub(" ", raw_line).strip()
            key = _heading_key(line)
            if not line:
                continue

            if any(marker in key for marker in GENERIC_EUROLEAGUE_MARKERS):
                euroleague_seen = True
                in_points = False
                lines_since_header = 0

            if euroleague_seen and any(marker in key for marker in GENERIC_PLAYER_POINTS_MARKERS):
                in_points = True
                odds_order = _generic_player_points_odds_order(key)
                lines_since_header = 0
                continue

            if not in_points:
                continue

            if any(marker in key for marker in GENERIC_OTHER_PROP_MARKERS):
                in_points = False
                continue

            offer = _parse_row(
                line,
                bookmaker=bookmaker,
                page_number=page_number,
                odds_order=odds_order,
            )
            if offer is not None:
                offers.append(offer)
                lines_since_header = 0
                continue

            lines_since_header += 1
            if lines_since_header > 35:
                in_points = False

    return offers


def parse_meridian_player_points_pages(pages: Iterable[str]) -> list[ParsedOffer]:
    return parse_generic_euroleague_player_points_pages(pages, bookmaker="meridian")


def parse_millennium_player_points_pages(pages: Iterable[str]) -> list[ParsedOffer]:
    return parse_generic_euroleague_player_points_pages(pages, bookmaker="millennium")
