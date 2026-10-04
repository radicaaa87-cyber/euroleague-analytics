"""Build cross-league recent-form context for matched ACB/EuroLeague players.

This script deliberately keeps ACB and EuroLeague observations as separate rows.
It DOES NOT average the two leagues together.

Inputs:
- ACB model-ready game logs from build_acb_player_game_logs.py
- ACB↔EuroLeague bridge from build_acb_el_player_bridge.py
- EuroLeague PostgreSQL warehouse via DATABASE_URL

Outputs:
- recent_form_rows.csv: last N games per player per league
- recent_form_summary.csv: separate ACB and EL averages/trends per player

Example:
    python scripts/build_cross_league_recent_form.py \
      --acb data/acb_processed/2026-27/player_game_logs.csv \
      --bridge data/cross_league/acb_el_player_bridge.csv \
      --el-season E2026 \
      --last-n 5
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any

from euroleague.config import DatabaseSettings
from euroleague.mcp.db import connect


NUMERIC_FIELDS = ("min", "pts", "fga", "fga3", "fta", "reb", "ast", "tov")


def _float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _load_bridge(path: Path) -> list[dict[str, str]]:
    rows = _read_csv(path)
    return [
        row
        for row in rows
        if row.get("status") == "MATCHED"
        and row.get("acb_player_id")
        and row.get("euroleague_player_id")
    ]


def _acb_recent(
    acb_rows: list[dict[str, str]],
    bridge_rows: list[dict[str, str]],
    last_n: int,
) -> list[dict[str, Any]]:
    bridge_by_acb = {str(row["acb_player_id"]): row for row in bridge_rows}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for row in acb_rows:
        acb_id = str(row.get("player_id") or "")
        bridge = bridge_by_acb.get(acb_id)
        if bridge is None:
            continue

        grouped[acb_id].append(
            {
                "league": "ACB",
                "acb_player_id": acb_id,
                "euroleague_player_id": bridge["euroleague_player_id"],
                "player": bridge.get("euroleague_player") or row.get("player", ""),
                "game_id": row.get("match_id", ""),
                "game_date": row.get("date", ""),
                "team": row.get("team", ""),
                "opponent": row.get("opponent", ""),
                "min": _float(row.get("min")),
                "pts": _float(row.get("pts")),
                "fga": _float(row.get("fga")),
                "fga3": _float(row.get("fga3")),
                "fta": _float(row.get("fta")),
                "reb": _float(row.get("reb")),
                "ast": _float(row.get("ast")),
                "tov": _float(row.get("tov")),
            }
        )

    result: list[dict[str, Any]] = []
    for player_rows in grouped.values():
        # ISO dates sort naturally; match id is a stable fallback.
        player_rows.sort(
            key=lambda row: (str(row.get("game_date") or ""), str(row.get("game_id") or "")),
            reverse=True,
        )
        result.extend(player_rows[:last_n])
    return result


def _el_recent(
    season: str,
    bridge_rows: list[dict[str, str]],
    last_n: int,
) -> list[dict[str, Any]]:
    settings = DatabaseSettings.from_env()
    connection = connect(settings)
    try:
        with connection.cursor() as cursor:
            result: list[dict[str, Any]] = []
            for bridge in bridge_rows:
                player_id = bridge["euroleague_player_id"]
                cursor.execute(
                    """
                    select
                        p.gamecode,
                        g.utc_date::date as game_date,
                        p.team_code,
                        case
                            when p.team_code = g.home_team_code then g.away_team_code
                            else g.home_team_code
                        end as opponent_team_code,
                        round(p.seconds_corrected::numeric / 60.0, 2) as min,
                        p.points as pts,
                        p.field_goals_attempted as fga,
                        p.three_pointers_attempted as fga3,
                        p.free_throws_attempted as fta,
                        p.total_rebounds as reb,
                        p.assists as ast,
                        p.turnovers as tov
                    from v_player_game p
                    join v_game g
                      on g.season_code = p.season_code
                     and g.gamecode = p.gamecode
                    where p.season_code = %s
                      and p.player_id = %s
                      and not g.excluded_by_default
                    order by g.utc_date desc, p.gamecode desc
                    limit %s
                    """,
                    (season, player_id, last_n),
                )
                columns = [column[0] for column in cursor.description]
                for values in cursor.fetchall():
                    row = dict(zip(columns, values, strict=True))
                    result.append(
                        {
                            "league": "EUROLEAGUE",
                            "acb_player_id": bridge["acb_player_id"],
                            "euroleague_player_id": player_id,
                            "player": bridge.get("euroleague_player", ""),
                            "game_id": row["gamecode"],
                            "game_date": row["game_date"],
                            "team": row["team_code"],
                            "opponent": row["opponent_team_code"],
                            "min": _float(row["min"]),
                            "pts": _float(row["pts"]),
                            "fga": _float(row["fga"]),
                            "fga3": _float(row["fga3"]),
                            "fta": _float(row["fta"]),
                            "reb": _float(row["reb"]),
                            "ast": _float(row["ast"]),
                            "tov": _float(row["tov"]),
                        }
                    )
            return result
    finally:
        connection.close()


def _avg(rows: list[dict[str, Any]], field: str) -> float | None:
    values = [row[field] for row in rows if row.get(field) is not None]
    return round(mean(values), 3) if values else None


def _trend(rows: list[dict[str, Any]], field: str) -> float | None:
    """Newest-half average minus older-half average within the requested window."""
    values = [row[field] for row in rows if row.get(field) is not None]
    if len(values) < 4:
        return None
    split = max(2, len(values) // 2)
    newer = values[:split]
    older = values[split:]
    if not older:
        return None
    return round(mean(newer) - mean(older), 3)


def _summaries(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row["euroleague_player_id"]), row["league"])].append(row)

    players: dict[str, dict[str, Any]] = {}
    for (player_id, league), league_rows in grouped.items():
        base = players.setdefault(
            player_id,
            {
                "euroleague_player_id": player_id,
                "acb_player_id": league_rows[0]["acb_player_id"],
                "player": league_rows[0]["player"],
            },
        )
        prefix = "el" if league == "EUROLEAGUE" else "acb"
        base[f"{prefix}_games"] = len(league_rows)
        for field in NUMERIC_FIELDS:
            base[f"{prefix}_{field}_avg"] = _avg(league_rows, field)
        base[f"{prefix}_min_trend"] = _trend(league_rows, "min")
        base[f"{prefix}_pts_trend"] = _trend(league_rows, "pts")
        base[f"{prefix}_fga_trend"] = _trend(league_rows, "fga")

    return list(players.values())


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--acb", required=True)
    parser.add_argument("--bridge", required=True)
    parser.add_argument("--el-season", required=True, help="Warehouse season code, e.g. E2026")
    parser.add_argument("--last-n", type=int, default=5)
    parser.add_argument("--output-dir", default="data/cross_league")
    args = parser.parse_args()

    bridge = _load_bridge(Path(args.bridge))
    acb_rows = _read_csv(Path(args.acb))

    acb_recent = _acb_recent(acb_rows, bridge, args.last_n)
    el_recent = _el_recent(args.el_season, bridge, args.last_n)

    combined = el_recent + acb_recent
    combined.sort(
        key=lambda row: (
            str(row["euroleague_player_id"]),
            row["league"],
            str(row.get("game_date") or ""),
        ),
        reverse=True,
    )

    out_dir = Path(args.output_dir)
    rows_path = out_dir / "recent_form_rows.csv"
    summary_path = out_dir / "recent_form_summary.csv"
    _write_csv(rows_path, combined)
    _write_csv(summary_path, _summaries(combined))

    print(
        f"Players={len(bridge)} EL_rows={len(el_recent)} ACB_rows={len(acb_recent)} "
        f"-> {rows_path} and {summary_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
