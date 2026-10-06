# EuroLeague Analytics — Sponsorship & Collaboration Brief

**A validated data warehouse and Model Context Protocol (MCP) server for European basketball, exposing exact play-by-play possessions, lineup on/off splits, and shot coordinates to language models and analysts.**

---

## 1. Project Mission

Most basketball analytics rely on box scores or thin API wrappers. EuroLeague Analytics builds a **precision derived layer** directly from the public play-by-play event stream:
- **Exact possession counting**: Counted independently per team across five verified ending events, preserving offensive rebound continuations and technical free throws.
- **5-man lineup tracking**: Maintaining 5 players on court across every substitution, yielding accurate on/off net ratings.
- **Spatial shot charts**: Attaching court coordinates to play-by-play events.
- **AI-native access**: 18 read-only MCP tools allowing AI models (such as Claude) to reason over structured basketball intelligence.

---

## 2. Verified Baseline & Track Record

Every metric in the warehouse is tested against official box scores and mechanical invariants before shipping:

| Metric | Verified Value | Evidence / Reference |
|---|---|---|
| **Loaded Public Games** | **732 games** (330 in E2024, 402 in E2025) | `docs/PRODUCTION_MIGRATIONS_AND_PROGRESS_REPORT.md` |
| **Reconstructed Possessions** | **107,311 possessions** (47,829 in E2024, 59,482 in E2025) | `docs/POSSESSION_RESIDUAL_REPORT.md` |
| **Court Shot Coordinates** | **41,524 verified** field goals with real coordinates | `docs/SHOT_DATA_TOOL_REPORT.md` |
| **Score Reconciliation** | **100%** (0 point mismatches across all 732 games) | `docs/PHASE_6_POSSESSIONS_REPORT.md` |
| **Minute Accuracy** | **99.54%** of player-games match official box scores to the second | `docs/PHASE_3_REPORT.md` |
| **Historical Archive** | **Backfill in progress** toward E2003–E2025; every completed season is restored byte for byte before the chain advances | `.github/workflows/historical-archive-chain.yml` |
| **Query Performance** | **88.5 ms** for full-season lineup on/off; **0.8 ms** for clutch filters | `docs/LINEUP_ON_OFF_PERFORMANCE_DECISION.md` |
| **Hosted Server Capacity** | **40 concurrent POST requests** verified at p95 3,205 ms | `docs/HOSTED_LOAD_TEST_2026-08-30.md` |

---

## 3. The Sponsorship Opportunity: Unlocking 23 Seasons

### Current Public Deployment (Free Tier)
The public server operates within Supabase's free tier, maintaining a high-performance hot window of **E2024, E2025, and live E2026** (~428 MB projected, 14.4% headroom).

### The Historical Archive
The automated archive backfill is working toward all **21 historical seasons (E2003 through E2023, 5,200+ played games)** in immutable cold storage. Every completed season passes a byte-for-byte restore gate before the chain advances. The final stored-byte total and complete-archive claim remain withheld until the workflow reports that no season remains. Loading and querying the eventual 23-season database in PostgreSQL requires a paid database instance.

```
+-------------------------------------------------------------------------------+
|                             One Open-Source Codebase                          |
+---------------------------------------+---------------------------------------+
                                        |
                 +----------------------+----------------------+
                 |                                             |
    PUBLIC DEPLOYMENT (Permanent)                 SPONSORED PRIVATE DEPLOYMENT
    - Free Supabase project                       - Dedicated Supabase Pro database
    - E2024, E2025, and live E2026                - All 20 seasons loaded & indexed
    - Open to public & community                  - Full historical query access
    - Funded by repository owner                  - Funded by sponsor (~$25-35/mo)
```

---

## 4. The Ask & Budget

- **Sponsorship Amount**: **~$25 to $35 / month** (covering the dedicated Supabase Pro compute and database subscription).
- **Zero Waste / Clean Architecture**: Built without complex paywalls or per-user billing code (`DECISIONS.md` item 32). If sponsorship ever ends, the public free deployment remains untouched and permanent.

---

## 5. What Sponsors Receive

1. **Full Historical Query Access**: Dedicated, authenticated access to query 20 seasons of European basketball data through MCP and SQL — E2007 to E2026, which is every season the public API serves. Measured 2026-09-03: E2006 and older return an empty body at every game endpoint, so they do not exist to be sold. See `DECISIONS.md` item 52.
2. **Prominent Recognition**: Sponsor acknowledgment on the project landing site (`euroleague.egemenyucelen.me`), repository README, and research reports.
3. **Collaborative Analytics**: Direct communication on custom tool development, statistical studies, and deep-dive analytics for European basketball.

---

## 6. Contact & Next Steps

To discuss collaboration or request a private test demonstration:

- **Egemen Yücelen**
- Email: [egemenyucelen@gmail.com](mailto:egemenyucelen@gmail.com)
- Website: [euroleague.egemenyucelen.me](https://euroleague.egemenyucelen.me)
- GitHub: [github.com/egemeny13/euroleague-analytics](https://github.com/egemeny13/euroleague-analytics)
