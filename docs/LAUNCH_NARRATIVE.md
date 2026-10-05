# Launch narrative

The shared story for the public website, announcement thread and launch videos.
It is a content source of truth, not finished channel copy and not deployment
approval.

The website, final copy, current schedule and verified claims live in this
repository. Video source, renders, micro-clips and social cards live in the
separate
[`euroleague-analytics-launch`](https://github.com/egemeny13/euroleague-analytics-launch)
repository. Its creative brief cannot override this repository's product
evidence or Decision 45's launch date; see Decision 47.

## Audience, in order

1. Professional basketball analysts and serious journalists. The work must be
   credible enough to support a job application to a club.
2. Basketball followers who are curious about a real question, not about a
   database architecture diagram.
3. AI and data builders who understand why an MCP interface and reproducible
   evidence matter.
4. A possible sponsor for the full-history deployment.

The project is not optimised for broad engagement at the expense of method.

## One-line promise

> Ask a real EuroLeague question. Get an answer built possession by possession.

Supporting sentence:

> EuroLeague Analytics turns public play-by-play into verified possessions,
> five-player lineups, on/off splits and shot context, then makes that derived
> layer available to AI assistants through 18 read-only MCP tools.

## The launch story

The launch uses one question, one answer and three proof points. It does not try
to explain the whole warehouse.

### Question

> How did Paris perform with TJ Shorts on the floor versus off it in E2024?

This is a real dual-path evaluation in `evaluation.xml`, not a made-for-launch
example. The current tool is `el_get_player_on_off`.

### Answer

For the non-quarantined E2024 population recorded in evaluation 2:

- TJ Shorts on court: Paris net rating **+5.09** per 100 possessions.
- TJ Shorts off court: Paris net rating **-11.45** per 100 possessions.

The compact answer must not turn this into an individual-value or causal claim.
The off split includes games he did not play; 22 games are excluded by default;
and the lineup split uses the disclosed substitution-straddle convention, whose
all-E2024 measured rate is 6.10%. Full copy links or expands to those caveats.

### Proof points

Use no more than three at once:

1. **732 loaded games and 107,311 reconstructed possessions** across E2024 and
   E2025.
2. **0 point discrepancies across all 732 loaded games** against official box
   scores.
3. **10/10 dual-path evaluations passed**, re-earning published answers through
   both ground-truth SQL and the live MCP tool sequence.

The historical archive is not a launch proof point until its running backfill
finishes and the final stored-byte total is published. Until then, say only that
the archive backfill is active and each completed season passes a byte-for-byte
restore gate.

## Channel roles

### Website

Explain the promise in one screen, demonstrate the TJ Shorts question and
answer, then show the proof and the path to connect. Technical architecture and
sponsorship belong below the product demonstration.

### Announcement thread

Lead with the basketball question, not “I built a data warehouse.” Move from
the answer to how it was derived, then to the validation evidence, availability
and repository. The data is evidence; the question is the hook.

### Video

Show the question being asked, the legitimate MCP activity, the answer becoming
the visual focus, and two or three proof points. Use original UI, typography,
charts and screen recordings only. No match clips or broadcast footage.

## Voice

- Specific, calm and technically serious.
- Basketball-native without sports-broadcast clichés.
- Transparent about exclusions and uncertainty.
- Open source and human-made, without generic AI-product language.

Avoid “objective”, “revolutionary”, “the future of basketball”, “AI-powered
insights”, and any claim that the model itself discovered or verified the data.
The warehouse and its gates provide the evidence; the assistant is the access
layer.

## Final refresh gate

Before website deployment, thread publication or final video render:

1. re-run the launch-package tests;
2. confirm every displayed MCP tool name against `TOOL_NAMES`;
3. re-earn every displayed number from its cited repository evidence;
4. update the archive wording only if the chain has finished and every season
   has passed its restore gate;
5. verify the final rendered media in the launch repository, not a superseded
   local worktree copy;
6. keep publication and deployment as explicit owner-approved actions.
