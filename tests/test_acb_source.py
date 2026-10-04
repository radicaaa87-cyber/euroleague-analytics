"""ACB source contracts for domestic box scores and play-by-play.

The first contract uses the official 2026 Supercopa final, Joventut-Barça
(match 105683), because it was played before Barcelona's EuroLeague opener and
therefore is exactly the kind of domestic role signal the player-points model
needs. The parser itself is competition-agnostic inside ACB's matchdata API.
"""

from __future__ import annotations

from euroleague.acb import (
    ACB_PUBLIC_API_KEY,
    AcbPlayerGame,
    build_acb_boxscore_url,
    build_acb_play_by_play_url,
    parse_acb_boxscore,
    parse_acb_play_by_play,
)


def _parra_boxscore_payload() -> dict:
    """Minimal API-shaped payload carrying Parra's official 20 Sep 2026 line."""
    return {
        "matchFinished": True,
        "teamBoxscores": [
            {
                "team": {"clubId": 2, "fullName": "Barça"},
                "statsByPeriods": [
                    {
                        "quarter": 0,
                        "stats": {
                            "players": [
                                {
                                    "player": {
                                        "id": 20212265,
                                        "firstName": " Joel ",
                                        "lastName": " Parra ",
                                        "shirtNumber": " 44 ",
                                    },
                                    "isStarted": False,
                                    "playTime": "26:05",
                                    "points": 24,
                                    "twoPointersMade": 5,
                                    "twoPointersAttempted": 5,
                                    "threePointersMade": 4,
                                    "threePointersAttempted": 6,
                                    "freeThrowsMade": 2,
                                    "freeThrowsAttempted": 4,
                                    "offRebounds": 0,
                                    "defRebounds": 2,
                                    "assists": 3,
                                    "steals": 1,
                                    "turnovers": 0,
                                    "blocks": 0,
                                    "personalFouls": 2,
                                    "foulsReceived": 3,
                                    "plusMinus": -15,
                                    "valuation": 27,
                                }
                            ],
                            "total": {},
                        },
                    }
                ],
            }
        ],
    }


def test_acb_url_builders_target_the_public_matchdata_api() -> None:
    assert build_acb_boxscore_url("105683") == (
        "https://api2.acb.com/api/matchdata/Result/boxscores?matchId=105683"
    )
    assert build_acb_play_by_play_url("105683") == (
        "https://api2.acb.com/api/matchdata/PlayByPlay/play-by-play?matchId=105683"
    )
    assert ACB_PUBLIC_API_KEY


def test_parra_official_line_is_parsed_without_losing_shot_volume() -> None:
    rows = parse_acb_boxscore("105683", _parra_boxscore_payload())

    assert rows == (
        AcbPlayerGame(
            match_id="105683",
            source_player_id="20212265",
            team_source_id="2",
            display_name="Joel Parra",
            jersey_number="44",
            is_starter=False,
            minutes_seconds=1565,
            points=24,
            two_made=5,
            two_attempted=5,
            three_made=4,
            three_attempted=6,
            free_throw_made=2,
            free_throw_attempted=4,
            offensive_rebounds=0,
            defensive_rebounds=2,
            assists=3,
            steals=1,
            turnovers=0,
            blocks=0,
            fouls_committed=2,
            fouls_received=3,
            plus_minus=-15,
            valuation=27,
        ),
    )
    assert rows[0].field_goals_attempted == 11


def test_play_by_play_preserves_source_array_order_even_if_source_order_is_odd() -> None:
    payload = {
        "plays": [
            {
                "order": 20,
                "playType": 599,
                "local": True,
                "quarter": 1,
                "minute": 10,
                "second": 0,
                "playerLicenseId": 20212265,
                "scoreHome": 0,
                "scoreAway": 0,
            },
            {
                "order": 19,
                "playType": 115,
                "local": True,
                "quarter": 1,
                "minute": 5,
                "second": 0,
                "playerLicenseId": 20212265,
                "scoreHome": 18,
                "scoreAway": 16,
            },
            {
                "order": 21,
                "playType": 112,
                "local": True,
                "quarter": 1,
                "minute": 5,
                "second": 0,
                "playerLicenseId": 99999999,
                "scoreHome": 18,
                "scoreAway": 16,
            },
        ]
    }

    rows = parse_acb_play_by_play("105683", payload)

    assert [row.ingest_index for row in rows] == [0, 1, 2]
    assert [row.source_order for row in rows] == [20, 19, 21]
    assert [row.event_kind for row in rows] == ["starter", "sub_out", "sub_in"]
    assert [row.source_player_id for row in rows] == ["20212265", "20212265", "99999999"]


def test_play_by_play_keeps_unknown_event_types_instead_of_guessing() -> None:
    rows = parse_acb_play_by_play(
        "105683",
        {
            "plays": [
                {
                    "order": 77,
                    "playType": 424242,
                    "local": None,
                    "quarter": 2,
                    "minute": 7,
                    "second": 31,
                    "playerLicenseId": None,
                    "scoreHome": 35,
                    "scoreAway": 32,
                }
            ]
        },
    )

    assert rows[0].event_kind == "source_event"
    assert rows[0].play_type == 424242
    assert rows[0].source_player_id is None
