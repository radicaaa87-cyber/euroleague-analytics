# Unified Player Points MODEL 0.3 — EuroLeague + ACB

Status: LOCKED WORKING SPEC  
Date: 2026-10-04

## Scope

This is the shared player-points decision framework for **EuroLeague and ACB / Liga Endesa**.

The model logic is shared across competitions, but **EuroLeague and ACB samples are not blindly pooled**. Each competition keeps its own minutes, attempts, efficiency, matchup, pace, rotation and opponent baselines. Cross-competition information is contextual only unless role translation is explicitly modeled.

## Core objective

The goal is **not to predict every player correctly**.  
The goal is to identify a small number of situations where:

1. the bookmaker line is meaningfully away from our conditional projection,
2. the player's role and shot-volume path are sufficiently understood,
3. the source of variance is known or exploitable,
4. the offered price still gives positive expected value.

A NO BET player may score far over or under the line without counting as a betting-model failure.

## Model order

**ROLE → ROLE CONFIDENCE → MINUTES → ATT RANGE → 2PA / 3PA / FTA → VARIANCE MODULE → EFFICIENCY → MATCHUP / PACE → LOW / BASE / HIGH SCENARIO → PTS DISTRIBUTION → EDGE → CONFIDENCE → BET / NO BET**

## 1. ROLE

Determine the player's current role before using averages.

Role changes include:
- transfer / new team,
- starter ↔ bench,
- teammate injury or absence,
- coach / rotation change,
- different competition,
- major lineup change,
- change in ball-handling or creation responsibility.

When role changes, season averages lose weight. Prioritize games with similar:
- minutes,
- usage,
- FGA,
- 3PA,
- FTA,
- starter/bench status,
- lineup context.

## 2. ROLE CONFIDENCE

Measure how certain we are that the current role is stable.

High confidence:
- several recent games in the same role,
- stable minutes and attempts,
- stable lineup responsibility.

Low confidence:
- new team,
- only 1–3 games in the role,
- large lineup changes,
- unclear rotation.

Low role confidence does **not** automatically mean NO BET, but it requires a larger edge and wider projected distribution.

## 3. MINUTES

Project minutes as a distribution, not a single number.

Track:
- season,
- L10 / L5 / L3,
- current-role sample,
- starter/bench split,
- matchup rotation,
- foul-risk and blowout sensitivity where relevant.

A miss in minutes is a structural model miss and should be reviewed separately from shooting variance.

## 4. ATT RANGE

Attempts are primary.

Project:
- total FGA range,
- 2PA range,
- 3PA range,
- FTA range.

Do not reduce a player to one average such as “6.5 FGA”.  
Use a realistic conditional range, e.g. 6–10 FGA, and identify what pushes him toward each end.

## 5. VARIANCE MODULE

Large variance is **not automatically a weakness**. It can be an edge if we understand what drives it.

### Structural / explainable variance

Examples:
- 3PA jumps when a primary creator is absent,
- usage rises in a specific lineup,
- FTA rises against foul-prone rim protection,
- minutes jump as starter,
- pace or matchup creates extra possessions,
- opponent scheme concedes catch-and-shoot threes.

If the trigger is present today, increase the weight of the corresponding LOW or HIGH scenario.

### Random variance

Examples:
- same volume, but 1/7 versus 5/7 from three,
- unusual FT conversion,
- short-term hot/cold shooting without a structural trigger.

Random variance widens the distribution and lowers confidence. Do not overreact by changing the mean projection after one game.

## 6. 3PT VOLATILITY

Players whose scoring depends heavily on threes require special treatment.

Track both:
- **3PA share of FGA**, and
- **game-to-game 3PA volatility**.

Example:
3PA sequence = 5, 2, 7, 3, 6.

The mean alone is misleading. Model a range and identify why the volume changes.

Guideline:
- 3PA >= 50% of FGA: increase scoring variance,
- 3PA >= 65%: materially widen distribution,
- 3PA >= 75% or frequent 6–8+ 3PA games: high-volatility profile,
- additional penalty if 3PA itself swings strongly game to game.

Do not simply penalize high-volatility players.  
If the HIGH/LOW trigger is explainable, variance can become an opportunity.

## 7. EFFICIENCY

Efficiency is downstream from role and attempts.

Evaluate:
- 2P%,
- 3P%,
- FT%,
- TS%,
- FTr,
- shot quality where available.

Separate:
- **process miss**: wrong minutes / wrong attempts / wrong role,
- **realization miss**: expected volume occurred but shooting outcome was unusual.

A lost bet with correct minutes and attempts should not automatically change the model.

## 8. MATCHUP / PACE

Adjust for:
- opponent pace,
- defensive scheme,
- positional matchup,
- likely primary defender,
- rim protection,
- 3PA allowed by role/type,
- foul rate,
- lineup combinations,
- current roster and absences.

Historical H2H is secondary unless current roles and rosters are comparable.

## 9. LOW / BASE / HIGH SCENARIOS

Every serious candidate should have three scenarios.

Example:

LOW: 5–7 pts  
BASE: 8–10 pts  
HIGH: 13–16 pts

Also record the trigger:
- LOW trigger,
- HIGH trigger.

The model should estimate which scenario today's context favors.

## 10. PTS DISTRIBUTION

The output is a distribution, not only one point estimate.

Required outputs:
- central projection,
- plausible LOW range,
- plausible HIGH range,
- probability OVER,
- probability UNDER,
- role confidence,
- variance confidence.

## 11. EDGE

Use the bookmaker's **central points line** as the evaluation line.

RAW EDGE = model central projection − bookmaker central line.

Also compare model probability with price-implied break-even probability.

A raw projection edge without probability/variance context is insufficient.

## 12. BET / NO BET

BET requires alignment of:

- Projection Edge,
- Role / ATT Confidence,
- Market Value.

If one is weak, prefer NO BET.

High unexplained variance requires a larger edge.

Large **explained** variance can create value when today's context clearly activates the favorable scenario.

The model is a **selection model**, not a “predict every player” model. From 20 offered players, 2–3 valid bets can be an excellent output.

## 13. Backtest evaluation

Track two separate results:

### Betting result
WIN / LOSS / PUSH for picks actually classified BET.

### Process result
For every reviewed player:
- role correct?,
- minutes correct?,
- FGA correct?,
- 3PA correct?,
- FTA correct?,
- efficiency normal or abnormal?,
- matchup trigger identified?,
- final points.

Do not count NO BET results as betting wins/losses.

DNP is excluded from W/L.

Predictions must be locked before the result.  
Do not change rules after seeing the outcome.

## 14. Competition rule

Shared logic:
- EuroLeague and ACB use this same model framework.

Separate calibration:
- EuroLeague data remains EuroLeague data,
- ACB data remains ACB data,
- do not mix raw samples just to increase N,
- cross-competition data is allowed only as contextual role evidence or through an explicit translation layer.

## 15. Locked lesson from Murcia–Barcelona test

The test reinforced:
- correct ATT can matter more than final shooting result,
- a loss caused by abnormal realization is not equivalent to a role/ATT miss,
- large 3PA volatility must be modeled as a distribution,
- the key opportunity is to explain **why** variance happens and identify the trigger before the game.

This VARIANCE MODULE applies to both EuroLeague and ACB.
