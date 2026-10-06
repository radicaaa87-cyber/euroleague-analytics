from __future__ import annotations

from euroleague.teammate_synergy import TEAMMATE_SYNERGY_SQL


def test_teammate_synergy_is_cut_off_before_target_time() -> None:
    sql = TEAMMATE_SYNERGY_SQL.lower()

    assert "p.utc_date < c.at" in sql
    assert "rn <= %s" in sql
    assert "not p.excluded_by_default" in sql


def test_pair_context_combines_game_level_role_and_actual_shared_stints() -> None:
    sql = TEAMMATE_SYNERGY_SQL.lower()

    assert "target_points_per_minute_with" in sql
    assert "target_points_per_minute_without" in sql
    assert "target_fga_per_minute_with" in sql
    assert "target_fga_per_minute_without" in sql
    assert "lineup_stint" in sql
    assert "shared_minutes" in sql
    assert "shared_off_rating" in sql
    assert "shared_def_rating" in sql
    assert "shared_net_rating" in sql


def test_pair_context_marks_small_samples_instead_of_overclaiming_synergy() -> None:
    sql = TEAMMATE_SYNERGY_SQL.lower()

    assert "games_together < 4" in sql
    assert "shared_minutes, 0) < 40" in sql
    assert "games_without < 2" in sql
    assert "'small_sample'" in sql
    assert "'positive'" in sql
    assert "'negative'" in sql
    assert "'mixed'" in sql
