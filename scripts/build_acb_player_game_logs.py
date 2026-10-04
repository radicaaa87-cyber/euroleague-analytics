"""Build model-ready ACB player game logs from pulled official boxscores.

Input:
    data/acb_raw/<season>/matches.json
    data/acb_raw/<season>/boxscores/*.json

Output:
    data/acb_processed/<season>/player_game_logs.csv

The output is intentionally close to the EuroLeague model inputs:
MIN, PTS, 2PA, 3PA, FTA, REB, AST, TOV, STL, BLK, starter, +/-.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("data", "results", "items", "players", "statistics"):
        value = payload.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
        if isinstance(value, dict):
            for nested in ("data", "items", "results"):
                inner = value.get(nested)
                if isinstance(inner, list):
                    return [x for x in inner if isinstance(x, dict)]
    return []


def _value(row: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return default


def _nested(row: dict[str, Any], key: str, nested_key: str, default: Any = "") -> Any:
    value = row.get(key)
    if isinstance(value, dict):
        return value.get(nested_key, default)
    return default


def _match_id(row: dict[str, Any]) -> str:
    return str(_value(row, "idMatch", "id_match", "id", default=""))


def _index_matches(payload: Any) -> dict[str, dict[str, Any]]:
    return {_match_id(row): row for row in _rows(payload) if _match_id(row)}


def _player_name(row: dict[str, Any]) -> str:
    license_obj = row.get("license")
    if isinstance(license_obj, dict):
        for key in ("licenseStr15", "licenseNick", "licenseStr", "name"):
            value = license_obj.get(key)
            if value:
                return str(value)
    return str(_value(row, "player", "player_name", default=""))


def _team_name(row: dict[str, Any]) -> str:
    team_id = _value(row, "id_team", "team_id")
    local_id = _value(row, "id_local_team", "local_team_id")
    local = _nested(row, "local_team", "team_actual_name")
    visitor = _nested(row, "visitor_team", "team_actual_name")
    if team_id is not None and local_id is not None and str(team_id) == str(local_id):
        return str(local)
    if visitor:
        return str(visitor)
    return str(_value(row, "team", "team_name", default=local))


def _opponent_name(row: dict[str, Any]) -> str:
    team_id = _value(row, "id_team", "team_id")
    local_id = _value(row, "id_local_team", "local_team_id")
    local = _nested(row, "local_team", "team_actual_name")
    visitor = _nested(row, "visitor_team", "team_actual_name")
    if team_id is not None and local_id is not None and str(team_id) == str(local_id):
        return str(visitor)
    return str(local)


def _seconds_to_minutes(value: Any) -> float | None:
    try:
        return round(float(value) / 60.0, 3)
    except (TypeError, ValueError):
        return None


def normalize_row(
    season: str,
    match_id: str,
    row: dict[str, Any],
    match_meta: dict[str, Any] | None,
) -> dict[str, Any] | None:
    player_id = _value(row, "id_license", "player_id")
    if player_id in (None, "", 0, "0"):
        return None

    minutes = _seconds_to_minutes(_value(row, "time_played", "tot_sec"))
    threepm = _value(row, "3pt_success", "threepm", default=0)
    threepa = _value(row, "3pt_tried", "threepa", default=0)
    twopm = _value(row, "2pt_success", "twopm", default=0)
    twopa = _value(row, "2pt_tried", "twopa", default=0)
    ftm = _value(row, "1pt_success", "ftm", default=0)
    fta = _value(row, "1pt_tried", "fta", default=0)

    meta = match_meta or {}
    return {
        "season": season,
        "match_id": match_id,
        "date": _value(meta, "date", "match_date", "start_date", "datetime", default=""),
        "round": _value(meta, "descriptor", "matchweek", "round", "jornada", default=""),
        "player_id": player_id,
        "player": _player_name(row),
        "team": _team_name(row),
        "opponent": _opponent_name(row),
        "min": minutes,
        "pts": _value(row, "points", "pts", default=0),
        "fgm2": twopm,
        "fga2": twopa,
        "fgm3": threepm,
        "fga3": threepa,
        "ftm": ftm,
        "fta": fta,
        "fga": (twopa or 0) + (threepa or 0),
        "reb": _value(row, "total_rebound", "treb", "reb", default=0),
        "oreb": _value(row, "offensive_rebound", "oreb", default=0),
        "dreb": _value(row, "defensive_rebound", "dreb", default=0),
        "ast": _value(row, "asis", "ast", default=0),
        "tov": _value(row, "turnovers", "tov", default=0),
        "stl": _value(row, "steals", "stl", default=0),
        "blk": _value(row, "blocks", "blk", default=0),
        "pf": _value(row, "personal_fouls", "pf", default=0),
        "fd": _value(row, "received_fouls", "fd", default=0),
        "pir": _value(row, "val", "pir", default=0),
        "starter": _value(row, "starting", "starter", default=False),
        "plus_minus": _value(row, "differential", "pm", default=0),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--season", required=True)
    parser.add_argument("--input", default="data/acb_raw")
    parser.add_argument("--output", default="data/acb_processed")
    args = parser.parse_args()

    season_dir = Path(args.input) / args.season
    matches_file = season_dir / "matches.json"
    boxscore_dir = season_dir / "boxscores"

    if not matches_file.exists():
        raise SystemExit(f"Missing {matches_file}")
    if not boxscore_dir.exists():
        raise SystemExit(f"Missing {boxscore_dir}")

    matches = _index_matches(json.loads(matches_file.read_text(encoding="utf-8")))
    output_rows: list[dict[str, Any]] = []

    for path in sorted(boxscore_dir.glob("*.json")):
        match_id = path.stem
        payload = json.loads(path.read_text(encoding="utf-8"))
        for row in _rows(payload):
            normalized = normalize_row(args.season, match_id, row, matches.get(match_id))
            if normalized is not None:
                output_rows.append(normalized)

    out_dir = Path(args.output) / args.season
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "player_game_logs.csv"

    fieldnames = [
        "season", "match_id", "date", "round", "player_id", "player",
        "team", "opponent", "min", "pts", "fgm2", "fga2", "fgm3", "fga3",
        "ftm", "fta", "fga", "reb", "oreb", "dreb", "ast", "tov", "stl",
        "blk", "pf", "fd", "pir", "starter", "plus_minus",
    ]

    with out_file.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(output_rows)

    print(f"Wrote {len(output_rows)} player-game rows to {out_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
