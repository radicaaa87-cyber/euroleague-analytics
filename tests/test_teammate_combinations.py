from __future__ import annotations

from euroleague.teammate_combinations import KEY_LINEUP_SQL, TEAMMATE_TRIPLE_SQL


def test_triple_context_is_leakage_safe() -> None:
    sql = TEAMMATE_TRIPLE_SQL.lower()

    assert "p.utc_date < c.at" in sql
    assert "rn <= %s" in sql
    assert "not p.excluded_by_default" in sql
    assert "lineup_stint" in sql


def test_triple_context_requires_strict_sample_before_directional_label() -> None:
    sql = TEAMMATE_TRIPLE_SQL.lower()

    assert "games_together < 5" in sql
    assert "shared_minutes, 0) < 75" in sql
    assert "games_without_combo < 3" in sql
    assert "target_points_per_minute_delta" in sql
    assert "target_fga_per_minute_delta" in sql
    assert "shared_net_rating" in sql
    assert "'small_sample'" in sql
    assert "'positive'" in sql
    assert "'negative'" in sql


def test_key_lineup_context_uses_exact_five_man_units_and_strict_sample() -> None:
    sql = KEY_LINEUP_SQL.lower()

    assert "target_lineups" in sql
    assert "v_lineup_player" in sql
    assert "lineup_stint" in sql
    assert "jsonb_agg" in sql
    assert "ls.games < 4" in sql
    assert "ls.shared_minutes < 60" in sql
    assert "ls.team_possessions < 50" in sql
    assert "off_rating" in sql
    assert "def_rating" in sql
    assert "net_rating" in sql
    assert "'small_sample'" in sql


def test_key_lineup_context_is_cut_off_before_target_time() -> None:
    sql = KEY_LINEUP_SQL.lower()

    assert "p.utc_date < c.at" in sql
    assert "rn <= %s" in sql
    assert "not p.excluded_by_default" in sql
