"""Build model-ready ACB player game logs from current ACB boxscores."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any


def _v(row: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return default


def _parse_minutes(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    if ":" in text:
        m, _, s = text.partition(":")
        try:
            return round(int(m) + int(s) / 60.0, 3)
        except ValueError:
            return None
    try:
        return round(float(text) / 60.0, 3)
    except TypeError, ValueError:
        return None


def _full_players(team_box: dict[str, Any]) -> list[dict[str, Any]]:
    for period in team_box.get("statsByPeriods") or []:
        if isinstance(period, dict) and period.get("quarter") == 0:
            stats = period.get("stats") or {}
            return [x for x in (stats.get("players") or []) if isinstance(x, dict)]
    return []


def _player_name(row: dict[str, Any]) -> str:
    p = row.get("player") or {}
    if not isinstance(p, dict):
        return ""
    return str(p.get("nickname") or "").strip() or " ".join(
        x
        for x in (str(p.get("firstName") or "").strip(), str(p.get("lastName") or "").strip())
        if x
    )


def _rows(
    season: str, match_id: str, match: dict[str, Any], payload: dict[str, Any]
) -> list[dict[str, Any]]:
    teams = [x for x in payload.get("teamBoxscores", []) if isinstance(x, dict)]
    if len(teams) < 2:
        return []
    out = []
    for i, team_box in enumerate(teams[:2]):
        opp_box = teams[1 - i]
        team = team_box.get("team") or {}
        opp = opp_box.get("team") or {}
        for row in _full_players(team_box):
            p = row.get("player") or {}
            pid = p.get("id") if isinstance(p, dict) else None
            if pid in (None, ""):
                continue
            twopa = _v(row, "twoPointersAttempted", default=0) or 0
            threepa = _v(row, "threePointersAttempted", default=0) or 0
            out.append(
                {
                    "season": season,
                    "match_id": match_id,
                    "date": str(match.get("startDateTime") or "")[:10],
                    "round": _v(match, "round", "matchWeekName", "weekName", default=""),
                    "player_id": pid,
                    "player": _player_name(row),
                    "team": team.get("fullName") or team.get("name") or "",
                    "opponent": opp.get("fullName") or opp.get("name") or "",
                    "min": _parse_minutes(row.get("playTime")),
                    "pts": _v(row, "points", default=0),
                    "fgm2": _v(row, "twoPointersMade", default=0),
                    "fga2": twopa,
                    "fgm3": _v(row, "threePointersMade", default=0),
                    "fga3": threepa,
                    "ftm": _v(row, "freeThrowsMade", default=0),
                    "fta": _v(row, "freeThrowsAttempted", default=0),
                    "fga": twopa + threepa,
                    "reb": _v(row, "totalRebounds", default=0),
                    "oreb": _v(row, "offRebounds", default=0),
                    "dreb": _v(row, "defRebounds", default=0),
                    "ast": _v(row, "assists", default=0),
                    "tov": _v(row, "turnovers", default=0),
                    "stl": _v(row, "steals", default=0),
                    "blk": _v(row, "blocks", default=0),
                    "pf": _v(row, "personalFouls", default=0),
                    "fd": _v(row, "foulsDrawn", default=0),
                    "pir": _v(row, "rating", default=0),
                    "starter": bool(_v(row, "isStarted", default=False)),
                    "plus_minus": _v(row, "plusMinus", default=0),
                }
            )
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--season", required=True)
    p.add_argument("--input", default="data/acb_raw")
    p.add_argument("--output", default="data/acb_processed")
    args = p.parse_args()

    season_dir = Path(args.input) / args.season
    matches = json.loads((season_dir / "matches.json").read_text(encoding="utf-8"))
    by_id = {
        str(m.get("id")): m for m in matches if isinstance(m, dict) and m.get("id") is not None
    }
    result = []

    for path in sorted((season_dir / "boxscores").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            result.extend(_rows(args.season, path.stem, by_id.get(path.stem, {}), payload))

    out_dir = Path(args.output) / args.season
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "player_game_logs.csv"
    fields = [
        "season",
        "match_id",
        "date",
        "round",
        "player_id",
        "player",
        "team",
        "opponent",
        "min",
        "pts",
        "fgm2",
        "fga2",
        "fgm3",
        "fga3",
        "ftm",
        "fta",
        "fga",
        "reb",
        "oreb",
        "dreb",
        "ast",
        "tov",
        "stl",
        "blk",
        "pf",
        "fd",
        "pir",
        "starter",
        "plus_minus",
    ]
    with out_file.open("w", newline="", encoding="utf-8") as h:
        w = csv.DictWriter(h, fieldnames=fields)
        w.writeheader()
        w.writerows(result)
    print(f"Wrote {len(result)} player-game rows to {out_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
