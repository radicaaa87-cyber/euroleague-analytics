"""Audit bookmaker player-points lines against leakage-safe pregame averages."""

from __future__ import annotations

import json
import os
import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from statistics import mean

import psycopg

OFFER_DATES = (
    "2024-10-16",
    "2024-10-23",
    "2024-11-01",
    "2024-11-07",
    "2024-11-20",
)

TEAM_ALIASES = {
    "Alb": {"BER"},
    "Pan": {"PAN"},
    "Mon": {"MCO"},
    "Arm": {"MIL"},
    "Žal": {"ZAL"},
    "Bar": {"BAR"},
    "Bas": {"BAS"},
    "Par": {"PAR", "PRS"},
    "Baj": {"MUN"},
    "Rea": {"MAD"},
    "Mak": {"TEL"},
    "Asv": {"ASV"},
    "Fen": {"ULK"},
    "Oli": {"OLY"},
    "Prz": {"PRS"},
    "Crv": {"RED"},
    "Vir": {"VIR"},
    "Efe": {"IST"},
}


def _norm(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def _parse_participant(value: str) -> tuple[str, str, str]:
    parts = value.strip().split()
    team_tag = parts[-1]
    player = " ".join(parts[:-1])
    if "." in player:
        prefix, surname = player.split(".", 1)
    else:
        tokens = player.split()
        prefix = tokens[0] if len(tokens) > 1 and len(tokens[0]) <= 3 else ""
        surname = " ".join(tokens[1:]) if prefix else player
    return team_tag, _norm(prefix), _norm(surname.rstrip("."))


def _name_score(
    participant: str,
    player_name: str,
    team_code: str,
) -> int:
    team_tag, prefix, book_surname = _parse_participant(participant)
    aliases = TEAM_ALIASES.get(team_tag, set())
    surname_raw, _, forename_raw = player_name.partition(",")
    surname = _norm(surname_raw)
    forename = _norm(forename_raw)

    score = 0
    if surname == book_surname:
        score += 100
    elif surname.startswith(book_surname) or book_surname.startswith(surname):
        score += 82
    else:
        shared = min(5, len(surname), len(book_surname))
        if shared >= 4 and surname[:shared] == book_surname[:shared]:
            score += 62

    if team_code in aliases:
        score += 30
    if prefix and forename:
        if forename.startswith(prefix):
            score += 20
        elif forename[0] == prefix[0]:
            score += 8
    return score


def _mae(rows: list[dict[str, float]], key: str) -> float:
    return mean(row[key] for row in rows)


def main() -> int:
    warehouse_url = os.environ["WAREHOUSE_DATABASE_URL"]
    frozen_offers = json.loads(
        Path("data/bookmaker_audit_mozzart_2024.json").read_text(encoding="utf-8")
    )
    offers = [
        (
            row["offer_id"],
            row["offer_date"],
            row["participant_text"],
            row["points_line"],
            row["over_odds"],
            row["under_odds"],
        )
        for row in frozen_offers
        if row["offer_date"] in OFFER_DATES
    ]

    with psycopg.connect(warehouse_url) as warehouse:
        player_games = warehouse.execute(
            """
            select
                b.season_code,
                b.gamecode,
                g.utc_date::date::text as game_date,
                b.player_id,
                p.display_name as player_name,
                b.team_code,
                b.points::float8,
                m.seconds_official::float8
            from warehouse.raw_boxscore_player b
            join warehouse.raw_game g
              on g.season_code = b.season_code
             and g.gamecode = b.gamecode
            left join warehouse.player p
              on p.player_id = b.player_id
            left join warehouse.player_game_minutes m
              on m.season_code = b.season_code
             and m.gamecode = b.gamecode
             and m.player_id = b.player_id
            left join warehouse.game_quality q
              on q.season_code = b.season_code
             and q.gamecode = b.gamecode
            where b.season_code = 'E2024'
              and g.utc_date::date between '2024-10-03' and '2024-11-20'
              and not coalesce(q.excluded_by_default, false)
            order by g.utc_date, b.gamecode, b.team_code, p.display_name
            """
        ).fetchall()

    games_by_date: dict[str, list[tuple]] = defaultdict(list)
    history_by_player: dict[str, list[tuple]] = defaultdict(list)
    for row in player_games:
        if float(row[7] or 0) <= 0:
            continue
        games_by_date[row[2]].append(row)
        history_by_player[row[3]].append(row)

    evaluated: list[dict[str, float | str | int]] = []
    unmatched: list[dict[str, str | int]] = []

    for offer in offers:
        offer_id, offer_date, participant, line, over_odds, under_odds = offer
        candidates = []
        for game_row in games_by_date.get(offer_date, []):
            score = _name_score(participant, game_row[4], game_row[5])
            if score >= 62:
                candidates.append((score, game_row))

        candidates.sort(key=lambda item: item[0], reverse=True)
        if not candidates or (len(candidates) > 1 and candidates[0][0] == candidates[1][0]):
            unmatched.append(
                {
                    "offer_id": offer_id,
                    "offer_date": offer_date,
                    "participant": participant,
                }
            )
            continue

        current = candidates[0][1]
        player_id = current[3]
        prior = [
            row
            for row in history_by_player[player_id]
            if row[2] < offer_date and float(row[7] or 0) > 0
        ]
        prior.sort(key=lambda row: (row[2], row[1]))
        if not prior:
            continue

        actual = float(current[6])
        season_avg = mean(float(row[6]) for row in prior)
        l3 = mean(float(row[6]) for row in prior[-3:])
        l5 = mean(float(row[6]) for row in prior[-5:])
        line = float(line)

        evaluated.append(
            {
                "offer_id": offer_id,
                "offer_date": offer_date,
                "participant": participant,
                "actual": actual,
                "line": line,
                "prior_games": len(prior),
                "season_avg": season_avg,
                "l3": l3,
                "l5": l5,
                "book_err": abs(actual - line),
                "season_err": abs(actual - season_avg),
                "l3_err": abs(actual - l3),
                "l5_err": abs(actual - l5),
                "signed_book_err": actual - line,
                "over_odds": float(over_odds),
                "under_odds": float(under_odds),
            }
        )

    if not evaluated:
        raise RuntimeError("No bookmaker offers matched the E2024 warehouse.")

    book_mae = _mae(evaluated, "book_err")
    season_mae = _mae(evaluated, "season_err")
    l3_mae = _mae(evaluated, "l3_err")
    l5_mae = _mae(evaluated, "l5_err")

    book_closer = sum(row["book_err"] < row["season_err"] for row in evaluated)
    season_closer = sum(row["season_err"] < row["book_err"] for row in evaluated)
    ties = len(evaluated) - book_closer - season_closer

    by_date = []
    for date_value in OFFER_DATES:
        subset = [row for row in evaluated if row["offer_date"] == date_value]
        if not subset:
            continue
        date_book = _mae(subset, "book_err")
        date_season = _mae(subset, "season_err")
        by_date.append(
            {
                "date": date_value,
                "n": len(subset),
                "book_mae": round(date_book, 4),
                "season_average_mae": round(date_season, 4),
                "improvement_pct": round(
                    100.0 * (date_season - date_book) / date_season,
                    2,
                ),
            }
        )

    result = {
        "offers_loaded": len(offers),
        "evaluated": len(evaluated),
        "unmatched": len(unmatched),
        "bookmaker_mae": round(book_mae, 4),
        "season_average_mae": round(season_mae, 4),
        "l3_mae": round(l3_mae, 4),
        "l5_mae": round(l5_mae, 4),
        "bookmaker_improvement_vs_season_average_points": round(
            season_mae - book_mae,
            4,
        ),
        "bookmaker_improvement_vs_season_average_pct": round(
            100.0 * (season_mae - book_mae) / season_mae,
            2,
        ),
        "bookmaker_closer_count": book_closer,
        "season_average_closer_count": season_closer,
        "ties": ties,
        "within_1_point_pct": round(
            100.0 * sum(row["book_err"] <= 1 for row in evaluated) / len(evaluated),
            2,
        ),
        "within_2_points_pct": round(
            100.0 * sum(row["book_err"] <= 2 for row in evaluated) / len(evaluated),
            2,
        ),
        "within_3_points_pct": round(
            100.0 * sum(row["book_err"] <= 3 for row in evaluated) / len(evaluated),
            2,
        ),
        "within_4_points_pct": round(
            100.0 * sum(row["book_err"] <= 4 for row in evaluated) / len(evaluated),
            2,
        ),
        "mean_signed_error_actual_minus_line": round(
            mean(row["signed_book_err"] for row in evaluated),
            4,
        ),
        "actual_over_count": sum(row["actual"] > row["line"] for row in evaluated),
        "actual_under_count": sum(row["actual"] < row["line"] for row in evaluated),
        "by_date": by_date,
        "unmatched_examples": unmatched[:15],
    }

    print("BOOKMAKER_MODEL_AUDIT=" + json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
