"""Blindfold-mode per-turn execution for pi-native.

Mirrors ``omnigent.harnesses.claude_native.blindfold`` for the ``pi`` CLI —
see that module's docstring for why blindfold turns run as disposable
one-shot processes instead of reusing the resident, extension-driven pi
terminal (the same "killing the shared terminal ends the whole Omnigent
session" hazard applies here: the pi-native terminal's death is what the
runner's reconnect loop treats as end-of-session).

Each turn:

1. Reads the connection state ``write_server_connection``
   (``omnigent.context_assembly.blindfold``) persisted at bridge prepare
   time; not blindfolded (or never written for this session) exits
   immediately with ``handled=False``.
2. Calls ``fetch_blindfold_turn_context`` (contract §2/§3).
3. Rebuilds a private pi v3 session file from the assembler's selected
   history using the existing rebuilder
   (``pi_session_records_from_session_items``), fed only the selected items.
4. Runs ``pi --print <message> --session <fresh file> --no-context-files
   --append-system-prompt <system + memory>`` under a fresh, disposable
   ``PI_CODING_AGENT_DIR`` — vendor memory off, new CLI session every turn.
5. Reports the outcome via ``/v1/sessions/{id}/context/observe``, best-effort.
6. Deletes the whole disposable ``PI_CODING_AGENT_DIR``.

Fails closed (contract §7) exactly like the claude-native module: once a
session is confirmed blindfolded, every later failure stays inside the
one-shot turn rather than falling back to the resident, history-carrying
terminal.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote as _url_quote

import httpx

from omnigent.context_assembly.blindfold import (
    BlindfoldTurnContext,
    fetch_blindfold_turn_context,
    is_blindfolded,
    post_oneshot_items,
    read_server_connection,
    record_user_message,
)
from omnigent.context_assembly.oneshot_events import parse_pi_json_events

_logger = logging.getLogger(__name__)

_TURN_TIMEOUT_SECONDS = 180.0
_SESSION_FETCH_TIMEOUT_SECONDS = 5.0
_DEFAULT_MODEL = "anthropic/claude-haiku-4.5"
_HISTORY_FORMAT = "pi_v3_session"


@dataclass(frozen=True)
class BlindfoldTurnResult:
    """See :class:`omnigent.harnesses.claude_native.blindfold.BlindfoldTurnResult`."""

    handled: bool
    response_text: str | None = None
    error: str | None = None


async def maybe_run_blindfold_turn(
    *,
    bridge_dir: Path,
    session_id: str,
    new_message_text: str,
    command: str = "pi",
) -> BlindfoldTurnResult:
    """Run one turn under the blindfold contract, if this session opts in.

    See :func:`omnigent.harnesses.claude_native.blindfold.maybe_run_blindfold_turn`.
    """
    connection = read_server_connection(bridge_dir)
    if connection is None:
        return BlindfoldTurnResult(handled=False)
    if connection.blindfolded is False:
        return BlindfoldTurnResult(handled=False)

    async with httpx.AsyncClient(
        base_url=connection.base_url,
        headers=connection.headers,
        timeout=_SESSION_FETCH_TIMEOUT_SECONDS,
    ) as client:
        try:
            session_resp = await client.get(f"/v1/sessions/{_url_quote(session_id, safe='')}")
            session_resp.raise_for_status()
            session_payload = session_resp.json()
            labels = session_payload.get("labels") if isinstance(session_payload, dict) else None
        except (httpx.HTTPError, ValueError):
            labels = None
            session_payload = None

        if connection.blindfolded is None:
            if not is_blindfolded(labels if isinstance(labels, dict) else None):
                return BlindfoldTurnResult(handled=False)

        model = (
            str(session_payload.get("model") or _DEFAULT_MODEL)
            if session_payload
            else _DEFAULT_MODEL
        )
        labels_dict = labels if isinstance(labels, dict) else {}

        # Must happen before assemble(): see record_user_message's docstring —
        # native harnesses have no other path that ever persists this turn's
        # input. A failure here means the record can't be trusted to anchor
        # on this turn yet, so run with no history rather than risk assembling
        # against the wrong (previous) turn's last item.
        history_available = True
        try:
            await record_user_message(client, session_id=session_id, text=new_message_text)
        except (httpx.HTTPError, ValueError):
            _logger.warning(
                "could not record new message for blindfolded session=%s; "
                "running this turn with no history",
                session_id,
                exc_info=True,
            )
            history_available = False

        if history_available:
            ctx = await fetch_blindfold_turn_context(
                client,
                session_id=session_id,
                labels=labels_dict,
                harness_name="pi-native",
                model=model,
                context_window_tokens=200_000,
                history_format=_HISTORY_FORMAT,
                fallback_instructions=None,
            )
        else:
            ctx = BlindfoldTurnContext(system_prompt="", prior_history_items=[], fallback=True)

        turn_id = f"turn_{uuid.uuid4().hex[:16]}"
        result = await _run_one_shot(
            session_id=session_id,
            new_message_text=new_message_text,
            system_text=ctx.system_prompt,
            selected_items=ctx.prior_history_items,
            model=model,
            command=command,
            turn_id=turn_id,
            client=client,
        )

        with contextlib.suppress(httpx.HTTPError, ValueError):
            from omnigent.context_assembly.models import ObserveRequest, UsageInfo

            await client.post(
                f"/v1/sessions/{_url_quote(session_id, safe='')}/context/observe",
                json=ObserveRequest(
                    turn_id=turn_id,
                    session_id=session_id,
                    outcome="failed" if result.error is not None else "completed",
                    new_item_ids=[],
                    usage=UsageInfo(),
                ).model_dump(),
            )

        # The one-shot process is not a real native forwarder, so nothing
        # else ever posts the turn-end edge (external_session_status) the
        # runner's native-turn tracking waits on — without this, the next
        # turn for this session sits buffered behind a slot that looks
        # permanently occupied (observed: ~60s delay, sometimes longer).
        with contextlib.suppress(httpx.HTTPError, ValueError):
            from omnigent.native._native_post_delivery import post_external_session_status

            await post_external_session_status(
                client,
                session_id=session_id,
                status="failed" if result.error is not None else "idle",
                turn_completed=True,
            )
        return result


async def _run_one_shot(
    *,
    session_id: str,
    turn_id: str,
    new_message_text: str,
    system_text: str,
    selected_items: list[dict],
    model: str,
    command: str,
    client: httpx.AsyncClient,
) -> BlindfoldTurnResult:
    """Run one disposable ``pi --print`` process and report the outcome."""
    # Measured separately from the process wall time below so a latency
    # table can tell "building the fresh config dir/history file" apart
    # from "the CLI process itself" (dominated by the model call).
    setup_started = time.monotonic()
    fresh_config_dir = Path(tempfile.mkdtemp(prefix="omnigent-blindfold-pi-"))
    workspace = Path.cwd().resolve()
    fresh_external_id = str(uuid.uuid4())
    # --mode json is the only one-shot output format that carries tool calls
    # and their results, not just the final answer — see parse_pi_json_events.
    args = ["--print", "--no-context-files", "--mode", "json"]
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
            # Verified against the real CLI: a session file written directly
            # under PI_CODING_AGENT_DIR's own root gets silently clobbered by
            # a brand-new session on startup (pi appears to treat that root
            # as its own session-index scope). A subdirectory avoids the
            # collision and the pre-written history loads correctly.
            session_path = fresh_config_dir / "sessions" / f"{fresh_external_id}.jsonl"
            write_pi_session_records(session_path, records)
            args += ["--session", str(session_path)]

    args.append(new_message_text)

    from omnigent.context_assembly.blindfold import one_shot_env

    env = one_shot_env(keep=frozenset({"OPENROUTER_API_KEY"}))
    env["PI_CODING_AGENT_DIR"] = str(fresh_config_dir)
    resolved_command = shutil.which(command) or command

    response_text: str | None = None
    error: str | None = None
    tool_call_count = 0
    setup_ms = (time.monotonic() - setup_started) * 1000
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            resolved_command,
            *args,
            cwd=str(workspace),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_TURN_TIMEOUT_SECONDS
            )
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            error = f"pi --print timed out after {_TURN_TIMEOUT_SECONDS:.0f}s"
        else:
            # Parse regardless of exit code: a crash partway through the turn
            # can still have written useful tool-call events before it died.
            items, final_text = parse_pi_json_events(stdout.decode("utf-8", errors="replace"))
            tool_call_count = sum(1 for item in items if item.item_type == "function_call")
            await post_oneshot_items(
                client, session_id=session_id, response_id=turn_id, items=items
            )
            if proc.returncode == 0:
                # final_text is None only if the process exited 0 without ever
                # emitting an assistant message — not expected, but "" beats
                # crashing on a response the caller must still return.
                response_text = (final_text or "").strip()
            else:
                error = (
                    f"pi --print exited {proc.returncode}: "
                    f"{stderr.decode('utf-8', errors='replace').strip()[:2000]}"
                )
    except OSError as exc:
        error = f"Could not start {resolved_command!r}: {exc}"
    duration_s = time.monotonic() - started

    _logger.info(
        "blindfold pi-native turn=%s session=%s setup_ms=%.0f process_s=%.2fs ok=%s "
        "has_memory_block=%s resumed_with_items=%d tool_calls=%d",
        turn_id,
        session_id,
        setup_ms,
        duration_s,
        error is None,
        "<long_term_memory>" in system_text,
        len(selected_items),
        tool_call_count,
    )

    with contextlib.suppress(OSError):
        shutil.rmtree(fresh_config_dir, ignore_errors=True)

    if error is not None:
        return BlindfoldTurnResult(handled=True, error=error)
    return BlindfoldTurnResult(handled=True, response_text=response_text)
