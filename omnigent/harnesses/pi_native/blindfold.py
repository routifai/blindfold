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
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote as _url_quote

import httpx

from omnigent.context_assembly.blindfold import (
    fetch_blindfold_turn_context,
    is_blindfolded,
    read_server_connection,
)

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

        turn_id = f"turn_{uuid.uuid4().hex[:16]}"
        result = await _run_one_shot(
            session_id=session_id,
            new_message_text=new_message_text,
            system_text=ctx.system_prompt,
            selected_items=ctx.prior_history_items,
            model=model,
            command=command,
            turn_id=turn_id,
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
) -> BlindfoldTurnResult:
    """Run one disposable ``pi --print`` process and report the outcome."""
    # Measured separately from the process wall time below so a latency
    # table can tell "building the fresh config dir/history file" apart
    # from "the CLI process itself" (dominated by the model call).
    setup_started = time.monotonic()
    fresh_config_dir = Path(tempfile.mkdtemp(prefix="omnigent-blindfold-pi-"))
    workspace = Path.cwd().resolve()
    fresh_external_id = str(uuid.uuid4())
    args = ["--print", "--no-context-files"]
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
            session_path = fresh_config_dir / f"{fresh_external_id}.jsonl"
            write_pi_session_records(session_path, records)
            args += ["--session", str(session_path)]

    args.append(new_message_text)

    env = dict(os.environ)
    env["PI_CODING_AGENT_DIR"] = str(fresh_config_dir)
    resolved_command = shutil.which(command) or command

    response_text: str | None = None
    error: str | None = None
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
            if proc.returncode == 0:
                response_text = stdout.decode("utf-8", errors="replace").strip()
            else:
                error = (
                    f"pi --print exited {proc.returncode}: "
                    f"{stderr.decode('utf-8', errors='replace').strip()[:2000]}"
                )
    except OSError as exc:
        error = f"Could not start {resolved_command!r}: {exc}"
    duration_s = time.monotonic() - started

    _logger.info(
        "blindfold pi-native turn=%s session=%s setup_ms=%.0f process_s=%.2fs ok=%s",
        turn_id,
        session_id,
        setup_ms,
        duration_s,
        error is None,
    )

    with contextlib.suppress(OSError):
        shutil.rmtree(fresh_config_dir, ignore_errors=True)

    if error is not None:
        return BlindfoldTurnResult(handled=True, error=error)
    return BlindfoldTurnResult(handled=True, response_text=response_text)
