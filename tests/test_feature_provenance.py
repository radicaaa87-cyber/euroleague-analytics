from euroleague.feature_provenance import feature_provenance, provenance_manifest


def test_feature_provenance_tracks_primary_signal_sources() -> None:
    cases = {
        "pre_l10_minutes": "player_role_boxscore",
        "pre_l10_pbp_on_off_rating": "pbp_lineup",
        "pre_l5_top_pair_net_rating": "lineup_synergy",
        "pre_matchup_height_diff_cm": "matchup",
        "pre_acb_l5_minutes": "acb",
        "pre_context_official_event_count": "pregame_news",
        "pre_teammate_out_vacated_minutes_l5": "pregame_news",
        "pre_travel_air_km": "schedule_travel",
        "pre_hot_break_eff_reversion_rate": "efficiency_state",
        "pre_l10_fga_std": "role_volatility",
        "pre_team_l5_off_rating": "team_environment",
        "pre_player_height_cm": "roster",
        "is_home": "schedule_travel",
    }
    for feature, expected_family in cases.items():
        assert feature_provenance(feature).source_family == expected_family


def test_provenance_manifest_keeps_one_entry_per_feature() -> None:
    features = ["pre_l10_minutes", "pre_acb_l5_minutes", "is_home"]
    manifest = provenance_manifest(features)

    assert [item["feature"] for item in manifest] == features
    assert all(item["source_surface"] for item in manifest)
    assert all(item["interpretation"] for item in manifest)
