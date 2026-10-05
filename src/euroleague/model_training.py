"""Server-side feature export for player-points machine learning.

The training path talks directly to PostgreSQL in one read-only query. It does
not page the warehouse through ChatGPT/MCP, so training data extraction does not
consume the MCP row budget or create hundreds of model actions.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from euroleague.config import DatabaseSettings
from euroleague.mcp.db import connect

_MINUTES_COLUMNS = {
    "official": "seconds_official",
    "corrected": "seconds_corrected",
    "raw": "seconds_raw",
}


def training_dataset_sql(minutes_basis: str = "official") -> str:
    """Return the leakage-safe player-game feature query.

    Rolling windows always end at 1 PRECEDING, so target-game outcomes never
    enter their own features.
    """
    try:
        seconds = _MINUTES_COLUMNS[minutes_basis]
    except KeyError as exc:
        raise ValueError(
            "minutes_basis must be one of: official, corrected, raw"
        ) from exc

    return f"""
    with player_base as (
        select
            p.season_code,
            p.gamecode,
            p.utc_date::date as game_date,
            p.player_id,
            p.player_name,
            p.team_code,
            p.opponent_team_code,
            tg.is_home,
            p.is_starter,
            p.points,
            p.field_goals_attempted,
            p.three_pointers_attempted,
            p.free_throws_attempted,
            p.{seconds} as seconds_played
        from v_player_game p
        join v_team_game tg
          on tg.season_code = p.season_code
         and tg.gamecode = p.gamecode
         and tg.team_code = p.team_code
        where p.season_code = any(%s)
          and p.seconds_official > 0
          and not p.excluded_by_default
    ),
    player_features as (
        select
            *,
            count(*) over w10 as pre_history_games,

            round(avg(seconds_played::numeric / 60.0) over w3, 3) as pre_l3_minutes,
            round(avg(seconds_played::numeric / 60.0) over w5, 3) as pre_l5_minutes,
            round(avg(seconds_played::numeric / 60.0) over w10, 3) as pre_l10_minutes,

            round(avg(points::numeric) over w3, 3) as pre_l3_points,
            round(avg(points::numeric) over w5, 3) as pre_l5_points,
            round(avg(points::numeric) over w10, 3) as pre_l10_points,

            round(avg(field_goals_attempted::numeric) over w3, 3) as pre_l3_fga,
            round(avg(field_goals_attempted::numeric) over w5, 3) as pre_l5_fga,
            round(avg(field_goals_attempted::numeric) over w10, 3) as pre_l10_fga,

            round(avg(three_pointers_attempted::numeric) over w3, 3) as pre_l3_3pa,
            round(avg(three_pointers_attempted::numeric) over w5, 3) as pre_l5_3pa,
            round(avg(three_pointers_attempted::numeric) over w10, 3) as pre_l10_3pa,

            round(avg(free_throws_attempted::numeric) over w3, 3) as pre_l3_fta,
            round(avg(free_throws_attempted::numeric) over w5, 3) as pre_l5_fta,
            round(avg(free_throws_attempted::numeric) over w10, 3) as pre_l10_fta,

            round(avg(case when is_starter then 1.0 else 0.0 end) over w5, 4)
                as pre_l5_starter_rate,
            round(avg(case when is_starter then 1.0 else 0.0 end) over w10, 4)
                as pre_l10_starter_rate,
            lag(is_starter) over wall as pre_last_was_starter,

            round(
                60.0 * sum(points) over w10
                / nullif(sum(seconds_played) over w10, 0),
                4
            ) as pre_l10_points_per_minute,
            round(
                60.0 * sum(field_goals_attempted) over w10
                / nullif(sum(seconds_played) over w10, 0),
                4
            ) as pre_l10_fga_per_minute
        from player_base
        window
            wall as (
                partition by season_code, player_id
                order by game_date, gamecode
            ),
            w3 as (
                partition by season_code, player_id
                order by game_date, gamecode
                rows between 3 preceding and 1 preceding
            ),
            w5 as (
                partition by season_code, player_id
                order by game_date, gamecode
                rows between 5 preceding and 1 preceding
            ),
            w10 as (
                partition by season_code, player_id
                order by game_date, gamecode
                rows between 10 preceding and 1 preceding
            )
    ),
    team_features as (
        select
            season_code,
            gamecode,
            team_code,
            round(
                100.0 * sum(points) over w5
                / nullif(sum(possessions) over w5, 0),
                3
            ) as pre_l5_off_rating,
            round(
                100.0 * sum(opponent_points) over w5
                / nullif(sum(opponent_possessions) over w5, 0),
                3
            ) as pre_l5_def_rating,
            round(avg(possessions::numeric) over w5, 3) as pre_l5_possessions
        from v_team_game
        where season_code = any(%s)
          and not excluded_by_default
        window w5 as (
            partition by season_code, team_code
            order by utc_date::date, gamecode
            rows between 5 preceding and 1 preceding
        )
    )
    select
        pf.season_code,
        pf.gamecode,
        pf.game_date,
        pf.player_id,
        pf.player_name,
        pf.team_code,
        pf.opponent_team_code,
        pf.is_home,

        pf.pre_history_games,
        pf.pre_l3_minutes,
        pf.pre_l5_minutes,
        pf.pre_l10_minutes,
        pf.pre_l3_points,
        pf.pre_l5_points,
        pf.pre_l10_points,
        pf.pre_l3_fga,
        pf.pre_l5_fga,
        pf.pre_l10_fga,
        pf.pre_l3_3pa,
        pf.pre_l5_3pa,
        pf.pre_l10_3pa,
        pf.pre_l3_fta,
        pf.pre_l5_fta,
        pf.pre_l10_fta,
        pf.pre_l5_starter_rate,
        pf.pre_l10_starter_rate,
        pf.pre_last_was_starter,
        pf.pre_l10_points_per_minute,
        pf.pre_l10_fga_per_minute,

        ot.pre_l5_off_rating as opponent_pre_l5_off_rating,
        ot.pre_l5_def_rating as opponent_pre_l5_def_rating,
        ot.pre_l5_possessions as opponent_pre_l5_possessions,

        round(pf.pre_l3_minutes - pf.pre_l10_minutes, 3)
            as pre_minutes_trend_l3_vs_l10,
        round(pf.pre_l3_fga - pf.pre_l10_fga, 3)
            as pre_fga_trend_l3_vs_l10,
        round(pf.pre_l3_points - pf.pre_l10_points, 3)
            as pre_points_trend_l3_vs_l10,

        pf.points as target_points,
        round(pf.seconds_played::numeric / 60.0, 3) as target_minutes,
        pf.field_goals_attempted as target_fga,
        pf.three_pointers_attempted as target_3pa,
        pf.free_throws_attempted as target_fta,
        pf.is_starter as target_was_starter
    from player_features pf
    left join team_features ot
      on ot.season_code = pf.season_code
     and ot.gamecode = pf.gamecode
     and ot.team_code = pf.opponent_team_code
    where pf.pre_history_games >= %s
    order by pf.game_date, pf.gamecode, pf.player_id
    """


def export_training_dataset(
    *,
    seasons: Iterable[str],
    output_path: Path | str,
    minutes_basis: str = "official",
    min_history_games: int = 3,
) -> dict[str, Any]:
    """Write the complete feature set with one database query and no MCP paging."""
    season_list = [str(season).strip() for season in seasons if str(season).strip()]
    if not season_list:
        raise ValueError("At least one season code is required.")
    if min_history_games < 1 or min_history_games > 10:
        raise ValueError("min_history_games must be between 1 and 10.")

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    settings = DatabaseSettings.from_env()
    connection = connect(settings)
    row_count = 0
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                training_dataset_sql(minutes_basis),
                (season_list, season_list, min_history_games),
            )
            columns = [column[0] for column in cursor.description]
            with destination.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(columns)
                while True:
                    batch = cursor.fetchmany(1000)
                    if not batch:
                        break
                    writer.writerows(batch)
                    row_count += len(batch)
    finally:
        connection.close()

    return {
        "output_path": str(destination),
        "rows": row_count,
        "seasons": season_list,
        "minutes_basis": minutes_basis,
        "min_history_games": min_history_games,
    }
