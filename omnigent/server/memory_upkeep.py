"""Server-side scheduling for the memory upkeep job (Phase 3).

Two triggers (``rollover/MEMORY-PLAN.md`` section 9, ``SUPERSIDE-CHAT-PLAN.md``
S6): applies only to ``superside-chat`` sessions (native-CLI ``rollover``
sessions keep today's behaviour unchanged).

- :class:`MemoryUpkeepCoordinator` — fire-and-forget, at most one in-flight
  run per user, scheduled right after a ``superside-chat`` session's
  compaction item is recorded (``routes/sessions/routes_events.py``, via
  :mod:`omnigent.superchat.memory`).
- :class:`MemoryUpkeepScheduler` — an hourly sweep (mirrors
  :class:`~omnigent.server.managed_sandbox_reaper.ManagedSandboxReaper`)
  that enqueues a run for every recently-active user.

Both ultimately call :func:`omnigent.memory.upkeep.run_upkeep_once`, which
holds the real per-user lease (a ``running`` row) — the in-process dedup
sets here are just a cheap short-circuit for the common case.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress

from omnigent.memory.service import MemoryService
from omnigent.memory.upkeep import HOURLY_SWEEP_INTERVAL_S, UpkeepLLMCaller, run_upkeep_once
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.memory_upkeep_store import MemoryUpkeepStore

_logger = logging.getLogger(__name__)


class MemoryUpkeepCoordinator:
    """Schedule at most one in-flight upkeep run per user, fire-and-forget."""

    def __init__(
        self,
        *,
        conversation_store: ConversationStore,
        memory_service: MemoryService,
        upkeep_store: MemoryUpkeepStore,
        llm_caller_factory: Callable[[], UpkeepLLMCaller | None],
        max_concurrency: int = 4,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self._conversation_store = conversation_store
        self._memory_service = memory_service
        self._upkeep_store = upkeep_store
        self._llm_caller_factory = llm_caller_factory
        self._slots = asyncio.Semaphore(max_concurrency)
        self._pending: set[asyncio.Task[None]] = set()
        self._scheduled_user_ids: set[str] = set()

    def schedule(self, user_id: str) -> None:
        """Enqueue a run for *user_id* unless one is already pending here."""
        if user_id in self._scheduled_user_ids:
            return
        self._scheduled_user_ids.add(user_id)
        task = asyncio.create_task(self._run(user_id), name=f"memory-upkeep-{user_id}")
        self._pending.add(task)

        def _discard(completed: asyncio.Task[None]) -> None:
            self._pending.discard(completed)
            self._scheduled_user_ids.discard(user_id)

        task.add_done_callback(_discard)

    async def _run(self, user_id: str) -> None:
        try:
            async with self._slots:
                await run_upkeep_once(
                    user_id=user_id,
                    conversation_store=self._conversation_store,
                    memory=self._memory_service,
                    upkeep_store=self._upkeep_store,
                    llm_caller=self._llm_caller_factory(),
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - background job must never crash the caller
            _logger.warning("memory upkeep run failed user=%s", user_id, exc_info=True)

    async def wait_for_idle(self) -> None:
        """Wait for currently scheduled runs; used by focused tests."""
        if self._pending:
            await asyncio.gather(*tuple(self._pending))

    async def shutdown(self) -> None:
        """Cancel and drain pending runs during server shutdown."""
        pending = tuple(self._pending)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


class MemoryUpkeepScheduler:
    """Hourly sweep: enqueue every recently-active user's upkeep run.

    "Recently active" is deliberately cheap and approximate — a session
    touched since the last sweep, scanned across all users. A false
    positive costs one gated (and watermark-preserving) skip; a user
    missed this sweep is caught by the next one or by the compaction
    trigger.
    """

    def __init__(
        self,
        *,
        conversation_store: ConversationStore,
        coordinator: MemoryUpkeepCoordinator,
        sweep_interval_s: int = HOURLY_SWEEP_INTERVAL_S,
        scan_limit: int = 200,
    ) -> None:
        self._conversation_store = conversation_store
        self._coordinator = coordinator
        self._sweep_interval_s = sweep_interval_s
        self._scan_limit = scan_limit
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start this server process's hourly sweep loop."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="memory-upkeep-scheduler")

    async def shutdown(self) -> None:
        """Stop the sweep loop and wait for cancellation to settle."""
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def sweep_once(self) -> int:
        """Enqueue one run per recently-active user; return how many."""
        user_ids = await asyncio.to_thread(self._recently_active_user_ids)
        for user_id in user_ids:
            self._coordinator.schedule(user_id)
        return len(user_ids)

    def _recently_active_user_ids(self) -> list[str]:
        page = self._conversation_store.list_conversations(
            kind=None,
            limit=self._scan_limit,
            order="desc",
            sort_by="updated_at",
        )
        owners: set[str] = set()
        for conv in page.data:
            owner = self._conversation_store.get_session_owner(conv.id)
            if owner:
                owners.add(owner)
        return sorted(owners)

    async def _run(self) -> None:
        while True:
            try:
                enqueued = await self.sweep_once()
                if enqueued:
                    _logger.info("Memory upkeep sweep enqueued %s user(s)", enqueued)
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("Memory upkeep sweep failed; retrying later")
            await asyncio.sleep(self._sweep_interval_s)
