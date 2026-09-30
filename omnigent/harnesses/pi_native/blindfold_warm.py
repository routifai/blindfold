"""Warm ``pi --mode rpc`` lifecycle for blindfolded pi-native turns.

Opt-in per session via the ``omnigent.context.lifecycle=warm_if_valid`` label
(:data:`omnigent.context_assembly.labels.LIFECYCLE_LABEL` /
``LIFECYCLE_WARM_IF_VALID``). Unset (the default), every turn still runs the
disposable one-shot ``pi --print`` process :mod:`omnigent.harnesses.pi_native.
blindfold` has always run — this module is never imported by that path.

Design: one long-lived ``pi --mode rpc`` subprocess per blindfolded session,
started with exactly today's blindfold settings (fresh
``PI_CODING_AGENT_DIR``, ``--no-context-files``, ``--append-system-prompt``,
``--model``, ``--session`` of the assembler-written history) — see
:func:`_spawn`. Each later turn sends only the new message over RPC and
collects that turn's events, mapped to Omnigent items with the same
:func:`~omnigent.context_assembly.oneshot_events.parse_pi_json_events` and
:func:`~omnigent.context_assembly.blindfold.post_oneshot_items` the one-shot
path uses (RPC and ``--print --mode json`` share the same event vocabulary —
``message_end``/``agent_end``/etc. — pi's RPC mode is that same event stream
kept open instead of exited after one turn).

Reuses :class:`omnigent.inner.pi_executor._PiRpcSession` for the subprocess
I/O plumbing (reader/writer/close) — the precedent this feature is built on
— rather than writing a new RPC client. Its convenience ``start()`` method is
not used: it hardcodes ``--no-session``, which conflicts with the blindfold
contract's own ``--session <rebuilt file>`` requirement (pi-native, like
claude-native and codex-native, carries history via a rebuilt native session
file, not by replaying it as chat turns) — so :func:`_spawn` builds argv
itself and starts the same reader/stderr tasks the class's own ``start()``
would.

Validity (checked every turn before reusing, all must hold; the first that
fails is the log's "reason"):

1. The process is alive (``returncode is None``).
2. It has not sat idle past :data:`_IDLE_TIMEOUT_SECONDS`.
3. ``system_prompt`` (system text + rendered memory digest, already combined
   by :func:`omnigent.context_assembly.assembler.render_system_text`) is
   byte-identical to what the process was started with.
4. ``model`` is unchanged.
5. The assembler's selected prior-history item ids for this turn are
   *exactly* the ids this process has already seen — what it started with
   plus every turn it has processed since, in the same order. A sliding
   ``max_messages`` window that drops an old message from the front changes
   this list, so it fails validity rather than silently under-forgetting.

Any failure kills the process and starts a fresh one with this turn's own
(current) context — never a silent fallback to "all history" or to reusing a
stale process. A crash mid-turn (RPC send failure, process death, timeout, an
error-terminated pi message) fails just that turn, exactly like the one-shot
path's non-zero exit, and discards the process so the next turn starts clean.
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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from omnigent.context_assembly.blindfold import one_shot_env, post_oneshot_items
from omnigent.context_assembly.oneshot_events import parse_pi_json_events

if TYPE_CHECKING:
    from omnigent.inner.pi_executor import _PiRpcSession

_logger = logging.getLogger(__name__)

_JsonObject = dict[str, Any]

# Matches omnigent.harnesses.pi_native.blindfold._TURN_TIMEOUT_SECONDS — a
# reused/fresh warm turn gets the same overall budget a one-shot turn would.
_TURN_TIMEOUT_SECONDS = 180.0
_REQUEST_TIMEOUT_SECONDS = 10.0
# How long a process may sit unused before the next turn refuses to reuse it
# and a background sweep closes it outright, freeing the subprocess even if
# no later turn ever arrives to notice.
_IDLE_TIMEOUT_SECONDS = 600.0
_SWEEP_INTERVAL_SECONDS = 60.0
# Read-loop poll granularity while waiting on a turn's RPC events — bounds
# how promptly an overall-timeout is noticed without busy-looping.
_READ_POLL_SECONDS = 5.0
# One session's own turn rarely produces more than a handful of items; 200 is
# generous headroom for the post-turn "what did the record actually gain"
# resync fetch (see _sync_seen_ids_after_turn) while staying a single page.
_TRAILING_SYNC_FETCH_LIMIT = 200


@dataclass
class _WarmPiState:
    """A live warm pi RPC process for one blindfolded session.

    :param rpc: The live RPC session (``omnigent.inner.pi_executor.
        _PiRpcSession``, imported lazily — see module docstring).
    :param system_prompt: The rendered system+memory text this process was
        started with (or last confirmed to still match).
    :param model: The model this process was started with.
    :param seen_item_ids: Record item ids this process has been exposed to,
        oldest first — what it started with plus every turn processed since.
    :param last_used: ``time.monotonic()`` of the last turn run on it.
    :param config_dir: Its disposable ``PI_CODING_AGENT_DIR``, removed on
        discard.
    """

    rpc: _PiRpcSession
    system_prompt: str
    model: str
    seen_item_ids: list[str]
    last_used: float
    config_dir: Path


@dataclass(frozen=True)
class WarmTurnResult:
    """One warm-or-cold turn's outcome, for the caller to fold into
    :class:`omnigent.harnesses.pi_native.blindfold.BlindfoldTurnResult`.

    :param response_text: The turn's final answer, or ``None`` on error.
    :param error: The failure description, or ``None`` on success.
    :param warm_reused: Whether an existing warm process served this turn.
    :param reason: Why a warm process was *not* reused — ``None`` when
        ``warm_reused`` is ``True`` or this is the session's first turn.
    """

    response_text: str | None
    error: str | None
    warm_reused: bool
    reason: str | None


_WARM_SESSIONS: dict[str, _WarmPiState] = {}
_SESSION_LOCKS: dict[str, asyncio.Lock] = {}
_SWEEP_TASK: asyncio.Task[None] | None = None


def _get_lock(session_id: str) -> asyncio.Lock:
    lock = _SESSION_LOCKS.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _SESSION_LOCKS[session_id] = lock
    return lock


async def _discard(session_id: str, state: _WarmPiState) -> None:
    """Remove *session_id*'s warm state and tear down its process/config dir."""
    _WARM_SESSIONS.pop(session_id, None)
    with contextlib.suppress(Exception):  # teardown must never raise into a turn's result
        await state.rpc.close()
    with contextlib.suppress(OSError):
        shutil.rmtree(state.config_dir, ignore_errors=True)


async def close_warm_session(session_id: str) -> None:
    """Tear down *session_id*'s warm pi process, if any (session-delete hook).

    Best-effort and idempotent — a session with no warm process is a no-op.
    Callers (e.g. the runner's per-session cleanup on ``DELETE
    /v1/sessions/{id}``) should call this regardless of harness; it costs
    nothing when this session never opted into ``warm_if_valid``.
    """
    state = _WARM_SESSIONS.get(session_id)
    if state is not None:
        await _discard(session_id, state)
    _SESSION_LOCKS.pop(session_id, None)


async def _sweep_idle_forever() -> None:
    while True:
        await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)
        now = time.monotonic()
        for session_id, state in list(_WARM_SESSIONS.items()):
            if now - state.last_used > _IDLE_TIMEOUT_SECONDS:
                _logger.info(
                    "blindfold pi-native warm session=%s idle-expired by background sweep",
                    session_id,
                )
                await _discard(session_id, state)


def _ensure_sweeper() -> None:
    global _SWEEP_TASK
    if _SWEEP_TASK is not None and not _SWEEP_TASK.done():
        return
    _SWEEP_TASK = asyncio.create_task(_sweep_idle_forever())


async def reset_for_tests() -> None:
    """Cancel the sweeper and close every warm process. Test teardown only."""
    global _SWEEP_TASK
    if _SWEEP_TASK is not None:
        _SWEEP_TASK.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await _SWEEP_TASK
        _SWEEP_TASK = None
    for session_id, state in list(_WARM_SESSIONS.items()):
        await _discard(session_id, state)
    _SESSION_LOCKS.clear()


def _invalid_reason(
    state: _WarmPiState | None,
    *,
    system_prompt: str,
    model: str,
    expected_prior_ids: list[str],
) -> str | None:
    """The reason *state* may not be reused this turn, or ``None`` if valid."""
    if state is None:
        return "no_warm_process"
    if state.rpc.process is None or state.rpc.process.returncode is not None:
        return "process_not_alive"
    if time.monotonic() - state.last_used > _IDLE_TIMEOUT_SECONDS:
        return "idle_expired"
    if state.system_prompt != system_prompt:
        return "system_or_memory_changed"
    if state.model != model:
        return "model_changed"
    if state.seen_item_ids != expected_prior_ids:
        return "history_window_changed"
    return None


def _prior_ids(selected_items: list[_JsonObject]) -> list[str]:
    return [
        item["id"]
        for item in selected_items
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]


async def _spawn(
    *,
    session_id: str,
    system_text: str,
    selected_items: list[_JsonObject],
    model: str,
    command: str,
    workspace: Path,
    initial_seen_ids: list[str],
) -> tuple[_WarmPiState | None, str | None]:
    """Start a fresh warm ``pi --mode rpc`` process with today's blindfold
    settings. Returns ``(state, error)``; ``state`` is ``None`` on failure.
    """
    # Local import: pi_executor.py is PiExecutor's (SDK harness) module, not
    # pi-native's own — importing it only where its RPC session class is
    # actually needed avoids pulling its full module graph (Databricks
    # gateway routing, model catalogs, ...) into pi-native's import time.
    from omnigent.inner.pi_executor import _PiRpcSession

    fresh_config_dir = Path(tempfile.mkdtemp(prefix="omnigent-blindfold-pi-warm-"))
    fresh_external_id = str(uuid.uuid4())
    args = ["--mode", "rpc", "--no-context-files"]
    if system_text:
        args += ["--append-system-prompt", system_text]
    if model:
        args += ["--model", model]

    if selected_items:
        from omnigent.harnesses.pi_native.resume import (
            pi_session_records_from_session_items,
            write_pi_session_records,
        )

        records = pi_session_records_from_session_items(
            selected_items,
            session_id=session_id,
            external_session_id=fresh_external_id,
            cwd=workspace,
            model=model,
        )
        if len(records) > 1:
            # See omnigent.harnesses.pi_native.blindfold._run_one_shot for
            # why the session file lives in a subdirectory of the config
            # dir rather than at its root.
            session_path = fresh_config_dir / "sessions" / f"{fresh_external_id}.jsonl"
            write_pi_session_records(session_path, records)
            args += ["--session", str(session_path)]

    env = one_shot_env(keep=frozenset({"OPENROUTER_API_KEY"}))
    env["PI_CODING_AGENT_DIR"] = str(fresh_config_dir)
    resolved_command = shutil.which(command) or command

    rpc = _PiRpcSession()
    try:
        rpc.process = await asyncio.create_subprocess_exec(
            resolved_command,
            *args,
            cwd=str(workspace),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        shutil.rmtree(fresh_config_dir, ignore_errors=True)
        return None, f"Could not start {resolved_command!r}: {exc}"

    # Same reader/stderr-drain tasks _PiRpcSession.start() would create —
    # only the argv-building convenience wrapper is bypassed (see module
    # docstring for why).
    rpc._read_task = asyncio.create_task(rpc._reader())
    rpc._stderr_task = asyncio.create_task(rpc._stderr_reader())

    state = _WarmPiState(
        rpc=rpc,
        system_prompt=system_text,
        model=model,
        seen_item_ids=list(initial_seen_ids),
        last_used=time.monotonic(),
        config_dir=fresh_config_dir,
    )
    return state, None


async def _read_rpc_turn(rpc: _PiRpcSession, *, timeout_s: float) -> tuple[str, str | None]:
    """Read one turn's RPC events until ``agent_end`` (or failure/timeout).

    :returns: ``(raw_jsonl_text, error)``. ``raw_jsonl_text`` is every line
        read, newline-joined — the same shape ``--print --mode json``'s
        stdout has, so it feeds :func:`parse_pi_json_events` unchanged.
        ``error`` is ``None`` on a clean ``agent_end``.
    """
    lines: list[str] = []
    pending_error: str | None = None
    deadline = time.monotonic() + timeout_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "\n".join(lines), f"pi RPC turn timed out after {timeout_s:.0f}s"
        line = await rpc.read_line(timeout=min(remaining, _READ_POLL_SECONDS))
        if line is None:
            if rpc.stdout_at_eof():
                return "\n".join(lines), pending_error or "pi RPC process ended unexpectedly"
            continue  # idle, not dead — keep waiting up to the deadline
        lines.append(line)
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if event_type == "response" and event.get("success") is False:
            return "\n".join(lines), str(event.get("error") or "pi command failed")
        if event_type == "message_end":
            message = event.get("message")
            if isinstance(message, dict) and message.get("stopReason") in ("aborted", "error"):
                pending_error = str(message.get("errorMessage") or message.get("stopReason"))
        if event_type == "agent_end":
            return "\n".join(lines), pending_error


async def _fetch_last_item_id(client: httpx.AsyncClient, session_id: str) -> str | None:
    """The record's current last item id — this turn's just-recorded user
    message, fetched before this turn adds anything else (see
    :func:`_sync_seen_ids_after_turn`, which needs it as the resync anchor).
    """
    quoted = urllib.parse.quote(session_id, safe="")
    try:
        resp = await client.get(
            f"/v1/sessions/{quoted}/items",
            params={"limit": 1, "order": "desc"},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        page = resp.json()
        data = page.get("data") if isinstance(page, dict) else None
        if isinstance(data, list) and data and isinstance(data[0], dict):
            item_id = data[0].get("id")
            if isinstance(item_id, str) and item_id:
                return item_id
    except (httpx.HTTPError, ValueError):
        pass
    return None


async def _sync_seen_ids_after_turn(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    prior_ids: list[str],
    new_message_id: str,
) -> list[str] | None:
    """What this warm process has now seen: *prior_ids* plus everything the
    record gained from *new_message_id* onward (the new user message itself,
    then whatever this turn's own posts added), fetched fresh rather than
    guessed so a validity check next turn can trust it exactly.

    :returns: The updated ids, oldest first, or ``None`` if the record
        couldn't be read or no longer contains *new_message_id* within the
        fetch window — callers must treat ``None`` as "can't be trusted" and
        discard the process rather than reuse it with stale bookkeeping.
    """
    quoted = urllib.parse.quote(session_id, safe="")
    try:
        resp = await client.get(
            f"/v1/sessions/{quoted}/items",
            params={"limit": _TRAILING_SYNC_FETCH_LIMIT, "order": "desc"},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        page = resp.json()
        items_desc = page.get("data") if isinstance(page, dict) else None
        if not isinstance(items_desc, list):
            return None
        ids_desc = [
            item["id"]
            for item in items_desc
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
    except (httpx.HTTPError, ValueError):
        return None
    if new_message_id not in ids_desc:
        return None
    trailing_chrono = list(reversed(ids_desc[: ids_desc.index(new_message_id) + 1]))
    return prior_ids + trailing_chrono


def _log_turn(
    *,
    turn_id: str,
    session_id: str,
    warm_reused: bool,
    reason: str | None,
    setup_ms: float,
    duration_s: float,
    ok: bool,
    tool_calls: int,
) -> None:
    _logger.info(
        "blindfold pi-native warm turn=%s session=%s warm_reused=%s reason=%s "
        "setup_ms=%.0f process_s=%.2fs ok=%s tool_calls=%d",
        turn_id,
        session_id,
        warm_reused,
        reason,
        setup_ms,
        duration_s,
        ok,
        tool_calls,
    )


async def run_warm_or_cold(
    *,
    session_id: str,
    turn_id: str,
    new_message_text: str,
    system_text: str,
    selected_items: list[_JsonObject],
    model: str,
    command: str,
    client: httpx.AsyncClient,
    workspace: Path,
) -> WarmTurnResult:
    """Run one blindfolded pi-native turn under the ``warm_if_valid`` lifecycle.

    Reuses this session's warm ``pi --mode rpc`` process when the validity
    rule holds (module docstring), otherwise discards it (if any) and spawns
    a fresh one with this turn's own context — never a silent fallback to
    more history than the assembler selected. Turns for one session are
    serialized on a per-session lock so overlapping calls never race the same
    subprocess.

    :param selected_items: The assembler's ``prior_history_items`` for this
        turn (excludes the new message — see ``BlindfoldTurnContext``).
    :param workspace: The CLI's working directory (matches the one-shot
        path's ``Path.cwd().resolve()``).
    :returns: The turn's outcome; never raises.
    """
    _ensure_sweeper()
    async with _get_lock(session_id):
        prior_ids = _prior_ids(selected_items)
        state = _WARM_SESSIONS.get(session_id)
        reason = _invalid_reason(
            state, system_prompt=system_text, model=model, expected_prior_ids=prior_ids
        )
        warm_reused = reason is None

        setup_started = time.monotonic()
        if not warm_reused:
            if state is not None:
                await _discard(session_id, state)
            state, spawn_error = await _spawn(
                session_id=session_id,
                system_text=system_text,
                selected_items=selected_items,
                model=model,
                command=command,
                workspace=workspace,
                initial_seen_ids=prior_ids,
            )
            if state is None:
                setup_ms = (time.monotonic() - setup_started) * 1000
                _log_turn(
                    turn_id=turn_id,
                    session_id=session_id,
                    warm_reused=False,
                    reason=reason,
                    setup_ms=setup_ms,
                    duration_s=0.0,
                    ok=False,
                    tool_calls=0,
                )
                return WarmTurnResult(
                    response_text=None, error=spawn_error, warm_reused=False, reason=reason
                )
            _WARM_SESSIONS[session_id] = state
        setup_ms = (time.monotonic() - setup_started) * 1000

        # Anchor for the post-turn resync — must be read before this turn's
        # own items are posted (see _sync_seen_ids_after_turn).
        new_message_id = await _fetch_last_item_id(client, session_id)

        started = time.monotonic()
        cmd_id = f"warm_{turn_id}"
        try:
            await state.rpc.send_command(
                {
                    "type": "prompt",
                    "message": new_message_text,
                    "id": cmd_id,
                    "streamingBehavior": "followUp",
                }
            )
        except Exception as exc:  # noqa: BLE001 — RPC boundary: any failure discards this process
            await _discard(session_id, state)
            duration_s = time.monotonic() - started
            _log_turn(
                turn_id=turn_id,
                session_id=session_id,
                warm_reused=warm_reused,
                reason=reason,
                setup_ms=setup_ms,
                duration_s=duration_s,
                ok=False,
                tool_calls=0,
            )
            return WarmTurnResult(
                response_text=None,
                error=f"Failed to send prompt to warm pi RPC: {exc}",
                warm_reused=warm_reused,
                reason=reason,
            )

        raw, turn_error = await _read_rpc_turn(state.rpc, timeout_s=_TURN_TIMEOUT_SECONDS)
        duration_s = time.monotonic() - started
        items, final_text = parse_pi_json_events(raw)
        tool_call_count = sum(1 for item in items if item.item_type == "function_call")

        await post_oneshot_items(
            client,
            session_id=session_id,
            response_id=turn_id,
            items=items,
            final_text=final_text if turn_error is None else None,
        )

        if turn_error is not None:
            await _discard(session_id, state)
            _log_turn(
                turn_id=turn_id,
                session_id=session_id,
                warm_reused=warm_reused,
                reason=reason,
                setup_ms=setup_ms,
                duration_s=duration_s,
                ok=False,
                tool_calls=tool_call_count,
            )
            return WarmTurnResult(
                response_text=None,
                error=f"pi RPC turn failed: {turn_error}",
                warm_reused=warm_reused,
                reason=reason,
            )

        synced = None
        if new_message_id is not None:
            synced = await _sync_seen_ids_after_turn(
                client, session_id=session_id, prior_ids=prior_ids, new_message_id=new_message_id
            )
        if synced is None:
            # Bookkeeping can't be trusted going forward — never reuse with a
            # guessed seen-ids list; the next turn starts clean instead.
            await _discard(session_id, state)
        else:
            state.seen_item_ids = synced
            state.last_used = time.monotonic()

        _log_turn(
            turn_id=turn_id,
            session_id=session_id,
            warm_reused=warm_reused,
            reason=reason,
            setup_ms=setup_ms,
            duration_s=duration_s,
            ok=True,
            tool_calls=tool_call_count,
        )
        return WarmTurnResult(
            response_text=(final_text or "").strip(),
            error=None,
            warm_reused=warm_reused,
            reason=reason,
        )
