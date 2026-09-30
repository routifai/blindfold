"""Warm ("keep the CLI process alive across turns") lifecycle for
claude-native blindfold sessions.

``omnigent/harnesses/claude_native/blindfold.py`` implements the
context-assembly contract's default lifecycle: a brand-new, disposable
``claude -p`` process every turn (contract §5.3, "fresh"). This module is an
opt-in alternative for sessions labelled
``omnigent.context.lifecycle=omnigent.context.WARM_IF_VALID_LIFECYCLE``
(see ``omnigent.context_assembly.labels``): keep ONE ``claude -p
--input-format stream-json`` process alive per session, across turns, and
feed each new user message as a JSONL line on its stdin instead of spawning
a fresh process. Startup (process boot + config-dir setup) is ~470-515ms of
a ~2.2-2.4s turn (dev/blindfold measurements); a warm turn skips that and
also keeps Anthropic's prompt cache hot.

Correctness over speed: the contract still promises the CLI sees *only*
what the assembler selected for this exact turn. A warm process's own
accumulated conversation state can only ever grow — it has no way to
"forget" an item once it has seen it — so reuse is safe only when the
assembler's selection for this turn is *exactly* what the process has
already seen, with nothing dropped from the front. A small
``max_messages`` window that slides (drops the oldest kept message to make
room for the new one) means the process would remember more than the
current turn is allowed to see; that must invalidate reuse, not be worked
around. See :func:`_check_validity`.

Every failure mode here degrades to "run this turn like the fresh path
would, on a new process" — never to skipping the validity check. A
performance miss is acceptable; a context leak is not.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
import tempfile
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from omnigent.context_assembly.blindfold import one_shot_env, post_oneshot_items
from omnigent.context_assembly.labels import LIFECYCLE_LABEL, WARM_IF_VALID_LIFECYCLE
from omnigent.context_assembly.oneshot_events import parse_claude_stream_json

if TYPE_CHECKING:
    from omnigent.harnesses.claude_native.blindfold import BlindfoldTurnResult

_logger = logging.getLogger(__name__)

# Same budget as the fresh one-shot path (blindfold.py's
# _TURN_TIMEOUT_SECONDS) -- kept as its own constant rather than imported so
# this module has no import-time dependency on blindfold.py's internals.
_WARM_TURN_TIMEOUT_SECONDS = 180.0

# How long an unused warm process is kept alive before the background sweep
# discards it. Generous for a chat session's natural pace; short enough that
# an abandoned session doesn't hold a process (and its API-key-bearing env)
# open indefinitely.
_IDLE_TIMEOUT_SECONDS = 600.0
_SWEEP_INTERVAL_SECONDS = 60.0

# Items fetched (desc, then reversed) to both learn the new message's item
# id and, ground-truth, what a just-finished turn actually produced (see
# _check_validity). A single turn's own output (tool calls/results/the
# answer) is a handful of items; well within this.
_RECENT_ITEMS_FETCH_LIMIT = 50

_STDERR_TAIL_MAX_BYTES = 4000


def is_warm_lifecycle(labels: dict[str, str] | None) -> bool:
    """Whether *labels* opt this session into the warm-if-valid lifecycle.

    :param labels: A session's labels, or ``None``.
    :returns: ``True`` only when ``omnigent.context.lifecycle`` is exactly
        ``"warm_if_valid"``. Anything else (unset, ``"fresh"``, a typo)
        keeps today's behavior unchanged.
    """
    return bool(labels) and labels.get(LIFECYCLE_LABEL) == WARM_IF_VALID_LIFECYCLE


class _WarmProcessEnded(Exception):
    """The warm process's stdin/stdout closed mid-turn (it exited)."""


@dataclass
class WarmProcess:
    """One live, disposable-across-turns ``claude -p`` process for one
    blindfolded session.

    :param session_id: Omnigent conversation id this process belongs to.
    :param model: The model it was started with.
    :param system_text: The rendered system+memory text (contract §3's
        ``render_system_text`` output) it was started with.
    :param seen_item_ids: Ordered record item ids the process has been
        fed/produced so far — the validity check's "what has it already
        seen" side. Updated after every turn; see :func:`_check_validity`
        for how the tail (this turn's own output) is reconciled from the
        server's record rather than tracked locally.
    :param proc: The live subprocess.
    :param config_dir: Its disposable ``CLAUDE_CONFIG_DIR`` — kept alive
        (not deleted) for the process's whole lifetime, unlike the fresh
        path's per-turn tempdir.
    :param stderr_task: Background task draining ``proc.stderr`` into
        *stderr_tail* so a large diagnostic write can't deadlock the pipe.
    :param stderr_tail: Bounded recent stderr bytes, for error messages.
    :param lock: Serializes turns for this one process (defense in depth;
        the runner already serializes turns per session).
    :param last_used: ``time.monotonic()`` of the last turn it ran —
        idle-expiry clock.
    :param turns: How many turns it has completed, for logging.
    """

    session_id: str
    model: str
    system_text: str
    seen_item_ids: list[str]
    proc: asyncio.subprocess.Process
    config_dir: Path
    stderr_task: asyncio.Task[None]
    stderr_tail: list[bytes] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: float = field(default_factory=time.monotonic)
    turns: int = 0


async def _drain_stderr(proc: asyncio.subprocess.Process, tail: list[bytes]) -> None:
    """Continuously read *proc*'s stderr so it can never back up the pipe."""
    if proc.stderr is None:
        return
    total = 0
    try:
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                return
            tail.append(chunk)
            total += len(chunk)
            while total > _STDERR_TAIL_MAX_BYTES and tail:
                total -= len(tail.pop(0))
    except asyncio.CancelledError:
        return


async def _terminate(process: WarmProcess, *, reason: str) -> None:
    """Kill *process* and clean up everything it owns."""
    _logger.info(
        "blindfold claude-native warm session=%s discarded reason=%s turns=%d",
        process.session_id,
        reason,
        process.turns,
    )
    with contextlib.suppress(ProcessLookupError):
        if process.proc.returncode is None:
            process.proc.kill()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(process.proc.wait(), timeout=5.0)
    process.stderr_task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await process.stderr_task
    with contextlib.suppress(OSError):
        shutil.rmtree(process.config_dir, ignore_errors=True)


class WarmProcessRegistry:
    """One warm process per session, with idle expiry."""

    def __init__(self) -> None:
        self._by_session: dict[str, WarmProcess] = {}
        self._lock = asyncio.Lock()
        self._sweeper: asyncio.Task[None] | None = None

    async def get(self, session_id: str) -> WarmProcess | None:
        async with self._lock:
            return self._by_session.get(session_id)

    async def set(self, session_id: str, process: WarmProcess) -> None:
        async with self._lock:
            self._by_session[session_id] = process
        self._ensure_sweeper()

    async def discard(self, session_id: str, *, reason: str) -> None:
        async with self._lock:
            process = self._by_session.pop(session_id, None)
        if process is not None:
            await _terminate(process, reason=reason)

    def _ensure_sweeper(self) -> None:
        if self._sweeper is None or self._sweeper.done():
            self._sweeper = asyncio.create_task(self._sweep_loop())

    async def _sweep_loop(self) -> None:
        # Exits (and lets _ensure_sweeper restart it later) once nothing is
        # left to watch, rather than polling forever on an empty registry.
        while True:
            await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)
            now = time.monotonic()
            async with self._lock:
                idle_ids = [
                    sid
                    for sid, p in self._by_session.items()
                    if now - p.last_used > _IDLE_TIMEOUT_SECONDS
                ]
                idle_processes = [self._by_session.pop(sid) for sid in idle_ids]
                still_watching = bool(self._by_session)
            for process in idle_processes:
                await _terminate(process, reason="idle_expired")
            if not still_watching:
                return


_REGISTRY = WarmProcessRegistry()


async def discard_warm_session(session_id: str, *, reason: str) -> None:
    """Discard *session_id*'s warm process, if any. Safe to call unconditionally.

    Called from ``ClaudeNativeExecutor.close_session`` (session delete /
    teardown) — a no-op for every session that never had one (not
    blindfolded, not warm-lifecycle, or already discarded).

    :param session_id: Omnigent conversation id.
    :param reason: Logged discard reason, e.g. ``"session_closed"``.
    """
    await _REGISTRY.discard(session_id, reason=reason)


async def _fetch_recent_item_ids(
    client: httpx.AsyncClient, *, session_id: str, limit: int
) -> list[str]:
    """This session's most recent item ids, oldest first. ``[]`` on any failure.

    Failure here must never crash a turn -- see the module docstring's
    "degrades to fresh" rule. Returning ``[]`` makes every validity check
    downstream fail closed to "don't reuse", which only costs the perf win.
    """
    quoted_id = urllib.parse.quote(session_id, safe="")
    try:
        resp = await client.get(
            f"/v1/sessions/{quoted_id}/items",
            params={"limit": limit, "order": "desc"},
            timeout=10.0,
        )
        resp.raise_for_status()
        page = resp.json()
    except (httpx.HTTPError, ValueError):
        return []
    items_desc = page.get("data") if isinstance(page, dict) else None
    if not isinstance(items_desc, list):
        return []
    return [
        str(item["id"])
        for item in reversed(items_desc)
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]


def _check_validity(
    process: WarmProcess,
    *,
    system_text: str,
    model: str,
    prior_ids: list[str],
    recent_ids_ascending: list[str],
) -> str:
    """Contract for reuse (see module docstring): system+memory text
    identical, model identical, process alive, and the assembler's
    selection for this turn exactly equal to what the process has already
    seen -- nothing dropped from the front.

    The process's own last turn's output (its tool calls/results/answer)
    is never tracked locally: those items are persisted by the Session
    layer *after* this module's caller returns control (see
    ``post_oneshot_items``'s docstring), so this reads them back from the
    server record instead -- ground truth, and race-free because turns for
    one session are strictly serial (this only runs at the START of the
    next turn, once the previous one has fully completed).

    :returns: ``"valid"``, or the reason reuse is refused.
    """
    if process.proc.returncode is not None:
        return "process_dead"
    if process.model != model:
        return "model_changed"
    if process.system_text != system_text:
        return "system_or_memory_changed"
    if not process.seen_item_ids:
        # Cold-started with an empty window (e.g. this session's very first
        # turn) -- nothing to reconcile; the selection must itself be empty.
        expected = []
    else:
        anchor = process.seen_item_ids[-1]
        if anchor not in recent_ids_ascending:
            # Window too small to reach the anchor, or the record changed
            # in a way this module doesn't model (e.g. compaction). Refuse
            # rather than guess.
            return "history_untraceable"
        tail = recent_ids_ascending[recent_ids_ascending.index(anchor) :]
        expected = process.seen_item_ids[:-1] + tail
    if expected != prior_ids:
        return "history_mismatch"
    process.seen_item_ids = expected
    return "valid"


async def _spawn_warm_process(
    *,
    bridge_dir: Path,
    session_id: str,
    system_text: str,
    selected_items: list[dict[str, Any]],
    model: str,
    command: str,
) -> WarmProcess:
    """Start a fresh ``claude -p --input-format stream-json`` process,
    resumed from *selected_items* exactly like the fresh path's first turn,
    but left running afterward instead of thrown away.
    """
    config_dir = Path(tempfile.mkdtemp(prefix="omnigent-blindfold-warm-claude-"))
    workspace = Path.cwd().resolve()
    external_id = str(uuid.uuid4())
    args = [
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--setting-sources",
        "",
    ]
    if system_text:
        args += ["--append-system-prompt", system_text]
    if model:
        args += ["--model", model]

    seen_item_ids: list[str] = []
    if selected_items:
        from omnigent.harnesses.claude_native.main import (
            _claude_transcript_records_from_session_items,
            _sanitize_claude_project_name,
        )

        records = _claude_transcript_records_from_session_items(
            selected_items,
            session_id=session_id,
            external_session_id=external_id,
            cwd=workspace,
            bridge_dir=bridge_dir,
        )
        if records:
            project_name = _sanitize_claude_project_name(str(workspace))
            project_dir = config_dir / "projects" / project_name
            project_dir.mkdir(parents=True, exist_ok=True)
            transcript_path = project_dir / f"{external_id}.jsonl"
            with transcript_path.open("w", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record, separators=(",", ":")) + "\n")
            args += ["--resume", external_id]
        seen_item_ids = [
            str(item["id"]) for item in selected_items if isinstance(item.get("id"), str)
        ]

    env = one_shot_env(keep=frozenset({"ANTHROPIC_API_KEY"}))
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] = "1"
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    resolved_command = shutil.which(command) or command

    proc = await asyncio.create_subprocess_exec(
        resolved_command,
        *args,
        cwd=str(workspace),
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stderr_tail: list[bytes] = []
    stderr_task = asyncio.create_task(_drain_stderr(proc, stderr_tail))
    return WarmProcess(
        session_id=session_id,
        model=model,
        system_text=system_text,
        seen_item_ids=seen_item_ids,
        proc=proc,
        config_dir=config_dir,
        stderr_task=stderr_task,
        stderr_tail=stderr_tail,
    )


async def _feed_turn(
    process: WarmProcess, *, text: str, timeout_s: float
) -> tuple[list[Any], str | None]:
    """Write one user message as a stream-json line and read events back
    until this turn's ``result`` event. Leaves the process alive/attached.

    :raises _WarmProcessEnded: stdin write failed or stdout hit EOF —
        the process exited mid-turn.
    :raises TimeoutError: no ``result`` event within *timeout_s*.
    """
    if process.proc.stdin is None or process.proc.stdout is None:
        raise _WarmProcessEnded("process has no stdin/stdout pipes")
    request = {
        "type": "user",
        "message": {"role": "user", "content": text},
        "parent_tool_use_id": None,
        "session_id": "default",
    }
    payload = (json.dumps(request) + "\n").encode("utf-8")
    try:
        process.proc.stdin.write(payload)
        await process.proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError) as exc:
        raise _WarmProcessEnded(f"stdin write failed: {exc}") from exc

    lines: list[str] = []

    async def _read_until_result() -> None:
        while True:
            raw = await process.proc.stdout.readline()
            if not raw:
                raise _WarmProcessEnded("stdout closed (process exited)")
            line = raw.decode("utf-8", errors="replace")
            lines.append(line)
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("type") == "result":
                return

    await asyncio.wait_for(_read_until_result(), timeout=timeout_s)
    return parse_claude_stream_json("".join(lines))


def _log_turn(
    *,
    turn_id: str,
    session_id: str,
    setup_ms: float,
    duration_s: float,
    ok: bool,
    reused: bool,
    reason: str,
    system_text: str,
    selected_items: list[dict[str, Any]],
    tool_call_count: int,
) -> None:
    _logger.info(
        "blindfold claude-native warm turn=%s session=%s setup_ms=%.0f process_s=%.2fs ok=%s "
        "has_memory_block=%s resumed_with_items=%d tool_calls=%d warm_reused=%s reason=%s",
        turn_id,
        session_id,
        setup_ms,
        duration_s,
        ok,
        "<long_term_memory>" in system_text,
        len(selected_items),
        tool_call_count,
        reused,
        reason,
    )


async def run_warm_turn(
    *,
    bridge_dir: Path,
    session_id: str,
    turn_id: str,
    new_message_text: str,
    system_text: str,
    selected_items: list[dict[str, Any]],
    model: str,
    command: str,
    client: httpx.AsyncClient,
) -> BlindfoldTurnResult:
    """Run one blindfolded turn under the warm-if-valid lifecycle.

    Drop-in alternative to ``blindfold._run_one_shot`` for sessions labelled
    ``omnigent.context.lifecycle=warm_if_valid``: reuses this session's live
    process when :func:`_check_validity` says it's safe to, otherwise
    starts a fresh one (same as the fresh path's first turn) and keeps it
    running for next time.

    :returns: ``BlindfoldTurnResult`` with ``warm_reused``/``warm_reason``
        set, for the per-turn log line and tests.
    """
    from omnigent.harnesses.claude_native.blindfold import BlindfoldTurnResult

    prior_ids = [str(i["id"]) for i in selected_items if isinstance(i.get("id"), str)]
    recent_ids_ascending = await _fetch_recent_item_ids(
        client, session_id=session_id, limit=_RECENT_ITEMS_FETCH_LIMIT
    )
    new_item_id = recent_ids_ascending[-1] if recent_ids_ascending else None

    process = await _REGISTRY.get(session_id)
    reused = False
    reason = "no_warm_process"
    if process is not None:
        reason = _check_validity(
            process,
            system_text=system_text,
            model=model,
            prior_ids=prior_ids,
            recent_ids_ascending=recent_ids_ascending,
        )
        reused = reason == "valid"
        if not reused:
            await _REGISTRY.discard(session_id, reason=reason)
            process = None

    setup_started = time.monotonic()
    if process is None:
        try:
            process = await _spawn_warm_process(
                bridge_dir=bridge_dir,
                session_id=session_id,
                system_text=system_text,
                selected_items=selected_items,
                model=model,
                command=command,
            )
        except OSError as exc:
            return BlindfoldTurnResult(
                handled=True,
                error=f"Could not start {command!r}: {exc}",
                warm_reused=False,
                warm_reason=reason,
            )
        await _REGISTRY.set(session_id, process)
    setup_ms = (time.monotonic() - setup_started) * 1000

    started = time.monotonic()
    async with process.lock:
        try:
            items, final_text = await _feed_turn(
                process, text=new_message_text, timeout_s=_WARM_TURN_TIMEOUT_SECONDS
            )
        except (_WarmProcessEnded, TimeoutError) as exc:
            await _REGISTRY.discard(session_id, reason="process_crashed")
            duration_s = time.monotonic() - started
            _log_turn(
                turn_id=turn_id,
                session_id=session_id,
                setup_ms=setup_ms,
                duration_s=duration_s,
                ok=False,
                reused=reused,
                reason=reason,
                system_text=system_text,
                selected_items=selected_items,
                tool_call_count=0,
            )
            return BlindfoldTurnResult(
                handled=True,
                error=f"warm claude -p turn failed: {exc}",
                warm_reused=reused,
                warm_reason=reason,
            )

    tool_call_count = sum(1 for item in items if item.item_type == "function_call")
    await post_oneshot_items(
        client, session_id=session_id, response_id=turn_id, items=items, final_text=final_text
    )
    response_text = (final_text or "").strip()

    process.turns += 1
    process.last_used = time.monotonic()
    # This turn's own response items are reconciled ground-truth at the
    # START of the *next* turn (see _check_validity) -- here we only append
    # the id of the message we just fed, which anchors that reconciliation.
    process.seen_item_ids = prior_ids + ([new_item_id] if new_item_id else [])

    duration_s = time.monotonic() - started
    _log_turn(
        turn_id=turn_id,
        session_id=session_id,
        setup_ms=setup_ms,
        duration_s=duration_s,
        ok=True,
        reused=reused,
        reason=reason,
        system_text=system_text,
        selected_items=selected_items,
        tool_call_count=tool_call_count,
    )
    return BlindfoldTurnResult(
        handled=True, response_text=response_text, warm_reused=reused, warm_reason=reason
    )
