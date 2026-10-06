"""How much room is left, reported every night rather than discovered late.

WHY THIS EXISTS. The warehouse lives on a free tier that stops at 500,000,000
bytes, and Decision 28 sets a stop rule 20 MB below that as the whole safety
margin. The database grows every night as E2026 loads. Nobody should have to
remember to check a number, and nobody should learn it was exceeded by watching
a load fail.

WHAT IT DOES NOT DO. It reports; it never refuses. A load that stops because
storage is tight loses the night's games, and a missed game is harder to repair
than a tight database. The stop rule is enforced where a write actually happens
- `compaction.py` and the backfill scripts both assert it before writing - and
this module's job is to make sure the owner sees the number coming.

THE TWO BUDGETS ARE SEPARATE AND MUST NOT BE ADDED. The PostgreSQL database and
the Supabase Storage archive have their own ceilings. Archiving a season costs
the archive about 7 MB and costs the database nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Supabase's free tier stops here. Everything below is measured against it.
DATABASE_CEILING_BYTES = 500_000_000
# Decision 28's stop rule. The 20 MB between this and the ceiling is the margin
# the compaction work of 2026-08-18 was run inside.
DATABASE_STOP_BYTES = 480_000_000
# Where the owner should start deciding rather than start reacting. Chosen so
# that roughly a month of E2026 loading still fits between here and the rule.
DATABASE_WARNING_BYTES = 450_000_000

# The archive is a different budget on a different service. It held 1.5% of this
# on 2026-08-29, so the warning exists for completeness rather than concern.
ARCHIVE_CEILING_BYTES = 1_000_000_000
ARCHIVE_STOP_BYTES = 950_000_000
ARCHIVE_WARNING_BYTES = 900_000_000

# Decision 28's measured cost of one loaded game, across every table it touches.
# It is a measurement of E2025, not a law, and E2026 may differ.
BYTES_PER_GAME = 359_504.6

# Only relations whose size grows primarily with EuroLeague warehouse games
# belong in the EL per-game load estimate. ACB staging, archive metadata, model
# registry and MCP accounting share the same public schema but are independent
# fixed/other-competition costs and must not be divided by raw_game.
EL_GAME_COST_RELATIONS = (
    "raw_game",
    "raw_boxscore_player",
    "raw_boxscore_team",
    "raw_shot",
    "player",
    "team",
    "team_season",
    "lineup",
    "lineup_stint",
    "possession",
    "game_event",
    "player_game_minutes",
    "game_quality",
    "game_source_state",
    "roster_registration",
    "person_game_link",
)

LEVEL_OK = "ok"
LEVEL_WARNING = "warning"
LEVEL_STOP = "stop"


@dataclass(frozen=True)
class BudgetReading:
    """One budget, what it holds now, and the three lines drawn across it."""

    name: str
    used_bytes: int
    warning_bytes: int
    stop_bytes: int
    ceiling_bytes: int

    @property
    def level(self) -> str:
        """Which of the three lines this reading has crossed."""
        if self.used_bytes >= self.stop_bytes:
            return LEVEL_STOP
        if self.used_bytes >= self.warning_bytes:
            return LEVEL_WARNING
        return LEVEL_OK

    @property
    def headroom_to_stop(self) -> int:
        """Bytes left before the stop rule. Negative once it is past, on purpose.

        A budget that has been exceeded should read as exceeded. Clamping this at
        zero would make an overrun look like a near miss.
        """
        return self.stop_bytes - self.used_bytes

    @property
    def percent_of_ceiling(self) -> float:
        return 100.0 * self.used_bytes / self.ceiling_bytes


def assess_database(used_bytes: int) -> BudgetReading:
    """Read the PostgreSQL database against Decision 28's thresholds."""
    return BudgetReading(
        name="database",
        used_bytes=used_bytes,
        warning_bytes=DATABASE_WARNING_BYTES,
        stop_bytes=DATABASE_STOP_BYTES,
        ceiling_bytes=DATABASE_CEILING_BYTES,
    )


def assess_archive(used_bytes: int) -> BudgetReading:
    """Read the Supabase Storage archive against its own, separate budget."""
    return BudgetReading(
        name="archive",
        used_bytes=used_bytes,
        warning_bytes=ARCHIVE_WARNING_BYTES,
        stop_bytes=ARCHIVE_STOP_BYTES,
        ceiling_bytes=ARCHIVE_CEILING_BYTES,
    )


def games_until_stop(reading: BudgetReading, bytes_per_game: float = BYTES_PER_GAME) -> int:
    """How many more games fit before the stop rule, at the measured per-game cost.

    In plain language: this is the number the owner actually needs. "144 MB left"
    means nothing on its own; "403 more games" is a season's worth and says
    whether there is a problem this year or not.

    WHAT IT ASSUMES, and it is a real assumption: that a future game costs what a
    measured E2025 game cost. It also assumes the database does not grow for any
    other reason, which is false - every derived rebuild leaves dead tuples. Treat
    the number as an optimistic bound, not a forecast.
    """
    if reading.headroom_to_stop <= 0:
        return 0
    return int(reading.headroom_to_stop // bytes_per_game)


@dataclass(frozen=True)
class PerGameCost:
    """What one loaded game costs tonight, measured rather than assumed.

    Decision 21 measured 347,667.6 bytes per game on 2026-08-19 and Decision 28
    re-measured 359,504.6 after compaction. Both were one-off readings, and
    both went stale the day migrations 0021 to 0023 removed 100 MB. This
    class is the nightly reading that replaces the ritual: every public
    relation's bytes, divided by the games the warehouse holds.
    """

    public_bytes: int
    games_loaded: int

    @property
    def bytes_per_game(self) -> float:
        return self.public_bytes / self.games_loaded

    @property
    def ratio_to_assumed(self) -> float:
        """Measured over Decision 28's figure. 1.0 means the assumption still holds."""
        return self.bytes_per_game / BYTES_PER_GAME


def read_per_game_cost(connection: Any) -> PerGameCost | None:
    """Measure EuroLeague warehouse bytes per loaded EL game.

    ACB staging now shares the public schema and can be much larger than the
    EuroLeague hot warehouse. Dividing every public-table byte by `raw_game`
    therefore makes one EL game appear to cost megabytes and falsely blocks
    historical loads. Only relations whose size is driven by EL games belong in
    this numerator; the whole-database stop rule is still checked separately.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "select coalesce(sum(pg_total_relation_size("
            "format('%I.%I', schemaname, tablename)::regclass)), 0) "
            "from pg_tables "
            "where schemaname = 'public' and tablename = any(%s)",
            (list(EL_GAME_COST_RELATIONS),),
        )
        public_bytes = int(cursor.fetchone()[0])
        cursor.execute("select count(*) from raw_game")
        games_loaded = int(cursor.fetchone()[0])
    if games_loaded == 0:
        return None
    return PerGameCost(public_bytes=public_bytes, games_loaded=games_loaded)


def _line(reading: BudgetReading) -> str:
    mark = {LEVEL_OK: "OK", LEVEL_WARNING: "WARNING", LEVEL_STOP: "STOP RULE PASSED"}[reading.level]
    return (
        f"- **{reading.name}:** {reading.used_bytes:,} bytes "
        f"({reading.percent_of_ceiling:.1f}% of {reading.ceiling_bytes:,}) — **{mark}**"
    )


def format_storage_summary(
    database: BudgetReading,
    archive: BudgetReading,
    per_game_cost: PerGameCost | None = None,
) -> str:
    """Format both budgets for the nightly step summary.

    The games-left figure uses tonight's measured per-game cost when there is
    one, and says which it used. Decision 28's assumed figure is printed
    beside the measurement so a drift is visible without opening a decision.
    """
    if per_game_cost is not None:
        games_left = games_until_stop(database, per_game_cost.bytes_per_game)
        cost_line = (
            f"- **Measured per-game cost (Decision 21, re-measured nightly):** "
            f"{per_game_cost.bytes_per_game:,.0f} bytes over {per_game_cost.games_loaded:,} games, "
            f"{per_game_cost.ratio_to_assumed:.1%} of Decision 28's assumed {BYTES_PER_GAME:,}."
        )
        basis = "tonight's measured per-game cost"
    else:
        games_left = games_until_stop(database)
        cost_line = (
            f"- **Per-game cost:** not measured tonight; Decision 28's assumed "
            f"{BYTES_PER_GAME:,} used."
        )
        basis = "the assumed per-game cost"
    lines = [
        "### 💾 Storage budgets\n",
        _line(database),
        _line(archive),
        cost_line,
        f"- **Headroom to Decision 28's stop rule:** {database.headroom_to_stop:,} bytes, "
        f"about **{games_left}** more games at {basis}.",
    ]
    if database.level != LEVEL_OK:
        lines.append(
            "\n> The database has reached Decision 28's warning threshold. "
            "This is a decision point, not a failure: either the hot window "
            "shrinks or the plan moves to a paid tier. Nothing here blocks "
            "tonight's load."
        )
    return "\n".join(lines) + "\n"


def read_budgets(connection: Any) -> tuple[BudgetReading, BudgetReading]:
    """Measure both budgets from the live database. Reads only, and must stay that way.

    In plain language: two questions. How big is the database, and how many bytes
    do the archived response bodies add up to. Nothing here writes, and a test
    asserts that by inspecting the statements this function issues.

    An archive with no objects yet answers NULL rather than zero, which is a
    difference PostgreSQL cares about and a nightly summary should not die on.
    """
    with connection.cursor() as cursor:
        cursor.execute("select pg_database_size(current_database())")
        database_bytes = int(cursor.fetchone()[0])
        cursor.execute("select sum((metadata->>'size')::bigint) from storage.objects")
        archive_row = cursor.fetchone()[0]
        archive_bytes = int(archive_row) if archive_row is not None else 0
    return assess_database(database_bytes), assess_archive(archive_bytes)
