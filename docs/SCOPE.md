# Scope of version 1

Written 2026-09-06, the day the roadmap's eight phases were all found closed
and the goal queue empty. This document exists so that a request can be sorted
into one of three bins before anyone writes code: **missing**, **left out on
purpose**, or **never**. Without it, every request looks like a new phase.

The rule that makes the first list trustworthy: `tests/test_scope_document.py`
reads the tool names from the registry and fails if this table and the registry
disagree in either direction. A tool added without a line here does not ship.

The reason for the second list is in `DECISIONS.md` item 65. The one-line
version: the value of this project is the derived layer, and a season total the
league already publishes is not derived.

## What version 1 does

18 read-only tools, served identically over stdio and HTTP. Every response
states its data coverage, the games it excludes, and whether a minute figure is
raw or corrected.

| Tool | What it answers |
|---|---|
| `el_describe_warehouse` | Which seasons are loaded, how many games, which games are excluded and why. |
| `el_find_games` | Which games match a season, round, date, team or result. |
| `el_get_game` | One game: score, pace, the four factors, the exact possession count, quality flags. |
| `el_get_boxscore` | The official box score for one game, with raw, corrected and official minutes side by side. |
| `el_get_play_by_play` | The event stream in source order, with the five players on court and the running margin. |
| `el_get_shot_data` | Shot attempts with half-court coordinates. Free throws carry no coordinate. |
| `el_get_team_stats` | A team's season: four factors, offensive and defensive rating, pace. |
| `el_get_player_stats` | A player's season, per game and per 100 possessions. |\n| `el_get_player_model_context` | Compact pre-game player context for role, usage, rotation, possessions and opponent matchup. |
| `el_get_lineup_stats` | Five-player lineups: possessions, offensive, defensive and net rating. |
| `el_get_player_on_off` | The team's net rating with a player on court against off court; accepts the caller's clutch thresholds (max_seconds_remaining, max_margin), applied to both splits. |
| `el_get_possessions` | Possession rows with start margin, clock and duration in seconds, so any clutch or transition definition is a filter (max_seconds_remaining, max_margin, max_duration_seconds); can aggregate by team, end reason, or both. |
| `el_get_fouls` | Fouls committed and drawn by type, per player, team or game; reconciles to the box score. |
| `el_get_referee_stats` | A referee's season: games worked, fouls per game, home-win rate, pace; unpivoted from the schedule's officiating crew. |
| `el_get_roster` | A team's roster with biography (jersey, position, height, weight, birth date, country), linked to the box-score player by observed stat lines, never by name. |
| `el_acb_find_games` | Liga Endesa games for an ACB season, optionally filtered by team or date. |
| `el_acb_get_player_games` | One Liga Endesa player's game-by-game source box score, including minutes and shot volume. |
| `el_acb_get_play_by_play` | One Liga Endesa match's source-order event stream by ACB match id. |

Under the tools, the warehouse: three v1 game endpoints (`Boxscore`,
`PlaybyPlay`, `Points`) and one v2 endpoint (the per-season people list, for
rosters before a season starts), cached byte-for-byte with checksums, parsed,
and rebuilt into possessions, matchup-bounded stints, lineups and corrected
minutes. Every derived number has a validation test with either external ground
truth or a mechanical invariant.

## What it deliberately does not do

Each of these exists in the public API. Measured 2026-09-06 in
`exploration/SEASON_ENDPOINT_PROBE.md`. Each is left out because it is either a
number the warehouse already derives exactly, or a number the league publishes
that adds no derived value.

| Available in the API | Why it is not here |
|---|---|
| Season statistics per player and per club (v2 `people/{id}/stats`, `clubs/{code}/stats`, v3 `statistics/*/traditional`) | The warehouse computes season lines from the event stream, with possession denominators the league does not publish. Loading the league's own totals would give a second answer to the same question with no way to say which is right. Fetched and archived as a test oracle since Decision 78; never served. |
| Standings per round (v2 `rounds/{n}/standings`) | Wins and losses are already in `raw_game`. A standings tool is a sort, not a metric. |
| The season schedule (v2 `games`) | Fixtures are already read for the settlement chain; a schedule tool would answer "when does Efes play" and nothing about how they play. |
| `Evolution`: score margin per minute | Derived exactly by `el_get_play_by_play` from the running score, at event resolution rather than minute resolution. |
| `Comparison` and `ShootingGraphic`: team-level summary totals | Recomputable from the event stream; `exploration/FINDINGS.md` records why storing them would be storing a second, unverifiable copy. |
| `Header`: arena, capacity, referees, coaches | Referees already come from `Boxscore.Referees` and are exposed by `el_get_game`. The rest is metadata about the venue, not about play. |
| Player biographies, heights, birthdates (v2 `people`) | Stored where the roster needs them, not exposed as a tool. This is a basketball warehouse, not a player database. |
| Advanced shot metrics beyond location: shot quality, closest defender, shot type inference | Not in the data. A shot chart with coordinates is the ceiling of what the public API allows. |

Any of these can move to the first list by a decision recorded in `DECISIONS.md`
with its reason, a validation test, and a line in the table above. Not by a
pull request alone.

## What it will never do

- Video, clips, or broadcast footage. Copyright, and the reason the largest
  Turkish EuroLeague account has been shut down twice.
- Tracking data at 25 frames per second. Not public, and not inferable from the
  play-by-play.
- Possessions estimated from box-score formulas. The event stream gives the
  exact count; an estimate would be a regression.
- Metrics with neither external ground truth nor a mechanical invariant.
- Any endpoint that requires scraping a site that forbids it.

## What "done" means from here

Version 1 is done in the sense the rules define: every phase gate passed, every
derived metric tested, the tool surface frozen by this document. It is not done
in the sense `CONTEXT.md` defines, "would a club's analytics staff respect
this", because nobody outside the invite list has used it. That is Phase 9 in
`ROADMAP.md`, and it is the only phase left.
