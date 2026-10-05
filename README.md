# EuroLeague Analytics

[![CI](https://github.com/egemeny13/euroleague-analytics/actions/workflows/ci.yml/badge.svg)](https://github.com/egemeny13/euroleague-analytics/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Website](https://img.shields.io/badge/Website-euroleague.egemenyucelen.me-blue)](https://euroleague.egemenyucelen.me)
[![Launch Media](https://img.shields.io/badge/Launch-media-orange)](https://github.com/egemeny13/euroleague-analytics-launch)

A validated data warehouse and [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server for EuroLeague and EuroCup basketball, exposing precision play-by-play possessions, lineup on/off splits, four factors, and court shot charts directly to AI language models.

The Remotion launch video, micro-clips and social cards are maintained in the
separate [launch media repository](https://github.com/egemeny13/euroleague-analytics-launch).
The deployable website remains in this repository under `site/`.

---

## 1. What This Is (and Why It Exists)

This is **not** an API wrapper. Thin wrappers already exist and provide little analytical depth.

The value of this project lives entirely in its **precision derived layer**:
- **Exact Possessions**: Counted independently from the event stream across five verified possession-ending criteria, avoiding inaccurate box-score estimation formulas (e.g. `FGA - ORB + TO + 0.44*FTA`).
- **5-Man Lineup Tracking**: Reconstructs substitution batches dynamically to maintain exactly five players on court at all times, computing lineup-level offensive, defensive, and net ratings.
- **On/Off Impact Splits**: Measures team performance differential with any player on court versus on the bench.
- **Court Shot Coordinates**: Links spatial half-court coordinates to individual play-by-play field goal attempts, lineups, and game margins (free throws and null sentinels cleanly excluded from spatial calculations).
- **Dynamic Clutch Filtering**: Clutch state is preserved as `margin_at_start` and `seconds_remaining_at_start` on every possession, allowing callers to query any clutch definition dynamically.

---

## 2. Verified Data Integrity & Invariants

Every number published by this warehouse is mechanically verified against official box scores and strict invariants before shipping:

| Metric / Dimension | Verified Value | Ground Truth & Evidence |
|---|---|---|
| **Loaded Public Games** | **732 games** | 330 in E2024 &bull; 402 in E2025 |
| **Reconstructed Possessions** | **107,311** | 47,829 in E2024 &bull; 59,482 in E2025 |
| **Court Shot Coordinates** | **41,524 verified** | E2024 field goals with real half-court coordinates |
| **Score Reconciliation** | **100.0%** | 0 point discrepancies across all 732 games |
| **Player Minutes Precision** | **99.54%** | Exact second match against official box scores |
| **Historical Archive** | **Backfill in progress** | E2003–E2025 target; every completed season passes a byte-for-byte restore gate |
| **Dual-Path Evaluations** | **10 / 10 passed** | Verified via SQL and live MCP tool calls |

---

## 3. The 17 MCP Tools

The server exposes 17 read-only tools designed specifically for LLMs. Every response declares its data coverage, quarantined game exclusions, and whether minutes are raw or corrected.

| Tool | Purpose |
|---|---|
| `el_describe_warehouse` | Returns loaded seasons, game counts, coverage notes, and data exclusions. |
| `el_find_games` | Search and filter games by season, round, date, team, or winner. |
| `el_get_game` | Single-game scores, pace, Four Factors, exact possession totals, and quality flags. |
| `el_get_boxscore` | Official player and team box scores with raw, corrected, and official minutes. |
| `el_get_play_by_play` | Source-ordered event stream with on-court lineups, score margins, and clock readings. |
| `el_get_shot_data` | Shot attempts with normalized half-court court coordinates (X, Y). |
| `el_get_team_stats` | Four Factors (eFG%, TOV%, ORB%, FTR), pace, offensive rating, and defensive rating. |
| `el_get_player_stats` | Player per-game and per-100 possession statistics. |
| `el_get_lineup_stats` | 5-man lineup performance with possession counts, offensive, defensive, and net ratings. |
| `el_get_player_on_off` | Team net rating differential with a specific player on court versus off court. |
| `el_get_possessions` | Individual possession records with start score, duration, ending reason, and clutch filters. |
| `el_get_fouls` | Fouls committed and drawn by type, grouped by player, team, or game; reconciles to the box score. |
| `el_get_referee_stats` | A referee's season: games worked, fouls per game, home-win rate, and pace; unpivoted from the schedule's officiating crew. |
| `el_get_roster` | A team's roster with biography (jersey, position, height, weight, birth date, country), linked to the box-score player by observed stat lines, never by name. |
| `el_acb_find_games` | Find Liga Endesa games by season, team and date using source-native ACB match ids. |
| `el_acb_get_player_games` | Liga Endesa player game logs with official minutes, points, shot attempts, rebounds, assists, turnovers, plus-minus and valuation. |
| `el_acb_get_play_by_play` | Source-order Liga Endesa play-by-play for one ACB match, with event type, quarter, clock and running score. |

---

## 4. Generic MCP Client Setup

The server remains a standard MCP server. The hosted and local transports publish the
same 17 tools, input schemas, output schemas, and safety annotations. No ChatGPT-specific
metadata is present in the tool registry.

### Hosted Streamable HTTP (recommended)

Use this endpoint in any MCP client that supports remote Streamable HTTP:

```text
https://euroleague-analytics-mcp.fly.dev/mcp
```

For clients that use a JSON MCP configuration, the shape is:

```json
{
  "mcpServers": {
    "euroleague": {
      "url": "https://euroleague-analytics-mcp.fly.dev/mcp"
    }
  }
}
```

The hosted endpoint uses OAuth. The client should follow the authorization discovery
advertised by the server.

The Fly deployment is configured to suspend its single machine when idle and
resume when a request arrives (Decision 86). The first request after idle time
can take longer. Normal resume retains process memory, but a restart or
cold-start fallback may require the MCP client to create a new session.
Persistent HTTP streams and continued requests can keep the machine awake;
suspended root filesystem storage and traffic may still be billed.

The configuration remains in `fly.toml`. A direct live setting can be overwritten
by the next default-branch CI deployment until the corresponding configuration
change is merged. This policy reduces idle compute time; it does not guarantee
a monthly spending cap.

### Local stdio

Clone the repository, configure your PostgreSQL connection string in `.env`, and point Claude Desktop to the local script:

```json
{
  "mcpServers": {
    "euroleague-local": {
      "command": "python",
      "args": ["/path/to/euroleague-analytics/scripts/mcp_server.py"]
    }
  }
}
```

This stdio configuration is suitable for Claude Desktop and other local MCP clients.
Cursor, Windsurf, Gemini/Antigravity, Codex, and custom MCP clients can use either
transport when they support it. Client-specific configuration screens differ, but the
server URL, tool names, and contracts do not.

For troubleshooting, see the [Support & Connection Guide](https://euroleague.egemenyucelen.me/support.html).

## 5. ChatGPT Setup

ChatGPT uses the same hosted MCP endpoint. There is no separate ChatGPT API and no fork
of the tool registry.

1. In ChatGPT, open **Settings → Security and login** and enable **Developer mode**.
2. Open [ChatGPT Plugins](https://chatgpt.com/#settings/Connectors/Advanced).
3. Add a new MCP connection and enter
   `https://euroleague-analytics-mcp.fly.dev/mcp` as the public Streamable HTTP URL.
4. Complete the OAuth sign-in.
5. Review the discovered 17 tools and start a new conversation with the connection enabled.

Developer mode availability can depend on the ChatGPT account and workspace policy. The
current official flow is documented in OpenAI's
[Connect and test your plugin](https://developers.openai.com/plugins/deploy/connect-chatgpt)
guide.

### OpenAI public directory submission

OpenAI's current documentation redirects the former Apps SDK/App Directory material to
the Plugins documentation. An MCP-only submission is supported and custom UI is optional.
This repository therefore keeps the integration deliberately thin:

- all three standard safety annotations are explicit on every tool;
- every structured result is described by the existing shared response-envelope schema;
- the portable registry contains no `openai/*` fields or ChatGPT-only `_meta` values;
- `OPENAI_APPS_CHALLENGE_TOKEN` optionally exposes the exact domain-verification response
  at `/.well-known/openai-apps-challenge`; when unset, that route does not exist;
- no custom UI resource is registered because all workflows are complete through normal
  MCP tool results.

Before submission, the publisher must still complete the external portal work: identity
verification, listing copy and assets, privacy/terms/support URLs, five positive and three
negative test cases, country availability, OAuth details, a live Scan Tools run, and domain
verification. See OpenAI's
[submission guide](https://developers.openai.com/plugins/deploy/submission) and
[MCP review requirements](https://developers.openai.com/plugins/deploy/app-review).

---

## 6. Architecture

```
                                  live.euroleague.net API
                                             |
                         [Scheduled GitHub Actions Pipeline]
                                             |
                +----------------------------+----------------------------+
                |                                                         |
     Immutable Response Archive                                PostgreSQL Database
   (Supabase Storage ~118 MB gzip)                         (Supabase / Frankfurt EU)
                |                                                         |
         Audit & Checksums                                    Raw & Derived Tables
                                                                          |
                                                               Security-Invoker Views
                                                                          |
                                                              Hosted / stdio MCP Server
                                                                          |
                                                            Language Models (Claude, etc.)
```

- **Daily Live Pipeline**: Automated fetch, incremental load, derived rebuild, and settlement re-checks running on GitHub Actions (`.github/workflows/e2026-live.yml`).
- **View-Driven Query Layer**: MCP queries execute against seven optimized security-invoker views. Server execution runs in under 90 ms for lineup on/off leaderboards and under 1 ms for clutch possession filters.
- **Zero Hallucination Invariants**: Games exhibiting unresolvable timing anomalies are quarantined in `game_quality` and disclosed on every query.

---

## 7. Development & Testing

Python &gt;= 3.14 is required.

```bash
# Set up virtual environment
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt   # Linux/macOS: .venv/bin/pip
.venv/Scripts/pip install -e .

# Run offline unit and integration tests (1,190+ tests, no network required)
.venv/Scripts/pytest

# Run linter and formatter
.venv/Scripts/ruff check .
.venv/Scripts/ruff format --check .
```

---

## 8. Dual-Path Evaluation Suite

[`evaluation.xml`](evaluation.xml) contains 10 complex, realistic questions designed to test LLM retrieval and reasoning over basketball data.

`tests/test_phase_8_evaluations.py` re-earns every published answer along two independent paths on demand:
1. Ground-truth SQL queries executed directly against warehouse tables.
2. The exact sequence of `el_*` MCP tool calls an LLM would execute.

Both paths must agree with the published `<expected_answer>`.

---

## 9. Links & Documentation

- **Landing Website**: [euroleague.egemenyucelen.me](https://euroleague.egemenyucelen.me)
- **Privacy Policy**: [euroleague.egemenyucelen.me/privacy.html](https://euroleague.egemenyucelen.me/privacy.html)
- **Support & FAQ**: [euroleague.egemenyucelen.me/support.html](https://euroleague.egemenyucelen.me/support.html)
- **Sponsorship One-Pager**: [`docs/SPONSOR_ONE_PAGER.md`](docs/SPONSOR_ONE_PAGER.md)
- **Scope of version 1**: [`docs/SCOPE.md`](docs/SCOPE.md) — what the seventeen tools do, what is left out on purpose, and why
- **Decision Log**: [`DECISIONS.md`](DECISIONS.md)
- **Phase Reports**: [`docs/`](docs/)

---

## 10. License

Open source under the [MIT License](LICENSE).
