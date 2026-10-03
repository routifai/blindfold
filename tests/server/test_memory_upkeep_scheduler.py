"""Unit tests for :mod:`omnigent.server.memory_upkeep`'s scheduling classes.

``run_upkeep_once`` itself is monkeypatched to a cheap async fake — these
tests cover the scheduling behavior (dedup, concurrency, the hourly sweep's
user discovery), not the upkeep pipeline (``tests/memory/test_upkeep.py``).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.server.memory_upkeep import MemoryUpkeepCoordinator, MemoryUpkeepScheduler


class _FakePagedList:
    def __init__(self, data: list[Any]) -> None:
        self.data = data
        self.has_more = False
        self.last_id = None


class _FakeConversationStore:
    def __init__(self, owners_by_conv: dict[str, str]) -> None:
        self._owners_by_conv = owners_by_conv

    def list_conversations(self, **_kwargs: Any) -> _FakePagedList:
        return _FakePagedList([SimpleNamespace(id=cid) for cid in self._owners_by_conv])

    def get_session_owner(self, conversation_id: str, **_kwargs: Any) -> str | None:
        return self._owners_by_conv.get(conversation_id)


def _coordinator(
    *, run_calls: list[str], max_concurrency: int = 4, delay: float = 0.0
) -> MemoryUpkeepCoordinator:
    """A real :class:`MemoryUpkeepCoordinator` with its ``_run`` (normally a
    call into ``run_upkeep_once``) replaced by a cheap fake — this is an
    instance attribute, not a class method, so it receives ``user_id`` as
    its only argument, exactly as ``schedule()`` calls ``self._run``."""

    async def _fake_run(user_id: str) -> None:
        run_calls.append(user_id)
        if delay:
            await asyncio.sleep(delay)

    coordinator = MemoryUpkeepCoordinator(
        conversation_store=_FakeConversationStore({}),
        memory_service=object(),  # type: ignore[arg-type]
        upkeep_store=object(),  # type: ignore[arg-type]
        llm_caller_factory=lambda: None,
        max_concurrency=max_concurrency,
    )
    coordinator._run = _fake_run  # type: ignore[method-assign]
    return coordinator


# ── MemoryUpkeepCoordinator ─────────────────────────────────────────────────


async def test_schedule_runs_the_upkeep_pipeline_for_the_user() -> None:
    calls: list[str] = []
    coordinator = _coordinator(run_calls=calls)
    coordinator.schedule("alice")
    await coordinator.wait_for_idle()
    assert calls == ["alice"]


async def test_schedule_dedups_a_user_already_pending() -> None:
    calls: list[str] = []
    coordinator = _coordinator(run_calls=calls, delay=0.02)
    coordinator.schedule("alice")
    coordinator.schedule("alice")  # same user, still running — must not re-enqueue
    await coordinator.wait_for_idle()
    assert calls == ["alice"]


async def test_schedule_runs_different_users_independently() -> None:
    calls: list[str] = []
    coordinator = _coordinator(run_calls=calls)
    coordinator.schedule("alice")
    coordinator.schedule("bob")
    await coordinator.wait_for_idle()
    assert sorted(calls) == ["alice", "bob"]


async def test_a_user_can_be_rescheduled_after_their_run_completes() -> None:
    calls: list[str] = []
    coordinator = _coordinator(run_calls=calls)
    coordinator.schedule("alice")
    await coordinator.wait_for_idle()
    coordinator.schedule("alice")
    await coordinator.wait_for_idle()
    assert calls == ["alice", "alice"]


async def test_max_concurrency_must_be_at_least_one() -> None:
    with pytest.raises(ValueError, match="max_concurrency"):
        MemoryUpkeepCoordinator(
            conversation_store=_FakeConversationStore({}),
            memory_service=object(),  # type: ignore[arg-type]
            upkeep_store=object(),  # type: ignore[arg-type]
            llm_caller_factory=lambda: None,
            max_concurrency=0,
        )


async def test_shutdown_cancels_pending_runs() -> None:
    calls: list[str] = []
    coordinator = _coordinator(run_calls=calls, delay=10.0)
    coordinator.schedule("alice")
    await asyncio.sleep(0)  # let the task start and enter the sleep
    await coordinator.shutdown()
    assert coordinator._pending == set()


# ── MemoryUpkeepScheduler ────────────────────────────────────────────────────


async def test_sweep_once_schedules_every_distinct_recently_active_owner() -> None:
    scheduled: list[str] = []
    coordinator = SimpleNamespace(schedule=lambda user_id: scheduled.append(user_id))
    conv_store = _FakeConversationStore({"c1": "alice", "c2": "bob", "c3": "alice"})
    scheduler = MemoryUpkeepScheduler(
        conversation_store=conv_store,
        coordinator=coordinator,  # type: ignore[arg-type]
    )
    enqueued = await scheduler.sweep_once()
    assert enqueued == 2
    assert sorted(scheduled) == ["alice", "bob"]


async def test_sweep_once_skips_conversations_with_no_owner() -> None:
    scheduled: list[str] = []
    coordinator = SimpleNamespace(schedule=lambda user_id: scheduled.append(user_id))
    conv_store = _FakeConversationStore({"c1": None})
    scheduler = MemoryUpkeepScheduler(
        conversation_store=conv_store,
        coordinator=coordinator,  # type: ignore[arg-type]
    )
    enqueued = await scheduler.sweep_once()
    assert enqueued == 0
    assert scheduled == []


async def test_start_is_idempotent_and_shutdown_stops_the_loop() -> None:
    conv_store = _FakeConversationStore({})
    coordinator = SimpleNamespace(schedule=lambda user_id: None)
    scheduler = MemoryUpkeepScheduler(
        conversation_store=conv_store,
        coordinator=coordinator,  # type: ignore[arg-type]
        sweep_interval_s=1000,
    )
    await scheduler.start()
    first_task = scheduler._task
    await scheduler.start()  # idempotent — no second task
    assert scheduler._task is first_task
    await scheduler.shutdown()
    assert scheduler._task is None
