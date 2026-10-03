"""Memory upkeep run entity — persisted in the ``memory_upkeep_runs`` table.

See ``rollover/MEMORY-PLAN.md`` Phase 3 for the design: one row per upkeep
run (window -> gate -> extract -> verify -> apply -> project -> record),
watermarked per user from the last *succeeded* run's ``window_until``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MemoryUpkeepRun:
    """
    One upkeep run for one user.

    :param run_id: Unique run identifier (bare 32-char hex string).
    :param user_id: The user this run processed.
    :param window_since: Unix epoch seconds the window started at (the
        watermark going in — the last succeeded run's ``window_until``,
        or ``0`` for a user's first run).
    :param window_until: Unix epoch seconds the window ended at. Becomes
        the next run's ``window_since`` only if this run ``succeeded``.
    :param state: ``running``, ``succeeded``, ``failed``, or ``skipped``.
    :param disposition: Short machine-readable outcome, e.g. ``"ok"``,
        ``"no_new_signal"`` (gate), ``"no_llm_configured"``, or ``"error"``.
    :param counts: ``{"seen", "candidates", "inserted", "reinforced",
        "superseded", "rejected"}`` (ints) plus ``"rejected_reasons"``
        (``{reason: count}``).
    :param started_at: Unix epoch seconds the run started.
    :param finished_at: Unix epoch seconds the run finished, or ``None``
        while ``state == "running"``.
    """

    run_id: str
    user_id: str
    window_since: int
    window_until: int
    state: str = "running"
    disposition: str | None = None
    counts: dict[str, Any] = field(default_factory=dict)
    started_at: int = 0
    finished_at: int | None = None
