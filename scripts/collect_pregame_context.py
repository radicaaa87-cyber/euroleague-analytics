"""Collect timestamped 72-hour pregame context from public news RSS.

Discovery uses Google News RSS only as a search surface.  Publisher name/url,
headline, snippet, publication timestamp and the discovery URL are persisted so
the evidence can be audited later.  Articles outside the 72-hour pre-tipoff
window are discarded before database insertion.
"""

from __future__ import annotations

import argparse
import email.utils
import html
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg

from euroleague.config import DatabaseSettings
from euroleague.pregame_context import (
    classify_context_text,
    in_pregame_window,
    role_query,
    team_query,
)

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
USER_AGENT = "euroleague-analytics-pregame-context/1.0"


@dataclass(frozen=True)
class UpcomingGame:
    season_code: str
    gamecode: int
    tipoff: datetime
    team_code: str
    team_name: str


@dataclass(frozen=True)
class RotationPlayer:
    player_id: str
    player_name: str


@dataclass(frozen=True)
class NewsItem:
    headline: str
    url: str
    source_name: str
    publisher_url: str
    summary: str
    published_at: datetime


def _strip_html(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _rss_items(query: str, *, limit: int) -> list[NewsItem]:
    params = urllib.parse.urlencode(
        {
            "q": query,
            "hl": "en",
            "gl": "US",
            "ceid": "US:en",
        }
    )
    request = urllib.request.Request(
        f"{GOOGLE_NEWS_RSS}?{params}",
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = response.read()

    root = ET.fromstring(payload)
    result: list[NewsItem] = []
    for item in root.findall("./channel/item")[:limit]:
        title = _strip_html(item.findtext("title") or "")
        link = (item.findtext("link") or "").strip()
        published_raw = item.findtext("pubDate") or ""
        source_node = item.find("source")
        source_name = _strip_html(source_node.text if source_node is not None else "")
        publisher_url = (
            (source_node.attrib.get("url") or "").strip() if source_node is not None else ""
        )
        summary = _strip_html(item.findtext("description") or "")

        try:
            published = email.utils.parsedate_to_datetime(published_raw)
        except TypeError, ValueError:
            continue
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)

        if title and link:
            result.append(
                NewsItem(
                    headline=title,
                    url=link,
                    source_name=source_name,
                    publisher_url=publisher_url,
                    summary=summary,
                    published_at=published.astimezone(UTC),
                )
            )
    return result


def _upcoming_games(connection: psycopg.Connection[Any]) -> list[UpcomingGame]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            select
                season_code,
                gamecode,
                utc_date,
                home_team_code,
                home_team_name,
                away_team_code,
                away_team_name
            from v_game
            where not played
              and utc_date > now()
              and utc_date <= now() + interval '72 hours'
            order by utc_date, gamecode
            """
        )
        result: list[UpcomingGame] = []
        for row in cursor.fetchall():
            season, gamecode, tipoff, home_code, home_name, away_code, away_name = row
            result.append(UpcomingGame(season, gamecode, tipoff, home_code, home_name or home_code))
            result.append(UpcomingGame(season, gamecode, tipoff, away_code, away_name or away_code))
        return result


def _rotation_players(
    connection: psycopg.Connection[Any],
    game: UpcomingGame,
    *,
    limit: int,
) -> list[RotationPlayer]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            with recent as (
                select
                    p.player_id,
                    p.player_name,
                    p.seconds_official,
                    row_number() over (
                        partition by p.player_id
                        order by p.utc_date desc, p.gamecode desc
                    ) as recency_rank
                from v_player_game p
                where p.team_code = %s
                  and p.utc_date < %s
                  and p.utc_date >= %s - interval '60 days'
                  and p.seconds_official > 0
                  and not p.excluded_by_default
            )
            select
                player_id,
                max(player_name) as player_name,
                avg(seconds_official) filter (where recency_rank <= 5) as recent_seconds
            from recent
            group by player_id
            order by recent_seconds desc nulls last
            limit %s
            """,
            (game.team_code, game.tipoff, game.tipoff, limit),
        )
        return [RotationPlayer(row[0], row[1]) for row in cursor.fetchall()]


def _insert_item(
    connection: psycopg.Connection[Any],
    *,
    game: UpcomingGame,
    player: RotationPlayer | None,
    query: str,
    item: NewsItem,
) -> bool:
    if not in_pregame_window(item.published_at, game.tipoff):
        return False

    classification = classify_context_text(
        item.headline,
        item.summary,
        source_name=item.source_name,
        publisher_url=item.publisher_url,
        team_name=game.team_name,
    )
    if classification.event_type == "other":
        return False

    with connection.cursor() as cursor:
        cursor.execute(
            """
            insert into pregame_context_event (
                season_code,
                gamecode,
                team_code,
                player_id,
                event_type,
                role_direction,
                severity,
                source_confidence,
                source_name,
                source_url,
                publisher_url,
                headline,
                summary,
                published_at,
                fetched_at,
                query_text,
                classification_method,
                metadata
            )
            values (
                %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, now(), %s, 'keyword_v1', %s::jsonb
            )
            on conflict do nothing
            returning context_id
            """,
            (
                game.season_code,
                game.gamecode,
                game.team_code,
                player.player_id if player else None,
                classification.event_type,
                classification.role_direction,
                classification.severity,
                classification.source_confidence,
                item.source_name or None,
                item.url,
                item.publisher_url or None,
                item.headline,
                item.summary or None,
                item.published_at,
                query,
                "{}",
            ),
        )
        return cursor.fetchone() is not None


def _record_collection(
    connection: psycopg.Connection[Any],
    *,
    game: UpcomingGame,
    query_count: int,
    successful_query_count: int,
    players_queried: int,
    items_seen: int,
    inserted_event_count: int,
) -> None:
    failed_query_count = query_count - successful_query_count
    with connection.cursor() as cursor:
        cursor.execute(
            """
            insert into pregame_context_collection (
                season_code,
                gamecode,
                team_code,
                query_count,
                successful_query_count,
                failed_query_count,
                players_queried,
                items_seen,
                inserted_event_count,
                collector_version,
                metadata
            )
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'rss_v1', '{}'::jsonb)
            """,
            (
                game.season_code,
                game.gamecode,
                game.team_code,
                query_count,
                successful_query_count,
                failed_query_count,
                players_queried,
                items_seen,
                inserted_event_count,
            ),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--players-per-team", type=int, default=10)
    parser.add_argument("--items-per-query", type=int, default=8)
    args = parser.parse_args(argv)

    if not 1 <= args.players_per_team <= 15:
        raise ValueError("players-per-team must be between 1 and 15.")
    if not 1 <= args.items_per_query <= 20:
        raise ValueError("items-per-query must be between 1 and 20.")

    settings = DatabaseSettings.from_env()
    inserted = 0
    queries = 0

    with psycopg.connect(
        settings.url(),
        autocommit=True,
        prepare_threshold=None,
    ) as connection:
        games = _upcoming_games(connection)
        for game in games:
            game_inserted_before = inserted
            game_queries = 0
            game_successful_queries = 0
            game_items_seen = 0

            query = team_query(game.team_name)
            queries += 1
            game_queries += 1
            try:
                items = _rss_items(query, limit=args.items_per_query)
                game_successful_queries += 1
            except OSError, ET.ParseError:
                items = []
            game_items_seen += len(items)
            for item in items:
                inserted += int(
                    _insert_item(
                        connection,
                        game=game,
                        player=None,
                        query=query,
                        item=item,
                    )
                )

            players = _rotation_players(
                connection,
                game,
                limit=args.players_per_team,
            )
            for player in players:
                query = role_query(player.player_name, game.team_name)
                queries += 1
                game_queries += 1
                try:
                    items = _rss_items(query, limit=args.items_per_query)
                    game_successful_queries += 1
                except OSError, ET.ParseError:
                    continue
                game_items_seen += len(items)
                for item in items:
                    inserted += int(
                        _insert_item(
                            connection,
                            game=game,
                            player=player,
                            query=query,
                            item=item,
                        )
                    )

            _record_collection(
                connection,
                game=game,
                query_count=game_queries,
                successful_query_count=game_successful_queries,
                players_queried=len(players),
                items_seen=game_items_seen,
                inserted_event_count=inserted - game_inserted_before,
            )

    print(f"pregame_context games_teams={len(games)} queries={queries} inserted={inserted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
