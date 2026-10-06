"""Pregame web/news context classification and leakage guards.

This layer is intentionally separate from the historical statistical model.
Until enough timestamped context has been archived, the structured signals are
used for forward role/minutes/FGA scenarios rather than retrofitted into old
blind tests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlparse

CONTEXT_WINDOW_HOURS = 72

_OUT = (
    "ruled out",
    "will not play",
    "won't play",
    "unavailable",
    "sidelined",
    "suspended",
    "descartado",
    "no jugará",
    "no jugara",
    "sancionado",
    "baja",
)
_INJURY_MENTION = (
    "injured",
    "injury",
    "lesionado",
    "lesión",
    "lesion",
)
_DOUBT = (
    "questionable",
    "doubtful",
    "game-time decision",
    "uncertain",
    "duda",
    "dudoso",
    "entre algodones",
)
_RETURN = (
    "returns",
    "returning",
    "cleared to play",
    "available again",
    "back in the lineup",
    "recovered",
    "vuelve",
    "regresa",
    "disponible",
    "alta médica",
    "alta medica",
)
_ROLE_UP = (
    "will start",
    "starting lineup",
    "bigger role",
    "increased role",
    "more minutes",
    "starter",
    "titular",
    "más minutos",
    "mas minutos",
    "mayor protagonismo",
)
_ROLE_DOWN = (
    "minutes restriction",
    "limited minutes",
    "reduced role",
    "moved to the bench",
    "bench role",
    "suplente",
    "minutos limitados",
    "restricción de minutos",
    "restriccion de minutos",
)
_ROTATION = (
    "rotation",
    "lineup change",
    "rotation change",
    "rotación",
    "rotacion",
    "cambio de quinteto",
)
_ROSTER = (
    "signed",
    "signing",
    "released",
    "waived",
    "transfer",
    "fichaje",
    "ficha por",
    "rescinde",
)


@dataclass(frozen=True)
class ContextClassification:
    event_type: str
    role_direction: int
    severity: float
    source_confidence: float
    role_impact_score: float


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def classify_context_text(
    headline: str,
    summary: str = "",
    *,
    source_name: str = "",
    publisher_url: str = "",
    team_name: str = "",
) -> ContextClassification:
    """Convert one article snippet into a conservative structured role signal."""
    text = re.sub(r"\s+", " ", f"{headline} {summary}").casefold()

    if _contains_any(text, _OUT):
        event_type, direction, severity = "availability_out", -1, 1.0
    elif _contains_any(text, _DOUBT):
        event_type, direction, severity = "availability_doubt", -1, 0.60
    elif _contains_any(text, _RETURN):
        event_type, direction, severity = "return", 1, 0.70
    elif _contains_any(text, _INJURY_MENTION):
        # A generic injury mention is evidence of uncertainty, not proof of an
        # absence. This also prevents phrases such as "returns after injury"
        # from being classified as OUT.
        event_type, direction, severity = "availability_doubt", -1, 0.45
    elif _contains_any(text, _ROLE_DOWN):
        event_type, direction, severity = "role_down", -1, 0.60
    elif _contains_any(text, _ROLE_UP):
        event_type, direction, severity = "role_up", 1, 0.60
    elif _contains_any(text, _ROTATION):
        event_type, direction, severity = "rotation_change", 0, 0.45
    elif _contains_any(text, _ROSTER):
        event_type, direction, severity = "roster_change", 0, 0.45
    else:
        event_type, direction, severity = "other", 0, 0.0

    confidence = source_confidence(
        source_name=source_name,
        publisher_url=publisher_url,
        team_name=team_name,
    )
    impact = round(direction * severity * confidence, 5)
    return ContextClassification(
        event_type=event_type,
        role_direction=direction,
        severity=severity,
        source_confidence=confidence,
        role_impact_score=impact,
    )


def source_confidence(
    *,
    source_name: str = "",
    publisher_url: str = "",
    team_name: str = "",
) -> float:
    """Score provenance, not whether the article's claim is ultimately true."""
    host = urlparse(publisher_url).netloc.casefold().removeprefix("www.")
    if host.endswith("euroleaguebasketball.net") or host.endswith("acb.com"):
        return 0.95

    source = re.sub(r"[^a-z0-9]+", " ", source_name.casefold()).strip()
    team = re.sub(r"[^a-z0-9]+", " ", team_name.casefold()).strip()
    if team and source and (team in source or source in team):
        return 0.90
    return 0.65


def in_pregame_window(
    published_at: datetime,
    tipoff_at: datetime,
    *,
    hours: int = CONTEXT_WINDOW_HOURS,
) -> bool:
    """True only for evidence known in the requested pre-tipoff window."""
    if published_at.tzinfo is None or tipoff_at.tzinfo is None:
        raise ValueError("published_at and tipoff_at must be timezone-aware.")
    return tipoff_at - timedelta(hours=hours) <= published_at < tipoff_at


def role_query(player_name: str, team_name: str) -> str:
    terms = (
        "injury OR injured OR out OR doubtful OR return OR role OR minutes OR "
        "baja OR lesionado OR duda OR vuelve OR titular OR minutos"
    )
    return f'"{player_name}" "{team_name}" ({terms})'


def team_query(team_name: str) -> str:
    terms = (
        "injury OR injured OR out OR doubtful OR rotation OR lineup OR "
        "baja OR lesionado OR duda OR rotación OR quinteto"
    )
    return f'"{team_name}" ({terms})'


ROLE_CONTEXT_SQL = """
with target as (
    select
        g.season_code,
        g.gamecode,
        g.utc_date as tipoff,
        r.team_code,
        r.position_name,
        (r.team_code = g.home_team_code) as is_home,
        case
            when r.team_code = g.home_team_code then g.away_team_code
            else g.home_team_code
        end as opponent_team_code
    from v_game g
    join v_roster r
      on r.season_code = g.season_code
     and r.player_id = %(player_id)s
     and r.team_code in (g.home_team_code, g.away_team_code)
    where g.season_code = %(season_code)s
      and g.gamecode = %(gamecode)s
),
self_context as (
    select
        coalesce(f.context_event_count, 0) as self_event_count,
        coalesce(f.context_role_up_score, 0) as self_role_up_score,
        coalesce(f.context_role_down_score, 0) as self_role_down_score,
        coalesce(f.context_out_score, 0) as self_out_score,
        coalesce(f.context_doubt_score, 0) as self_doubt_score,
        coalesce(f.context_return_score, 0) as self_return_score,
        coalesce(f.context_max_source_confidence, 0)
            as self_max_source_confidence,
        coalesce(f.context_max_severity, 0) as self_max_severity,
        f.context_feature_cutoff_time
    from target t
    left join v_pregame_player_context_features f
      on f.season_code = t.season_code
     and f.gamecode = t.gamecode
     and f.team_code = t.team_code
     and f.player_id = %(player_id)s
),
teammate_signal as (
    select
        e.player_id,
        max(case
            when e.event_type = 'availability_out'
                then e.severity * e.source_confidence
            else 0
        end) as out_score,
        max(case
            when e.event_type = 'availability_doubt'
                then e.severity * e.source_confidence
            else 0
        end) as doubt_score,
        max(e.source_confidence) as source_confidence,
        max(e.severity) as severity,
        max(e.published_at) as event_cutoff_time
    from target t
    join pregame_context_event e
      on e.season_code = t.season_code
     and e.gamecode = t.gamecode
     and e.team_code = t.team_code
     and e.player_id is not null
     and e.player_id <> %(player_id)s
     and e.event_type in ('availability_out', 'availability_doubt')
     and e.published_at >= t.tipoff - interval '72 hours'
     and e.published_at < t.tipoff
    group by e.player_id
),
teammate_role as (
    select
        s.player_id,
        s.out_score,
        s.doubt_score,
        s.source_confidence,
        s.severity,
        s.event_cutoff_time,
        tr.position_name as teammate_position_name,
        recent.avg_minutes_l5,
        recent.avg_fga_l5
    from teammate_signal s
    cross join target t
    left join v_roster tr
      on tr.season_code = t.season_code
     and tr.team_code = t.team_code
     and tr.player_id = s.player_id
    left join lateral (
        select
            round(avg(x.minutes), 3) as avg_minutes_l5,
            round(avg(x.fga), 3) as avg_fga_l5
        from (
            select
                pg.seconds_official::numeric / 60.0 as minutes,
                pg.field_goals_attempted::numeric as fga
            from v_player_game pg
            where pg.player_id = s.player_id
              and pg.team_code = t.team_code
              and pg.utc_date < t.tipoff
              and pg.seconds_official > 0
              and not pg.excluded_by_default
            order by pg.utc_date desc, pg.gamecode desc
            limit 5
        ) x
    ) recent on true
),
team_context as (
    select
        coalesce(f.team_context_event_count, 0) as team_event_count,
        coalesce(f.team_role_up_score, 0) as team_role_up_score,
        coalesce(f.team_role_down_score, 0) as team_role_down_score,
        coalesce(f.team_context_max_source_confidence, 0)
            as team_max_source_confidence,
        coalesce(f.team_context_max_severity, 0) as team_max_severity,
        f.team_context_feature_cutoff_time
    from target t
    left join v_pregame_team_context_features f
      on f.season_code = t.season_code
     and f.gamecode = t.gamecode
     and f.team_code = t.team_code
),
collection_context as (
    select
        count(c.collection_id) as collection_runs,
        coalesce(sum(c.query_count), 0) as query_count,
        coalesce(sum(c.successful_query_count), 0) as successful_query_count,
        coalesce(sum(c.failed_query_count), 0) as failed_query_count,
        coalesce(sum(c.players_queried), 0) as players_queried,
        coalesce(sum(c.items_seen), 0) as items_seen,
        coalesce(sum(c.inserted_event_count), 0) as inserted_event_count,
        case when count(c.collection_id) > 0 then 1 else 0 end as data_available,
        round(
            coalesce(sum(c.successful_query_count), 0)::numeric
            / nullif(coalesce(sum(c.query_count), 0), 0),
            4
        ) as query_success_rate,
        max(c.collected_at) as collection_feature_cutoff_time
    from target t
    left join pregame_context_collection c
      on c.season_code = t.season_code
     and c.gamecode = t.gamecode
     and c.team_code = t.team_code
     and c.collected_at >= t.tipoff - interval '72 hours'
     and c.collected_at < t.tipoff
)
select
    t.tipoff as target_tipoff_utc,
    t.team_code as target_team_code,
    t.opponent_team_code,
    t.is_home,
    sc.self_event_count,
    sc.self_role_up_score,
    sc.self_role_down_score,
    sc.self_out_score,
    sc.self_doubt_score,
    sc.self_return_score,
    sc.self_max_source_confidence,
    sc.self_max_severity,
    tc.team_event_count,
    tc.team_role_up_score,
    tc.team_role_down_score,
    tc.team_max_source_confidence,
    tc.team_max_severity,
    cc.collection_runs as context_collection_runs,
    cc.query_count as context_query_count,
    cc.successful_query_count as context_successful_query_count,
    cc.failed_query_count as context_failed_query_count,
    cc.players_queried as context_players_queried,
    cc.items_seen as context_items_seen,
    cc.inserted_event_count as context_inserted_event_count,
    cc.data_available as context_data_available,
    cc.query_success_rate as context_query_success_rate,
    count(tr.player_id) as teammate_availability_signal_count,
    coalesce(
        jsonb_agg(
            jsonb_build_object(
                'player_id', tr.player_id,
                'position', tr.teammate_position_name,
                'out_score', tr.out_score,
                'doubt_score', tr.doubt_score,
                'avg_minutes_l5', tr.avg_minutes_l5,
                'avg_fga_l5', tr.avg_fga_l5,
                'source_confidence', tr.source_confidence,
                'severity', tr.severity,
                'event_cutoff_time', tr.event_cutoff_time
            )
            order by greatest(tr.out_score, tr.doubt_score) desc, tr.player_id
        ) filter (where tr.player_id is not null),
        '[]'::jsonb
    ) as teammate_availability,
    coalesce(max(tr.source_confidence), 0) as teammate_max_source_confidence,
    coalesce(max(tr.severity), 0) as teammate_max_severity,
    greatest(
        sc.self_max_source_confidence,
        tc.team_max_source_confidence,
        coalesce(max(tr.source_confidence), 0)
    ) as context_max_source_confidence,
    greatest(
        sc.self_max_severity,
        tc.team_max_severity,
        coalesce(max(tr.severity), 0)
    ) as context_max_severity,
    round(sum(coalesce(tr.avg_minutes_l5, 0) * tr.out_score), 3)
        as teammate_out_vacated_minutes_l5,
    round(sum(coalesce(tr.avg_minutes_l5, 0) * tr.doubt_score), 3)
        as teammate_doubt_vacated_minutes_l5,
    round(sum(coalesce(tr.avg_fga_l5, 0) * tr.out_score), 3)
        as teammate_out_vacated_fga_l5,
    round(sum(coalesce(tr.avg_fga_l5, 0) * tr.doubt_score), 3)
        as teammate_doubt_vacated_fga_l5,
    round(sum(
        case
            when tr.teammate_position_name = t.position_name
                then coalesce(tr.avg_minutes_l5, 0) * tr.out_score
            else 0
        end
    ), 3) as same_position_out_vacated_minutes_l5,
    greatest(
        sc.context_feature_cutoff_time,
        tc.team_context_feature_cutoff_time,
        cc.collection_feature_cutoff_time,
        max(tr.event_cutoff_time)
    ) as context_feature_cutoff_time
from target t
cross join self_context sc
cross join team_context tc
cross join collection_context cc
left join teammate_role tr on true
group by
    t.tipoff,
    t.team_code,
    t.opponent_team_code,
    t.is_home,
    t.position_name,
    sc.self_event_count,
    sc.self_role_up_score,
    sc.self_role_down_score,
    sc.self_out_score,
    sc.self_doubt_score,
    sc.self_return_score,
    sc.self_max_source_confidence,
    sc.self_max_severity,
    sc.context_feature_cutoff_time,
    tc.team_event_count,
    tc.team_role_up_score,
    tc.team_role_down_score,
    tc.team_max_source_confidence,
    tc.team_max_severity,
    tc.team_context_feature_cutoff_time,
    cc.collection_runs,
    cc.query_count,
    cc.successful_query_count,
    cc.failed_query_count,
    cc.players_queried,
    cc.items_seen,
    cc.inserted_event_count,
    cc.data_available,
    cc.query_success_rate,
    cc.collection_feature_cutoff_time
"""


def load_role_context_features(
    connection: object,
    *,
    season_code: str,
    gamecode: int,
    player_id: str,
) -> dict[str, object]:
    """Return forward-only role opportunity features for one upcoming player-game."""
    cursor_factory = connection.cursor
    with cursor_factory() as cursor:
        cursor.execute(
            ROLE_CONTEXT_SQL,
            {
                "season_code": season_code,
                "gamecode": gamecode,
                "player_id": player_id,
            },
        )
        columns = [description[0] for description in cursor.description]
        row = cursor.fetchone()
    if row is None:
        return {}
    return dict(zip(columns, row, strict=True))
