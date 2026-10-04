# Unified Player Points MODEL 0.6 — EuroLeague + ACB

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

**ROTATION STATE → ROLE → AVAILABILITY / INJURY IMPACT → CONDITIONAL ROLE SAMPLE → MINUTE REDISTRIBUTION → USAGE / ATT REDISTRIBUTION → STATUS CONFIDENCE → ROLE CONFIDENCE → MINUTES → ATT RANGE → 2PA / 3PA / FTA → VARIANCE MODULE → EFFICIENCY → MATCHUP / PACE → LOW / BASE / HIGH SCENARIO → PTS DISTRIBUTION → EDGE → A BET / WATCH / NO BET**

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

## 2. AVAILABILITY / ROTATION IMPACT

Before projecting minutes or attempts, evaluate every pre-game item that can materially change a player's expected **minutes or shot opportunities**.

Relevant triggers include:
- teammate OUT / suspended / rested,
- teammate returning from injury,
- starter/bench change,
- shortened or expanded rotation,
- coach statement about role,
- minutes restriction,
- change in primary ball-handler or creator,
- frontcourt/backcourt depth loss,
- lineup change that shifts usage.

The model must translate news into basketball impact, not merely label it as uncertainty.

For every relevant trigger estimate:

- **ΔMIN**: expected change in minutes,
- **ΔFGA**: expected change in total field-goal attempts,
- **Δ3PA**: expected change in three-point attempts,
- **ΔFTA**: expected change in free-throw attempts,
- **ΔUSAGE / creation responsibility** where observable.

### Positive explained variance

If an absence or rotation change creates a clear path to more minutes or attempts for a player, that is a **potential value signal**, not a penalty.

Example:
- primary guard OUT,
- secondary guard historically gains +5 minutes and +3 FGA in comparable games,
- bookmaker line has not fully moved.

In that case, shift the player's BASE/HIGH scenario upward and allow the explained variance to strengthen the BET case.

### Negative explained variance

If a teammate returns or the rotation expands and the player is likely to lose minutes or attempts, shift the projection downward.

Example:
- high-usage scorer returns,
- player's minutes fall from 27 to 21 in comparable lineups,
- 3PA fall from 6 to 3.

This can create UNDER value.

### Evidence hierarchy

Use, in order:
1. confirmed availability / official team information,
2. coach or reliable beat-reporting statement,
3. recent games with the same absence/return pattern,
4. lineup/on-off and role-comparable historical samples,
5. generic positional assumptions only as a last resort.

If the expected redistribution cannot be supported, widen the distribution and lower confidence instead of inventing a role shift.



## MINUTE / USAGE REDISTRIBUTION MAP

When a player is OUT, returning, limited, suspended, rested or changes role, do **not** distribute his minutes and attempts evenly across teammates.

The model must identify the likely first, second and third beneficiaries or losers.

For every meaningful availability event, estimate:

- **primary minute beneficiary**
- **secondary minute beneficiary**
- **primary usage / FGA beneficiary**
- **secondary usage / FGA beneficiary**
- expected change in **MIN**
- expected change in **FGA**
- expected change in **3PA**
- expected change in **FTA**
- whether the change is direct or indirect

Use evidence in this order:

1. recent games with the same player absent/present,
2. same-lineup or same-role historical games,
3. coach statements / expected rotation,
4. substitution patterns and lineup combinations,
5. positional depth-chart logic only as a last resort.

A frontcourt absence does **not** mean every big receives the same boost. The model must determine who actually inherits the rotation.

### Redistribution map example

If Player A is OUT:

- Player B: +6 MIN, +3 FGA, primary beneficiary
- Player C: +2 MIN, +1 FGA, secondary beneficiary
- Player D: no meaningful change

Only B should receive the full role-adjusted projection boost.

If evidence is weak or beneficiaries are ambiguous, widen the distribution instead of forcing a deterministic redistribution.

## STATUS CONFIDENCE

Every availability or role-change input must receive a confidence label.

### HIGH
- officially OUT / IN,
- coach-confirmed role,
- repeated historical pattern in comparable games,
- clear first substitute / rotation replacement.

### MEDIUM
- reliable reporting but no direct coach confirmation,
- likely active but role/minutes uncertain,
- limited comparable history.

### LOW
- game-time decision,
- unclear minutes restriction,
- first game back,
- new signing / debut,
- unclear rotation,
- conflicting reports.

STATUS CONFIDENCE directly affects the projection:

- HIGH: apply the modeled MIN/FGA/3PA/FTA shift normally.
- MEDIUM: apply a smaller shift and widen the distribution.
- LOW: avoid aggressive role redistribution; widen LOW/HIGH scenarios and require a larger edge for BET.

A player returning from injury is not treated the same as a fully established active player unless minutes and role are confirmed.

## PROCESS AUDIT FOR AVAILABILITY MISSES

After the game, classify every availability-related error separately:

- **correct beneficiary, wrong magnitude**
- **wrong beneficiary**
- **status confidence too high**
- **minutes restriction missed**
- **usage redistribution missed**
- **random realization only**

This prevents the model from changing the wrong layer after one result.




## ROTATION STATE / CONTEXT-CONDITIONED ROLE ENGINE

Before using season, L10, L5 or L3 averages, build the **expected rotation state for today's game**.

The rotation state must record:

- confirmed OUT players,
- confirmed IN players,
- players returning from injury,
- known or suspected minutes restrictions,
- projected starters,
- primary and secondary ball-handlers,
- available frontcourt / backcourt depth,
- recent coach rotation pattern,
- any player whose role materially changed in the last few games.

This rotation state is the context in which all player projections are interpreted.

### ROTATION FINGERPRINT

For each candidate, search historical games by similarity to today's rotation state.

Priority:

1. **Exact-state sample** — same important absences / returns and same role structure.
2. **Primary-role sample** — same key creator, scorer or positional absence.
3. **Comparable-role sample** — player had a similar minute and usage role even if the exact teammates differed.
4. Generic season / L10 / L5 / L3 only after the conditional samples above.

A generic average must never override a strong same-state sample.

If only 1–2 comparable games exist, treat them as evidence, not certainty.

## CONDITIONAL ROLE SAMPLE

Do not assume that an absent player's production transfers to the most obvious teammate.

For every availability change, answer separately:

1. **Who inherits the minutes?**
2. **Who inherits the ball-handling / creation?**
3. **Who inherits the field-goal attempts?**
4. **Who inherits the three-point attempts?**
5. **Who inherits the free-throw pressure / rim attacks?**

These may be different players.

Example:

- Player A OUT
- Player B gains +7 minutes
- Player C gains +4 FGA
- Player D becomes secondary creator

The model must not give B all of A's usage simply because B gained the most minutes.

## MINUTE REDISTRIBUTION

Minute redistribution must respect the team rotation as a constrained system.

Rules:

- team regulation minutes sum to 200,
- added minutes for one player imply reduced minutes somewhere else,
- returning players reclaim minutes from current rotation members,
- first-game-back players use a restricted/full-role scenario unless the coach confirms otherwise,
- projected starters and first substitutes are weighted above generic positional substitutes.

For each relevant player estimate:

- base MIN,
- conditional MIN with today's rotation,
- LOW / BASE / HIGH MIN,
- source of the minute change.

Do not apply a minute boost unless there is evidence of who actually replaces the absent player.

## USAGE / ATT REDISTRIBUTION

Minutes and shot volume are modeled independently.

For each candidate estimate today's conditional:

- FGA per minute,
- 3PA per minute,
- FTA per minute,
- usage / creation responsibility where available.

A player can gain minutes without gaining shots.
A player can keep the same minutes and gain major usage.
A player can lose usage even if minutes remain stable when a high-usage teammate returns.

Use same-state and same-role games to estimate these changes.

## BENEFICIARY PROOF RULE

An injury / absence / return may strengthen a BET only when at least one of the following is true:

- repeated comparable games show the same beneficiary pattern,
- substitution / lineup history clearly identifies the replacement,
- coach information explicitly defines the new role,
- the recent role has already changed and the player is demonstrably receiving the added MIN / FGA / 3PA / FTA.

If none is true:

- do not manufacture a projection boost,
- widen the distribution,
- lower role confidence,
- classify the signal as WATCH or NO BET unless the normal-role edge remains strong independently.

## RETURNER SUPPRESSION RULE

When an important player returns:

- identify whose minutes he is likely to reclaim,
- identify whose usage / attempts he is likely to reduce,
- do not simply mark the whole team as more uncertain.

If the returning player's expected role is unclear, run at least two scenarios:

- restricted return,
- normal return.

The final probability is a mixture of those scenarios, not a single deterministic projection.

## INFORMATION-TO-NUMBER RULE

Every material pre-game information item used by the model must end in a numeric or categorical impact.

Required output:

- information item,
- affected player(s),
- ΔMIN,
- ΔFGA,
- Δ3PA,
- ΔFTA,
- role direction: UP / DOWN / NEUTRAL,
- confidence: HIGH / MEDIUM / LOW,
- evidence source type: exact-state / comparable-role / coach / lineup / generic assumption.

If a news item cannot be translated into one of these effects, it may be noted but must not change the projection.

## A BET / WATCH / NO BET

### A BET
Use for real selection only when:

- edge is sufficient under the existing price rules,
- no unresolved critical rotation ambiguity exists,
- MIN and ATT paths are supported,
- availability effects are either stable or supported by conditional evidence,
- variance is understood well enough for the offered price.

### WATCH
A statistically attractive situation with one unresolved structural question, for example:

- unclear beneficiary of an absence,
- first game after a return,
- uncertain minutes restriction,
- conflicting rotation evidence,
- very small conditional sample.

WATCH is tracked for research but is **not counted as a betting selection**.

### NO BET
No sufficient edge or too much unresolved uncertainty.

This prevents a large mathematical edge from overriding a weak role assumption.

## PRE-GAME LOCK / NO-LEAKAGE RULE

For historical simulation, every injury, status, lineup and coach-information input must have been publicly available before the betting decision time.

Do not use:

- final starting lineup if it was announced after the simulated decision point,
- post-game explanations,
- in-game rotation information,
- the final box score.

The exact information cutoff must be stored with the prediction.

## LESSON FROM R31 2026 TEST

The R31 simulation showed that strong mathematical edges failed when the model selected the wrong beneficiary or assumed the wrong role path.

Examples of failure types included:

- expected usage beneficiary was not the actual beneficiary,
- returners reclaimed more minutes than modeled,
- player gained minutes but not attempts,
- player retained minutes but lost usage,
- large edge was driven by a fragile role assumption.

Therefore availability information is useful only after it is translated through the rotation-state and beneficiary logic above.


## 3. ROLE CONFIDENCE

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

## 4. MINUTES

Project minutes as a distribution, not a single number.

Track:
- season,
- L10 / L5 / L3,
- current-role sample,
- starter/bench split,
- matchup rotation,
- foul-risk and blowout sensitivity where relevant.

A miss in minutes is a structural model miss and should be reviewed separately from shooting variance.

## 5. ATT RANGE

Attempts are primary.

Project:
- total FGA range,
- 2PA range,
- 3PA range,
- FTA range.

Do not reduce a player to one average such as “6.5 FGA”.  
Use a realistic conditional range, e.g. 6–10 FGA, and identify what pushes him toward each end.

## 6. VARIANCE MODULE

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

## 7. 3PT VOLATILITY

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

## 8. EFFICIENCY

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

## 9. MATCHUP / PACE

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

## 10. LOW / BASE / HIGH SCENARIOS

Every serious candidate should have three scenarios.

Example:

LOW: 5–7 pts  
BASE: 8–10 pts  
HIGH: 13–16 pts

Also record the trigger:
- LOW trigger,
- HIGH trigger.

The model should estimate which scenario today's context favors.

## 11. PTS DISTRIBUTION

The output is a distribution, not only one point estimate.

Required outputs:
- central projection,
- plausible LOW range,
- plausible HIGH range,
- probability OVER,
- probability UNDER,
- role confidence,
- variance confidence.

## 12. EDGE

Use the bookmaker's **central points line** as the evaluation line.

RAW EDGE = model central projection − bookmaker central line.

Also compare model probability with price-implied break-even probability.

A raw projection edge without probability/variance context is insufficient.

## 13. BET / NO BET

BET requires alignment of:

- Projection Edge,
- Role / ATT Confidence,
- Market Value.

If one is weak, prefer NO BET.

High unexplained variance requires a larger edge.

Large **explained** variance can create value when today's context clearly activates the favorable scenario.

The model is a **selection model**, not a “predict every player” model. From 20 offered players, 2–3 valid bets can be an excellent output.

## 14. Backtest evaluation

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

## 15. Competition rule

Shared logic:
- EuroLeague and ACB use this same model framework.

Separate calibration:
- EuroLeague data remains EuroLeague data,
- ACB data remains ACB data,
- do not mix raw samples just to increase N,
- cross-competition data is allowed only as contextual role evidence or through an explicit translation layer.

## 16. Locked lesson from Murcia–Barcelona test

The test reinforced:
- correct ATT can matter more than final shooting result,
- a loss caused by abnormal realization is not equivalent to a role/ATT miss,
- large 3PA volatility must be modeled as a distribution,
- the key opportunity is to explain **why** variance happens and identify the trigger before the game.

This VARIANCE MODULE applies to both EuroLeague and ACB.
