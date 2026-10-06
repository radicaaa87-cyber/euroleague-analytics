"""Bookmaker player-points parser tests using real offer-layout shapes."""

from __future__ import annotations

from datetime import UTC, datetime

from euroleague.bookmaker_odds import (
    AthleteCandidate,
    infer_document_date,
    parse_mozzart_player_points_pages,
    parse_starbet_player_points_pages,
    resolve_participant,
)


def test_parse_mozzart_central_player_points_only() -> None:
    text = """
KOSARKA - IGRAČI
KOD ZA KUCANJE
Čas R.b.
EVROLIGA
ZBIR više
807
manje
806
Broj poena igrača na meču
ZBIR manje
966
više
967
Mali broj poena igrača na
meču
ZBIR manje
103
više
104
Broj skokova igrača na meču
20:30 7068 M.BELINELLI Vir 10.5 2.00 1.80 8.5 2.55 1.45 12.5 1.40 2.70
20:30 7069 S.LEE Mak 16.5 1.90 1.90 14.5 2.41 1.50 18.5 1.50 2.41
TENIS
"""
    offers = parse_mozzart_player_points_pages([text])

    assert len(offers) == 2
    assert offers[0].participant_text == "M.BELINELLI Vir"
    assert offers[0].points_line == 10.5
    assert offers[0].over_odds == 2.00
    assert offers[0].under_odds == 1.80
    assert offers[0].source_event_code == "7068"


def test_parse_starbet_combined_players_table_uses_first_points_triplet() -> None:
    text = """
STAR BET
EuroLeague Players (Inc. Over Time)
Igrač Tim Ukupno Poena Ukupno Skokova Ukupno Asistencija
Gr. - + Gr. - + Gr. - +
Pet 18:00 8346 Brown Sterling Partizan 12.5 1.70 2.05 2.5 1.55 2.35
Pet 18:00 8347 Fernando Bruno Partizan 8.5 1.70 2.05 5.5 1.65 2.15
Euroleague Player Three's Made (Inc. Over Time)
Pet 18:00 9001 Brown Sterling Three Pts Made 2.5 1.80 1.95
"""
    offers = parse_starbet_player_points_pages([text])

    assert len(offers) == 2
    assert offers[0].participant_text == "Brown Sterling Partizan"
    assert offers[0].points_line == 12.5
    assert offers[0].under_odds == 1.70
    assert offers[0].over_odds == 2.05


def test_mozzart_abbreviation_resolves_to_canonical_athlete() -> None:
    candidates = [
        AthleteCandidate("a1", "Marco Belinelli"),
        AthleteCandidate("a2", "Sylvain Francisco"),
    ]

    match = resolve_participant(
        "M.BELINELLI Vir",
        candidates,
        bookmaker="mozzart",
    )

    assert match.athlete_id == "a1"
    assert match.player_name_raw == "M.BELINELLI"
    assert match.team_name_raw == "Vir"
    assert match.confidence == 1.0


def test_starbet_surname_first_resolves_and_keeps_team() -> None:
    candidates = [
        AthleteCandidate("a1", "Sterling Brown"),
        AthleteCandidate("a2", "Bruno Fernando"),
    ]

    match = resolve_participant(
        "Brown Sterling Partizan",
        candidates,
        bookmaker="starbet",
    )

    assert match.athlete_id == "a1"
    assert match.player_name_raw == "Brown Sterling"
    assert match.team_name_raw == "Partizan"


def test_document_date_comes_from_source_not_collection_time() -> None:
    capture = datetime(2025, 11, 23, 12, 0, tzinfo=UTC)

    dated = infer_document_date(
        "https://www.starbet.rs/content/Documents/Dopuna15.11.2024.pdf",
        capture_at=capture,
    )
    missing_year = infer_document_date(
        "https://starbet.rs/content/Documents/Dopuna22.11.pdf",
        capture_at=capture,
    )

    assert str(dated) == "2024-11-15"
    assert str(missing_year) == "2025-11-22"
