"""Tests for :class:`SqlAlchemyMemoryUpkeepStore`.

Exercises the run lifecycle, the per-user single-run lease, and the
watermark lookup (last *succeeded* run only — a skipped run never
advances it).
"""

from __future__ import annotations

import uuid

import pytest

from omnigent.stores.memory_upkeep_store.sqlalchemy_store import SqlAlchemyMemoryUpkeepStore


def _rid(seed: str) -> str:
    """Deterministic bare 32-char hex UUID string from a short readable seed."""
    return uuid.uuid5(uuid.NAMESPACE_DNS, seed).hex


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemyMemoryUpkeepStore:
    return SqlAlchemyMemoryUpkeepStore(db_uri)


def test_start_run_returns_a_running_row(store: SqlAlchemyMemoryUpkeepStore) -> None:
    run = store.start_run(_rid("r1"), "alice", 0, 100)
    assert run is not None
    assert run.run_id == _rid("r1")
    assert run.user_id == "alice"
    assert run.window_since == 0
    assert run.window_until == 100
    assert run.state == "running"
    assert run.started_at > 0
    assert run.finished_at is None


def test_start_run_is_the_single_run_lease(store: SqlAlchemyMemoryUpkeepStore) -> None:
    """A second concurrent start for the same user is refused."""
    first = store.start_run(_rid("r1"), "alice", 0, 100)
    assert first is not None

    second = store.start_run(_rid("r2"), "alice", 0, 200)
    assert second is None

    # A different user is unaffected by alice's lease.
    other = store.start_run(_rid("r3"), "bob", 0, 100)
    assert other is not None


def test_start_run_lease_releases_after_finish(store: SqlAlchemyMemoryUpkeepStore) -> None:
    run = store.start_run(_rid("r1"), "alice", 0, 100)
    assert run is not None
    store.finish_run(run.run_id, state="succeeded", counts={})

    second = store.start_run(_rid("r2"), "alice", 100, 200)
    assert second is not None


def test_finish_run_updates_state_counts_and_disposition(
    store: SqlAlchemyMemoryUpkeepStore,
) -> None:
    run = store.start_run(_rid("r1"), "alice", 0, 100)
    assert run is not None

    finished = store.finish_run(
        run.run_id,
        state="succeeded",
        counts={"seen": 3, "inserted": 1},
        disposition="ok",
    )
    assert finished is not None
    assert finished.state == "succeeded"
    assert finished.disposition == "ok"
    assert finished.counts == {"seen": 3, "inserted": 1}
    assert finished.finished_at is not None


def test_finish_run_returns_none_for_unknown_run(store: SqlAlchemyMemoryUpkeepStore) -> None:
    assert store.finish_run(_rid("missing"), state="succeeded", counts={}) is None


def test_has_running_run(store: SqlAlchemyMemoryUpkeepStore) -> None:
    assert store.has_running_run("alice") is False
    run = store.start_run(_rid("r1"), "alice", 0, 100)
    assert run is not None
    assert store.has_running_run("alice") is True
    store.finish_run(run.run_id, state="succeeded", counts={})
    assert store.has_running_run("alice") is False


# ── watermark: last succeeded run only ──────────────────────────────────────


def test_get_last_succeeded_run_is_none_when_no_run_ever_succeeded(
    store: SqlAlchemyMemoryUpkeepStore,
) -> None:
    assert store.get_last_succeeded_run("alice") is None

    run = store.start_run(_rid("r1"), "alice", 0, 100)
    assert run is not None
    store.finish_run(run.run_id, state="skipped", counts={}, disposition="no_new_signal")
    # A skipped (gated) run never sets the watermark.
    assert store.get_last_succeeded_run("alice") is None


def test_get_last_succeeded_run_returns_the_latest_by_window_until(
    store: SqlAlchemyMemoryUpkeepStore,
) -> None:
    first = store.start_run(_rid("r1"), "alice", 0, 100)
    assert first is not None
    store.finish_run(first.run_id, state="succeeded", counts={})

    second = store.start_run(_rid("r2"), "alice", 100, 250)
    assert second is not None
    store.finish_run(second.run_id, state="succeeded", counts={})

    watermark = store.get_last_succeeded_run("alice")
    assert watermark is not None
    assert watermark.window_until == 250


def test_watermark_is_scoped_to_user(store: SqlAlchemyMemoryUpkeepStore) -> None:
    run = store.start_run(_rid("r1"), "alice", 0, 100)
    assert run is not None
    store.finish_run(run.run_id, state="succeeded", counts={})

    assert store.get_last_succeeded_run("bob") is None


def test_list_runs_is_newest_first_and_scoped_to_user(
    store: SqlAlchemyMemoryUpkeepStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # now_epoch() has 1-second resolution — a same-second test would tie-break
    # on run id (arbitrary), not creation order. Force distinct clock ticks.
    clock = iter([100, 100, 200, 200, 300, 300, 400])
    monkeypatch.setattr(
        "omnigent.stores.memory_upkeep_store.sqlalchemy_store.now_epoch",
        lambda: next(clock),
    )
    for i in range(3):
        run = store.start_run(_rid(f"r{i}"), "alice", i * 10, (i + 1) * 10)
        assert run is not None
        store.finish_run(run.run_id, state="succeeded", counts={})
    store.start_run(_rid("bob1"), "bob", 0, 10)

    runs = store.list_runs("alice")
    assert [r.run_id for r in runs] == [_rid("r2"), _rid("r1"), _rid("r0")]
