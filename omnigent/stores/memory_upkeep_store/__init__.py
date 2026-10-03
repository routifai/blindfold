"""Memory-upkeep-run store — persists Phase 3 upkeep run records.

See ``rollover/MEMORY-PLAN.md`` section 4 for the design: one row per
upkeep run; the per-user watermark is derived from the last ``succeeded``
run's ``window_until`` (no separate watermark table), and a ``running`` row
doubles as that user's single-run lease.
"""

from abc import ABC, abstractmethod
from typing import Any

from omnigent.entities import MemoryUpkeepRun


class MemoryUpkeepStore(ABC):
    """Abstract base for upkeep-run persistence. Every query is user-scoped."""

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the upkeep-run store.

        :param storage_location: Backend-specific storage URI, e.g.
            ``"sqlite:///omnigent.db"``.
        """
        self.storage_location = storage_location

    @abstractmethod
    def start_run(
        self,
        run_id: str,
        user_id: str,
        window_since: int,
        window_until: int,
    ) -> MemoryUpkeepRun | None:
        """Atomically start a run, acting as *user_id*'s single-run lease.

        :param run_id: Pre-generated unique run identifier.
        :param user_id: The user this run processes.
        :param window_since: Unix epoch seconds the window starts at.
        :param window_until: Unix epoch seconds the window ends at.
        :returns: The new ``running`` :class:`MemoryUpkeepRun`, or ``None``
            if *user_id* already has a run in the ``running`` state (the
            caller must skip — never starts a second concurrent run).
        """
        ...

    @abstractmethod
    def finish_run(
        self,
        run_id: str,
        *,
        state: str,
        counts: dict[str, Any],
        disposition: str | None = None,
    ) -> MemoryUpkeepRun | None:
        """Transition a ``running`` run to a terminal state.

        :param state: ``"succeeded"``, ``"failed"``, or ``"skipped"``.
        :param counts: Final counts dict for the run.
        :param disposition: Short machine-readable outcome.
        :returns: The updated :class:`MemoryUpkeepRun`, or ``None`` if
            *run_id* doesn't exist.
        """
        ...

    @abstractmethod
    def get_last_succeeded_run(self, user_id: str) -> MemoryUpkeepRun | None:
        """Return *user_id*'s most recent ``succeeded`` run, or ``None``.

        Its ``window_until`` is the watermark for the next run — a
        ``skipped`` run (gated: not enough new signal) never advances it,
        so unread messages accumulate into the next attempt.
        """
        ...

    @abstractmethod
    def has_running_run(self, user_id: str) -> bool:
        """Return whether *user_id* currently has a run in the ``running`` state."""
        ...

    @abstractmethod
    def list_runs(self, user_id: str, *, limit: int = 20) -> list[MemoryUpkeepRun]:
        """List *user_id*'s runs, newest first. For status/debug reads."""
        ...
