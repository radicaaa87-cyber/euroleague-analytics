"""The storage budget watch that reports how much room is left.

The numbers here are the ones Decision 28 argues from. A change to any of them
is a change to that decision, so these tests fail loudly rather than adapting.
"""

from __future__ import annotations

from euroleague.storage_watch import (
    ARCHIVE_CEILING_BYTES,
    BYTES_PER_GAME,
    DATABASE_CEILING_BYTES,
    DATABASE_STOP_BYTES,
    DATABASE_WARNING_BYTES,
    EL_GAME_COST_RELATIONS,
    LEVEL_OK,
    LEVEL_STOP,
    LEVEL_WARNING,
    assess_archive,
    assess_database,
    format_storage_summary,
    games_until_stop,
    read_budgets,
    read_per_game_cost,
)


def test_the_thresholds_are_the_ones_decision_28_argues_from() -> None:
    """Break caught: a threshold drifts and the decision silently means something else."""
    assert DATABASE_STOP_BYTES == 480_000_000
    assert DATABASE_CEILING_BYTES == 500_000_000
    assert DATABASE_WARNING_BYTES == 450_000_000
    assert DATABASE_WARNING_BYTES < DATABASE_STOP_BYTES < DATABASE_CEILING_BYTES


def test_a_database_well_below_the_warning_is_ok() -> None:
    """Break caught: the watch cries wolf and gets ignored when it matters."""
    reading = assess_database(335_105_171)
    assert reading.level == LEVEL_OK
    assert reading.headroom_to_stop == 480_000_000 - 335_105_171


def test_the_warning_fires_exactly_at_its_threshold_not_after() -> None:
    """Break caught: an off-by-one lets the database pass the warning silently."""
    assert assess_database(DATABASE_WARNING_BYTES - 1).level == LEVEL_OK
    assert assess_database(DATABASE_WARNING_BYTES).level == LEVEL_WARNING


def test_the_stop_level_fires_exactly_at_decision_28s_rule() -> None:
    """Break caught: the stop rule is passed without anything saying so."""
    assert assess_database(DATABASE_STOP_BYTES - 1).level == LEVEL_WARNING
    assert assess_database(DATABASE_STOP_BYTES).level == LEVEL_STOP


def test_headroom_goes_negative_past_the_stop_rule_rather_than_clamping() -> None:
    """Break caught: an overrun is reported as zero and reads like a near miss."""
    assert assess_database(DATABASE_STOP_BYTES + 5_000_000).headroom_to_stop == -5_000_000


def test_the_archive_is_measured_against_its_own_separate_budget() -> None:
    """Break caught: the archive is judged against the database's ceiling."""
    reading = assess_archive(15_409_234)
    assert reading.level == LEVEL_OK
    assert reading.ceiling_bytes == ARCHIVE_CEILING_BYTES == 1_000_000_000


def test_games_until_stop_uses_the_measured_per_game_cost() -> None:
    """Break caught: the estimate is invented rather than taken from the measurement."""
    assert BYTES_PER_GAME == 359_504.6
    reading = assess_database(335_105_171)
    assert games_until_stop(reading) == int((480_000_000 - 335_105_171) // 359_504.6)


def test_games_until_stop_is_zero_and_never_negative_past_the_rule() -> None:
    """Break caught: a breached budget reports a negative number of games left."""
    assert games_until_stop(assess_database(DATABASE_STOP_BYTES + 1)) == 0


def test_the_summary_names_the_level_the_headroom_and_the_games() -> None:
    """Break caught: the summary reports a number without saying what it means."""
    text = format_storage_summary(assess_database(335_105_171), assess_archive(15_409_234))
    assert "335,105,171" in text
    assert "144,894,829" in text
    assert "403" in text
    assert "1.5%" in text


def test_the_summary_marks_a_warning_state_visibly() -> None:
    """Break caught: a warning is formatted the same as a healthy reading."""
    text = format_storage_summary(assess_database(460_000_000), assess_archive(1_000))
    assert "WARNING" in text
    assert "Decision 28" in text


class _Cursor:
    """A cursor that answers the two size queries and records what it was asked."""

    def __init__(self, connection) -> None:
        self.connection = connection

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def execute(self, query: str, params: object = None) -> None:
        self.connection.queries.append(query)
        self._result = (
            (self.connection.database_bytes,)
            if "pg_database_size" in query
            else (self.connection.archive_bytes,)
        )

    def fetchone(self):
        return self._result


class _Connection:
    def __init__(self, database_bytes: int, archive_bytes: int) -> None:
        self.database_bytes = database_bytes
        self.archive_bytes = archive_bytes
        self.queries: list[str] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self)


def test_reading_the_budgets_measures_both_and_writes_nothing() -> None:
    """Break caught: the nightly watch mutates the database it is only meant to read."""
    connection = _Connection(335_105_171, 15_409_234)
    database, archive = read_budgets(connection)
    assert database.used_bytes == 335_105_171
    assert archive.used_bytes == 15_409_234
    joined = " ".join(connection.queries).lower()
    for forbidden in ("insert", "update", "delete", "drop", "alter", "create", "vacuum"):
        assert forbidden not in joined


def test_an_empty_archive_reads_as_zero_rather_than_failing() -> None:
    """Break caught: a fresh archive returns NULL and the whole nightly summary dies."""
    _, archive = read_budgets(_Connection(1_000, None))
    assert archive.used_bytes == 0


class _RoutingCursor:
    """A cursor that answers by query text, so one fake can serve every read."""

    def __init__(self, connection) -> None:
        self.connection = connection

    def __enter__(self) -> _RoutingCursor:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def execute(self, query: str, params: object = None) -> None:
        self.connection.queries.append(query)
        lowered = query.lower()
        if "pg_total_relation_size" in lowered:
            self._result = (self.connection.public_bytes,)
        elif "from raw_game" in lowered:
            self._result = (self.connection.games_loaded,)
        else:
            raise AssertionError(f"unexpected query: {query}")

    def fetchone(self):
        return self._result


class _RoutingConnection:
    def __init__(self, public_bytes: int, games_loaded: int) -> None:
        self.public_bytes = public_bytes
        self.games_loaded = games_loaded
        self.queries: list[str] = []

    def cursor(self) -> _RoutingCursor:
        return _RoutingCursor(self)


def test_el_per_game_cost_excludes_acb_staging_relations() -> None:
    assert "game_event" in EL_GAME_COST_RELATIONS
    assert "raw_game" in EL_GAME_COST_RELATIONS
    assert "acb_event" not in EL_GAME_COST_RELATIONS
    assert "acb_player_game" not in EL_GAME_COST_RELATIONS

    connection = _RoutingConnection(264_334_483, 732)
    read_per_game_cost(connection)
    assert "tablename = any" in " ".join(connection.queries).lower()


def test_the_per_game_cost_is_public_bytes_over_games_loaded() -> None:
    """Break caught: the nightly figure is Decision 28's assumption restated, not a measurement."""
    cost = read_per_game_cost(_RoutingConnection(264_334_483, 732))
    assert cost is not None
    assert cost.public_bytes == 264_334_483
    assert cost.games_loaded == 732
    assert cost.bytes_per_game == 264_334_483 / 732
    assert cost.ratio_to_assumed == (264_334_483 / 732) / BYTES_PER_GAME


def test_the_per_game_cost_is_absent_rather_than_infinite_with_no_games() -> None:
    """Break caught: an empty warehouse divides by zero and kills the nightly summary."""
    assert read_per_game_cost(_RoutingConnection(10_000_000, 0)) is None


def test_reading_the_per_game_cost_writes_nothing() -> None:
    connection = _RoutingConnection(264_334_483, 732)
    read_per_game_cost(connection)
    joined = " ".join(connection.queries).lower()
    for forbidden in ("insert", "update", "delete", "drop", "alter", "create", "vacuum"):
        assert forbidden not in joined


def test_the_summary_prefers_the_measured_cost_and_names_the_assumed_one() -> None:
    """Break caught: the games-left figure quietly keeps using the E2025 assumption."""
    cost = read_per_game_cost(_RoutingConnection(264_334_483, 732))
    database = assess_database(264_334_483)
    text = format_storage_summary(database, assess_archive(15_409_234), cost)
    measured = 264_334_483 / 732
    assert f"{int((480_000_000 - 264_334_483) // measured)}" in text
    assert "361,113" in text  # 264,334,483 / 732 = 361,112.68, rounded
    assert "359,504.6" in text  # Decision 28's assumption, shown beside it
    assert "732 games" in text
    assert "Decision 21" in text


def test_the_summary_falls_back_to_the_assumed_cost_without_a_measurement() -> None:
    text = format_storage_summary(assess_database(335_105_171), assess_archive(15_409_234), None)
    assert "403" in text
    assert "not measured tonight" in text
