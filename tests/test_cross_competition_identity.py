"""Cross-competition player identity is evidence-based and source-native.

The ACB and EuroLeague publish different identifiers for the same athlete.
These tests lock the first bridge before any domestic-league rows are written.
"""

from __future__ import annotations

from datetime import date

from euroleague.identity import (
    IdentityDecision,
    SourcePerson,
    decide_identity_match,
    normalized_person_name,
)


def _parra_el() -> SourcePerson:
    return SourcePerson(
        source="EL",
        source_player_id="P007464",
        display_name="PARRA, JOEL",
        birth_date=date(2000, 4, 4),
        height_cm=201,
        country_code="ESP",
        team_code="BAR",
    )


def _parra_acb() -> SourcePerson:
    return SourcePerson(
        source="ACB",
        source_player_id="20212265",
        display_name="Joel Parra",
        birth_date=date(2000, 4, 4),
        height_cm=202,
        country_code="ESP",
        team_code="BAR",
    )


def test_name_normalization_only_canonicalizes_text_and_never_creates_an_id() -> None:
    assert normalized_person_name("PARRA, JOEL") == "JOEL PARRA"
    assert normalized_person_name("Joel Parra") == "JOEL PARRA"


def test_measured_parra_cross_source_record_is_safe_for_automatic_linking() -> None:
    decision = decide_identity_match(_parra_el(), _parra_acb())

    assert decision.status == IdentityDecision.AUTO_LINK
    assert decision.evidence.birth_date_match is True
    assert decision.evidence.name_match is True
    assert decision.evidence.height_difference_cm == 1
    assert decision.evidence.country_match is True
    assert decision.evidence.team_match is True


def test_name_and_team_without_birth_date_are_not_enough_for_an_automatic_link() -> None:
    acb = SourcePerson(
        source="ACB",
        source_player_id="99999999",
        display_name="Joel Parra",
        birth_date=None,
        height_cm=202,
        country_code="ESP",
        team_code="BAR",
    )

    decision = decide_identity_match(_parra_el(), acb)

    assert decision.status == IdentityDecision.REVIEW
    assert "birth date" in decision.reason.lower()


def test_conflicting_birth_dates_reject_the_match_even_when_everything_else_agrees() -> None:
    acb = SourcePerson(
        source="ACB",
        source_player_id="20212265",
        display_name="Joel Parra",
        birth_date=date(2000, 4, 5),
        height_cm=201,
        country_code="ESP",
        team_code="BAR",
    )

    decision = decide_identity_match(_parra_el(), acb)

    assert decision.status == IdentityDecision.REJECT
    assert "birth date" in decision.reason.lower()


def test_a_large_height_conflict_requires_review_instead_of_being_ignored() -> None:
    acb = SourcePerson(
        source="ACB",
        source_player_id="20212265",
        display_name="Joel Parra",
        birth_date=date(2000, 4, 4),
        height_cm=210,
        country_code="ESP",
        team_code="BAR",
    )

    decision = decide_identity_match(_parra_el(), acb)

    assert decision.status == IdentityDecision.REVIEW
    assert "height" in decision.reason.lower()


def test_source_ids_remain_opaque_and_are_never_rewritten_into_each_other() -> None:
    el = _parra_el()
    acb = _parra_acb()
    decision = decide_identity_match(el, acb)

    assert decision.left.source_player_id == "P007464"
    assert decision.right.source_player_id == "20212265"
    assert decision.left.source_player_id != decision.right.source_player_id
