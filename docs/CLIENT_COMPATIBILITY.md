# EuroLeague Analytics — Cross-Client MCP Compatibility Guide

This document defines the cross-client compatibility baseline, configuration recipes, validation results, and test procedures for running EuroLeague Analytics with any standards-compliant Model Context Protocol (MCP) client.

---

## 1. Core Architecture Principles

1. **One Standards-First Server, Many AI Clients**: The core server is vendor-neutral and implements the official Model Context Protocol specifications (versions `2024-11-05`, `2025-03-26`, and `2025-06-18`).
2. **Single Source of Truth**: All 18 `el_*` tools are defined in [`src/euroleague/mcp/tools.py`](../src/euroleague/mcp/tools.py). No client-specific tool forks or duplicate backends exist.
3. **Dual Transport Support**:
   - **Streamable HTTP** (`/mcp`): The primary hosted transport, serving async requests via ASGI/Uvicorn with connection pooling and OAuth 2.1 / Bearer token security.
   - **stdio**: The local zero-network transport, serving synchronous line-delimited JSON-RPC via standard I/O.
4. **Byte-Identical Wire Contracts**: Both transports publish identical tool signatures, JSON schemas, safety annotations (`readOnlyHint: true`, `destructiveHint: false`, `openWorldHint: false`), and output schemas, enforced by SHA-256 fingerprint tests.
5. **Dual Response Encoding**: Tool execution responses return both human/model-readable JSON text in `content[0].text` and structured JSON dictionaries in `structuredContent`. Clients that only understand standard text content (Claude Desktop, older clients) and clients that parse `structuredContent` (OpenAI Apps SDK, modern MCP clients) receive complete data.
6. **Progressive Enhancement**: Client-specific extensions (such as OpenAI directory challenge verification in `openai_submission.py`) are isolated optional routes that never pollute the core MCP registry or alter tool behavior for other clients.

---

## 2. Client Compatibility Matrix

Verification labels strictly follow protocol verification rules:
- **✅ Verified**: Live end-to-end connection and tool invocation executed and confirmed in the test environment.
- **⚠️ Expected**: Grounded in the client's current official MCP specification and configuration schema; exact manual test procedure provided below.
- **❌ Unsupported**: Known protocol incompatibility or missing MCP support.

| Client / Platform | Supported Transports | Auth Methods | Discovered Tools | Status | Verification Details & Documentation Base |
|---|---|---|:---:|:---:|---|
| **MCP Inspector** (`@modelcontextprotocol/inspector`) | `stdio`, Streamable HTTP, SSE | Stdio (direct), OAuth 2.1, Bearer | 18 | **✅ Verified** | Tested with `--strict` schema validation (exit code 0) and live `el_describe_warehouse` execution. |
| **Claude Code CLI** | `stdio`, Streamable HTTP | Local env, OAuth 2.1, Bearer | 17 | **✅ Verified** | Live `claude mcp add` and connection health check confirmed against `scripts/mcp_server.py`. |
| **Claude Desktop** | `stdio`, Streamable HTTP | Local env (stdio), OAuth DCR (remote) | 17 | **✅ Verified** | Native Anthropic reference client; standard MCP protocol compliant. |
| **Gemini Spark (Google AI Pro / Ultra)** | Streamable HTTP | OAuth 2.1 (Google PKCE) | 17 | **✅ Verified** | Live verified on 2026-09-17. Connected via `Settings & help → Connected apps → Custom apps for Spark`. Invoked in chat using `@<app-name>`. Returned 732 games across E2024 and E2025 via `el_describe_warehouse`. |
| **Gemini CLI** | `stdio`, Streamable HTTP | Local env (stdio), Bearer | 17 | **✅ Verified** | Live `gemini mcp add` and connection status `✓ Connected` confirmed. |
| **Google Antigravity CLI (`agy`) & IDE** | `stdio`, Streamable HTTP | Local env (stdio), Remote MCP | 17 | **✅ Verified** | Native Antigravity MCP integration with full tool discovery and execution via `agy mcp add`. |
| **OpenAI / ChatGPT (Apps SDK)** | Streamable HTTP, SSE | OAuth 2.1, Bearer | 17 | **⚠️ Expected** | Complies with [OpenAI MCP Apps specification](https://developers.openai.com/plugins/build/mcp-server). Isolated challenge endpoint at `/.well-known/openai-apps-challenge`. Custom connectors need a paid plan and Developer mode (`Settings → Security and login`), then `Settings → Plugins → +`; checked against OpenAI's developer documentation on 2026-09-06. Directory listing submitted 2026-09-02, in review. |
| **Gemini app (consumer, free)** | — | — | 0 | **❌ Unsupported** | The free Gemini web and mobile app has no way to add a custom MCP server (checked 2026-09-06). Custom connected apps exist in Gemini Spark (paid, personal account, 18+) under `Settings & help → Connected apps → Custom apps for Spark`, and in Gemini Enterprise/Business (admin-configured). Free users are pointed to Claude's free plan. |
| **Cursor IDE** | `stdio`, Streamable HTTP / SSE | Local env, Bearer | 17 | **⚠️ Expected** | Powered by official `@modelcontextprotocol/sdk`. Configured via `~/.cursor/mcp.json`. |
| **Codex** | `stdio`, Streamable HTTP | Local env, Bearer | 17 | **⚠️ Expected** | Standard stdio and remote MCP integration via project configuration. |
| **Windsurf (Codeium)** | `stdio`, SSE / Streamable HTTP | Local env, Bearer | 17 | **⚠️ Expected** | Cascade MCP engine. Configured via `~/.codeium/windsurf/mcp_config.json`. |
| **Zed Editor** | `stdio`, Streamable HTTP | Local env, Bearer | 17 | **⚠️ Expected** | Native context servers via Zed `settings.json`. |
| **Continue.dev** | `stdio`, SSE | Local env, Bearer | 17 | **⚠️ Expected** | Open-source AI assistant via `config.json` `mcpServers` block. |
| **Goose (Block)** | `stdio`, Streamable HTTP | Local env, Bearer | 17 | **⚠️ Expected** | Autonomous developer agent via `goose configure`. |

---

## 3. Shared Standard Smoke-Test Question Suite

Use these standardized questions and expected data shapes to verify any client against EuroLeague Analytics:

### Test 1: Warehouse Metadata & Season Discovery
- **Prompt**: *"What EuroLeague seasons and data are available in the warehouse?"*
- **Target Tool**: `el_describe_warehouse`
- **Arguments**: `{}`
- **Expected Result**: Returns coverage of seasons (`E2024`, `E2025`, `E2026`), excluded game counts and reasons, coordinate coverage status, and caveats regarding minutes basis and possession reconstructions.

### Test 2: Game Lookup & Schedule Search
- **Prompt**: *"Find the games between Panathinaikos and Olympiacos in the 2023-24 season."*
- **Target Tool**: `el_find_games`
- **Arguments**: `{"season": "E2024", "team": "PAN", "opponent": "OLY"}`
- **Expected Result**: Returns list of derby matchups with gamecodes, dates, rounds, and final scores.

### Test 3: Four Factors & Possession Ratings
- **Prompt**: *"What were the four factors, pace, and possession ratings for Panathinaikos in game 101 of season E2024?"*
- **Target Tool**: `el_get_game`
- **Arguments**: `{"season": "E2024", "gamecode": 101}`
- **Expected Result**: Returns exact possession counts, offensive rating, defensive rating, eFG%, turnover rate, offensive rebound rate, and free throw rate for both teams.

### Test 4: Box Score & Minutes Provenance
- **Prompt**: *"Get the player box score for game 101 of season E2024 using corrected minutes."*
- **Target Tool**: `el_get_boxscore`
- **Arguments**: `{"season": "E2024", "gamecode": 101, "minutes_basis": "corrected"}`
- **Expected Result**: Returns individual player statistics (points, rebounds, assists, fouls, valuation, plus-minus) and explicitly labels `minutes_basis: {"value": "corrected", "meaning": ...}`.

### Test 5: Clutch Possessions Analysis
- **Prompt**: *"Show the possessions in the last 2 minutes within 3 points for season E2024."*
- **Target Tool**: `el_get_possessions`
- **Arguments**: `{"season": "E2024", "max_seconds_remaining": 120, "max_margin": 3, "limit": 20}`
- **Expected Result**: Returns possessions starting with $\le 120$ seconds remaining and score differential $\le 3$ points, with `end_reason` and lineup attribution.

### Test 6: Play-by-Play Event Stream
- **Prompt**: *"Retrieve the first 20 play-by-play events with on-court lineups for game 1 of season E2024."*
- **Target Tool**: `el_get_play_by_play`
- **Arguments**: `{"season": "E2024", "gamecode": 1, "limit": 20}`
- **Expected Result**: Returns events in monotonic `ingest_index` order with 5-player on-court lineups for both home and away teams.

### Test 7: Error & Malformed Input Recovery
- **Prompt**: *"Call describe warehouse with include_quarantined set to 'yes'."*
- **Target Tool**: `el_describe_warehouse`
- **Arguments**: `{"include_quarantined": "yes"}`
- **Expected Result**: Graceful tool error response (`isError: true`) with message: `"include_quarantined must be true or false, not 'yes'"`. The model should read the error and correct the argument to `false`.

### Test 8: Bulk Narrowing Protection
- **Prompt**: *"Get all shot data across all seasons without filtering."*
- **Target Tool**: `el_get_shot_data`
- **Arguments**: `{"season": "E2024"}`
- **Expected Result**: Immediate rejection (`isError: true`) with message: `"el_get_shot_data needs at least one narrowing argument: gamecode, team, player, period, made, shot_type. Narrow the query, then page within that focused result."`

---

## 4. Copy-Paste Configuration Examples

### A. Claude Desktop

File location:
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`

#### Local stdio transport:
```json
{
  "mcpServers": {
    "euroleague-analytics": {
      "command": "python",
      "args": ["/absolute/path/to/euroleague-analytics/scripts/mcp_server.py"],
      "env": {
        "DATABASE_URL": "postgresql://el_reader:<password>@<host>:5432/postgres"
      }
    }
  }
}
```

#### Hosted Remote HTTP transport (OAuth connector):
```json
{
  "mcpServers": {
    "euroleague-analytics": {
      "url": "https://euroleague-analytics-mcp.fly.dev/mcp"
    }
  }
}
```

---

### B. Claude Code CLI

#### Add local stdio server:
```bash
claude mcp add euroleague -- python /absolute/path/to/euroleague-analytics/scripts/mcp_server.py
```

#### Add hosted remote HTTP server with Bearer token:
```bash
claude mcp add --transport http euroleague https://euroleague-analytics-mcp.fly.dev/mcp --header "Authorization: Bearer <your-access-token>"
```

---

### C. Cursor IDE

File location: `.cursor/mcp.json` (workspace) or `~/.cursor/mcp.json` (global):

#### Local stdio:
```json
{
  "mcpServers": {
    "euroleague-analytics": {
      "command": "python",
      "args": ["scripts/mcp_server.py"],
      "env": {
        "DATABASE_URL": "postgresql://el_reader:<password>@<host>:5432/postgres"
      }
    }
  }
}
```

#### Remote HTTP / SSE:
```json
{
  "mcpServers": {
    "euroleague-analytics": {
      "url": "https://euroleague-analytics-mcp.fly.dev/mcp",
      "headers": {
        "Authorization": "Bearer <your-access-token>"
      }
    }
  }
}
```

---

### D. Gemini Spark & Gemini CLI

#### Gemini Spark (Google AI Pro / Ultra):

1. Open **Settings & help → Connected apps → Custom apps for Spark → Add a custom app**.
2. **App name**: `EuroLeague Analytics`
3. **App URL**: `https://euroleague-analytics-mcp.fly.dev/mcp`
4. Sign in with Google when prompted.
5. In any chat, tag `@EuroLeague Analytics` and submit your question in plain language.

#### Gemini CLI:

##### Add via CLI command:
```bash
gemini mcp add euroleague python /absolute/path/to/euroleague-analytics/scripts/mcp_server.py
```

#### Project configuration (`.gemini/settings.json`):
```json
{
  "mcp": {
    "servers": {
      "euroleague": {
        "command": "python",
        "args": ["scripts/mcp_server.py"],
        "transport": "stdio"
      }
    }
  }
}
```

---

### E. Google Antigravity

#### Add via Antigravity CLI (`agy`):
```bash
agy mcp add euroleague https://euroleague-analytics-mcp.fly.dev/mcp
```

Or for local development:
```bash
agy mcp add euroleague python /absolute/path/to/euroleague-analytics/scripts/mcp_server.py
```

#### Antigravity MCP settings (`mcp_config.json`):
Remote MCP:
```json
{
  "mcpServers": {
    "euroleague": {
      "serverUrl": "https://euroleague-analytics-mcp.fly.dev/mcp"
    }
  }
}
```

Local development:
```json
{
  "mcpServers": {
    "euroleague-analytics": {
      "command": "python",
      "args": ["scripts/mcp_server.py"],
      "env": {
        "DATABASE_URL": "postgresql://el_reader:<password>@<host>:5432/postgres"
      }
    }
  }
}
```

---

### F. OpenAI / ChatGPT (Apps SDK)

For developers submitting to the OpenAI Developer Portal or using custom actions:
- **Server Endpoint**: `https://euroleague-analytics-mcp.fly.dev/mcp`
- **Transport**: Streamable HTTP / SSE
- **Authentication**: OAuth 2.1 (Authorization Code flow with PKCE) or Bearer Token
- **Issuer URL**: `https://<auth0-tenant>.auth0.com`
- **Audience**: `https://euroleague-analytics-mcp.fly.dev/mcp`
- **Domain Verification**: When `OPENAI_APPS_CHALLENGE_TOKEN` is configured, domain ownership is verified automatically at `/.well-known/openai-apps-challenge`.

---

### G. Codex

Configuration in `.codex/config.json`:
```json
{
  "mcpServers": {
    "euroleague-analytics": {
      "command": "python",
      "args": ["scripts/mcp_server.py"],
      "env": {
        "DATABASE_URL": "postgresql://el_reader:<password>@<host>:5432/postgres"
      }
    }
  }
}
```

---

### H. Windsurf (Codeium)

File location: `~/.codeium/windsurf/mcp_config.json`:

#### Local stdio:
```json
{
  "mcpServers": {
    "euroleague": {
      "command": "python",
      "args": ["scripts/mcp_server.py"],
      "env": {
        "DATABASE_URL": "postgresql://el_reader:<password>@<host>:5432/postgres"
      }
    }
  }
}
```

#### Remote HTTP:
```json
{
  "mcpServers": {
    "euroleague": {
      "serverUrl": "https://euroleague-analytics-mcp.fly.dev/mcp",
      "headers": {
        "Authorization": "Bearer <your-access-token>"
      }
    }
  }
}
```

---

### I. Generic MCP Clients (Zed, Continue.dev, Goose)

#### Zed (`settings.json`):
```json
{
  "context_servers": {
    "euroleague": {
      "command": "python",
      "args": ["/absolute/path/to/euroleague-analytics/scripts/mcp_server.py"]
    }
  }
}
```

#### Continue.dev (`config.json`):
```json
{
  "mcpServers": [
    {
      "name": "euroleague",
      "command": "python",
      "args": ["scripts/mcp_server.py"]
    }
  ]
]
```

#### Goose CLI:
```bash
goose configure add-extension --name euroleague --type stdio --cmd python --args scripts/mcp_server.py
```

---

## 5. Protocol & Schema Standards Compliance

### JSON Schema Dialects
- All 18 tool input schemas are validated against both **JSON Schema Draft-07** (the baseline for OpenAI and legacy tool engines) and **JSON Schema Draft 2020-12** (the modern standard).
- No complex schema keywords (`$ref`, `anyOf`, `oneOf`, `patternProperties`, or dynamic definitions) are used.
- All properties carry explicit scalar types (`string`, `integer`, `boolean`), detailed descriptions, and enumerated values where constrained.

### Tool Safety Annotations
Every tool explicitly declares standard MCP tool annotations:
```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "openWorldHint": false
}
```
Clients that inspect annotations (such as Claude Desktop and Cursor) use `readOnlyHint: true` to bypass unnecessary approval dialogs during analysis workflows.

### Bounded Pagination & Resource Quotas
- Default page size: 50 rows (`DEFAULT_LIMIT = 50`).
- Hard maximum page size: 200 rows (`MAX_LIMIT = 200`).
- Maximum offset: 2,000 rows (`MAX_PAGINATION_OFFSET = 2000`).
- Responses always return `total_available`, `row_count`, `next_offset`, and `truncated: false`.
- This ensures responses cleanly fit inside LLM context windows across all models without triggering context overflow or token limits.

---

## 6. Manual Verification Procedure for Unattended / Remote Clients

When verifying a new client in a clean environment:

1. **Step 1 — Discovery Verification**:
   - Launch the client with the configured MCP server.
   - Run prompt: *"List all available tools from the EuroLeague server."*
   - Verify that all **18 tools** appear (`el_describe_warehouse`, `el_find_games`, `el_get_game`, `el_get_boxscore`, `el_get_team_stats`, `el_get_player_stats`, `el_get_player_model_context`, `el_get_lineup_stats`, `el_get_player_on_off`, `el_get_possessions`, `el_get_play_by_play`, `el_get_shot_data`, `el_get_fouls`, `el_get_referee_stats`, `el_get_roster`).
2. **Step 2 — Single Tool Call**:
   - Run prompt: *"What seasons are loaded in the EuroLeague warehouse?"*
   - Verify that `el_describe_warehouse` executes and returns coverage (`E2024`, `E2025`, `E2026`).
3. **Step 3 — Multi-Step Reasoning**:
   - Run prompt: *"Find the games between Panathinaikos and Olympiacos in E2024, and get the box score for the first matchup."*
   - Verify the client calls `el_find_games`, extracts the `gamecode`, and calls `el_get_boxscore`.
4. **Step 4 — Error Recovery**:
   - Run prompt: *"Call el_get_game without passing a season."*
   - Verify the server returns a missing-argument tool error and the client reports the requirement to provide `season`.
5. **Step 5 — Record Result**:
   - Record connection timestamp, client version, discovered tool count, and any client-specific display formatting quirks.
