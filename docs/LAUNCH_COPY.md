# Launch Copy Drafts — EuroLeague Analytics

Pre-drafted social announcement threads and launch posts in English for the public release of EuroLeague Analytics.

> [!NOTE]
> The primary, character-verified 4-tweet launch thread is maintained in [`docs/LAUNCH_THREAD_FINAL.md`](./LAUNCH_THREAD_FINAL.md).

---

## 1. Principles Behind This Launch Copy

1. **Method, Not Empty "Objectivity":** We do not claim to be "unbiased" or "purely objective". We show the work, publish the code, state data exclusions, and attach the chart.
2. **Data as Evidence, Not the Hook:** The audience responds to narratives, drama, and real basketball questions. The hook is the question; the chart is the proof.
3. **Zero Copyrighted Footage:** No video clips or match broadcasts. All visual assets are original charts generated directly from our verified data warehouse.
4. **Transparent Integrity:** Honest about edge cases, quarantined games, and data limitations.

---

## 2. Main Social Launch Thread (X / Twitter)

### Tweet 1 (The Hook)
> Announcing the public release of **EuroLeague Analytics**: A validated data warehouse and Model Context Protocol (MCP) server for European basketball.
>
> Exposing exact play-by-play possessions, 5-man lineup on/off splits, and spatial shot coordinates directly to LLMs. 🧵👇

### Tweet 2 (The Problem with Traditional Box Scores)
> Why is analyzing EuroLeague teams from traditional box scores misleading?
>
> Two teams scoring 80 points might have played at 65 possessions versus 85 possessions. Box-score estimation formulas (like `FGA - ORB + TO + 0.44*FTA`) are crude approximations that distort efficiency ratings and pace adjustments.

### Tweet 3 (Beyond Thin Wrappers)
> Thin API wrappers already exist. The value of EuroLeague Analytics lives entirely in its verified derived layer:
>
> - Exact possession reconstruction counted from 5 verified ending criteria
> - Dynamic 5-man lineup tracking across all substitution batches
> - Dynamic clutch possession filtering on `margin_at_start` and `seconds_remaining_at_start`

### Tweet 4 (Verified by the Numbers)
> We don't just assert data quality; we prove it mechanically against official box scores:
>
> 📊 732 games loaded across E2024 and E2025
> ⏱️ 99.54% of player-games match official minutes to the exact second
> 🎯 100% final score reconciliation (0 point mismatches)
> 📍 41,524 field goal attempts with verified court coordinates in E2024
> 🗄️ Historical archive backfill running with a byte-for-byte restore gate on every completed season

### Tweet 5 (17 MCP Tools for AI Assistants)
> Designed from scratch for LLMs:
>
> 17 read-only `el_` tools provide focused responses with strict token-aware pagination.
>
> Claude Desktop or Cursor can analyze lineup net ratings, Four Factors, and shot distributions through simple natural language.

### Tweet 6 (Dual-Path Evaluations)
> Every question in our published `evaluation.xml` suite is re-earned on demand along two independent paths: recorded ground-truth SQL and live MCP tool calls.

### Tweet 7 (Open Source & Portfolio)
> Fully open source (MIT).
>
> 🌐 Landing & Docs: https://euroleague.egemenyucelen.me
> 💻 GitHub: https://github.com/egemeny13/euroleague-analytics
>
> Built for basketball analysts, data engineers, and AI developers. Check it out and let us know what you think! 🏀🤖

---

## 3. Professional Announcement Post (LinkedIn / Analytics Communities)

> I am excited to share **EuroLeague Analytics**—an open-source data warehouse and Model Context Protocol (MCP) server built for European basketball intelligence.
>
> Rather than relying on box-score approximations or thin API wrappers, this project builds a precision derived analytics layer from raw play-by-play event streams:
> • Exact possession counting across 5 verified possession-ending criteria
> • 5-man lineup reconstruction across all substitution batches (99.54% exact second-level player minute match)
> • Court shot attempts with normalized half-court spatial coordinates (41,524 verified in E2024)
> • 17 specialized, read-only MCP tools connecting the warehouse directly to AI assistants like Claude
> • 100% score reconciliation across 732 loaded games in E2024 and E2025
>
> The entire codebase, database migration history, and dual-path test suite are open source under the MIT License.
>
> 🔗 Website & Setup Guide: https://euroleague.egemenyucelen.me
> 🔗 GitHub Repository: https://github.com/egemeny13/euroleague-analytics
>
> #BasketballAnalytics #EuroLeague #DataEngineering #MCP #OpenSource #DataScience #Python #PostgreSQL
