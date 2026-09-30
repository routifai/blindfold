"""Per-session gate held while a rollover checkpoint is being written."""

from __future__ import annotations

import asyncio
import contextlib


class RolloverGate:
    """Single-flight rollover per session, and a gate new turns wait on.

    While a session's gate is open, its rollover is in progress: a second
    rollover is refused, and a turn arriving meanwhile waits (bounded) so it
    lands in the relaunched pane instead of the one being reaped.
    """

    def __init__(self, timeout_s: float = 120.0) -> None:
        self._gates: dict[str, asyncio.Event] = {}
        self._timeout_s = timeout_s

    def try_open(self, session_id: str) -> bool:
        """Open the session's gate; ``False`` if a rollover is already running."""
        if session_id in self._gates:
            return False
        self._gates[session_id] = asyncio.Event()
        return True

    def close(self, session_id: str) -> None:
        """Release waiting turns and end the session's rollover."""
        gate = self._gates.pop(session_id, None)
        if gate is not None:
            gate.set()

    def is_open(self, session_id: str) -> bool:
        return session_id in self._gates

    async def wait(self, session_id: str) -> None:
        """Wait for an open gate to close, up to the timeout."""
        gate = self._gates.get(session_id)
        if gate is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(gate.wait(), timeout=self._timeout_s)
