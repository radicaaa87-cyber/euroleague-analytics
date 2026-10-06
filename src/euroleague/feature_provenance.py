"""Feature provenance for player-points model inputs.

The model consumes numeric features, but operators need to know where each
signal came from. This module assigns every legal feature to a source family.
The mapping is metadata only; it does not alter feature values or model fitting.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FeatureProvenance:
    feature: str
    source_family: str
    source_surface: str
    interpretation: str


def feature_provenance(feature: str) -> FeatureProvenance:
    """Return the source family behind one legal model feature."""
    name = feature.lower()

    if feature == "is_home":
        return FeatureProvenance(
            feature,
            "schedule_travel",
            "EuroLeague schedule/team-game",
            "Static home/away game context.",
        )

    if (
        name.startswith("pre_context_")
        or name.startswith("pre_self_")
        or name.startswith("pre_teammate_")
    ):
        return FeatureProvenance(
            feature,
            "pregame_news",
            "pregame_context_event",
            "Timestamped pre-tipoff availability/news evidence with source confidence.",
        )

    if name.startswith("pre_acb_") or name.startswith("pre_combined_"):
        return FeatureProvenance(
            feature,
            "acb",
            "ACB player-game + canonical athlete identity",
            "Domestic-league role, workload and form before the EuroLeague target game.",
        )

    if "top_pair" in name or "top_triple" in name or "top_lineup" in name:
        return FeatureProvenance(
            feature,
            "lineup_synergy",
            "EuroLeague lineup_stint/v_lineup_player",
            "Historical shared-court combinations and lineup effectiveness.",
        )

    if "matchup" in name or "opponent_same_position" in name or "opponent_rotation" in name:
        return FeatureProvenance(
            feature,
            "matchup",
            "EuroLeague lineup/PBP + roster",
            "Opponent body/position/rotation profile and historical response to similar defenders.",
        )

    if name.startswith("pre_role2_"):
        return FeatureProvenance(
            feature,
            "pbp_role_engine",
            "EuroLeague possessions/lineups + player box score",
            (
                "Role Engine 2.0: possession-normalized scoring volume, offensive "
                "option hierarchy and teammate on/off attempt expansion."
            ),
        )

    if name.startswith("pre_rotation_"):
        return FeatureProvenance(
            feature,
            "rotation_engine",
            "EuroLeague lineup stints",
            (
                "Rotation Engine: first-stint pattern, substitution cadence, late-game "
                "presence and closing-minute role."
            ),
        )

    if "pbp_" in name:
        return FeatureProvenance(
            feature,
            "pbp_lineup",
            "EuroLeague possessions/lineup stints",
            "On-court possessions, ratings, stint structure and lineup concentration.",
        )

    if any(
        token in name
        for token in (
            "travel",
            "rest",
            "away",
            "home_away",
            "games_last_7d",
            "games_last_14d",
            "minutes_last_7d",
            "minutes_last_14d",
            "days_since",
            "hours_rest",
        )
    ):
        return FeatureProvenance(
            feature,
            "schedule_travel",
            "EuroLeague schedule + game locations",
            "Rest, schedule density, home/away sequence and travel load.",
        )

    if any(
        token in name
        for token in (
            "hot_",
            "cold_",
            "hand_state",
            "ts_trend",
            "ts_proxy",
            "eff_reversion",
            "eff_recovery",
        )
    ):
        return FeatureProvenance(
            feature,
            "efficiency_state",
            "EuroLeague player box score",
            "Leakage-safe shooting-efficiency state and hot/cold episode history.",
        )

    if any(
        token in name
        for token in (
            "_std",
            "_range",
            "_cv",
            "spike",
            "drop",
            "starter_change",
            "situational",
            "context_neutral",
        )
    ):
        return FeatureProvenance(
            feature,
            "role_volatility",
            "EuroLeague player box score + PBP game script",
            "Minute/attempt variance and attribution of role-driven versus situational changes.",
        )

    if name.startswith("pre_team_") or name.startswith("pre_opponent_l5_"):
        return FeatureProvenance(
            feature,
            "team_environment",
            "EuroLeague team box score",
            "Pregame team/opponent pace and efficiency environment.",
        )

    if any(
        token in name
        for token in (
            "minutes",
            "fga",
            "3pa",
            "fta",
            "starter",
            "points",
            "2p_pct",
            "3p_pct",
            "ft_pct",
            "naive",
        )
    ):
        return FeatureProvenance(
            feature,
            "player_role_boxscore",
            "EuroLeague player box score",
            "Pregame player role, volume, scoring and shot-efficiency history.",
        )

    if any(token in name for token in ("height", "weight", "is_guard", "is_forward", "is_center")):
        return FeatureProvenance(
            feature,
            "roster",
            "EuroLeague roster",
            "Player physical and positional profile.",
        )

    return FeatureProvenance(
        feature,
        "other_derived",
        "Derived pregame warehouse feature",
        "Leakage-safe derived feature not assigned to a narrower source family.",
    )


def provenance_manifest(features: list[str]) -> list[dict[str, str]]:
    """Serialize provenance for a model artifact/report."""
    return [
        {
            "feature": item.feature,
            "source_family": item.source_family,
            "source_surface": item.source_surface,
            "interpretation": item.interpretation,
        }
        for item in (feature_provenance(feature) for feature in features)
    ]
