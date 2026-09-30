"""Blindfold-mode per-turn execution for claude-native.

Implements the context-assembly contract v0.2
(``blindfold-mission/context-assembly-contract.md``) for sessions labelled
``omnigent.blindfold=true``. Normal claude-native turns inject the user's
message into a long-lived, interactive tmux pane that Claude Code keeps
running across the whole conversation — the pane *is* Claude's memory of
prior turns. Blindfold mode is the opposite: the CLI must see only what the
assembler hands it, freshly, every single turn.

Reusing the interactive pane for that would require killing it between
turns, but ``kill_session`` (see its docstring in ``bridge.py``) is the same
primitive the "Stop session" UI action uses — the runner's reconnect loop
treats a dead pane as "the conversation ended", not "ready for the next
turn". So blindfold turns never touch the pane at all: each turn is one
disposable, non-interactive ``claude -p`` process (the same one-shot pattern
``omnigent/runner/background_titles/claude_native.py`` already uses for
background titles), run with a throwaway ``CLAUDE_CONFIG_DIR`` and thrown
away afterwards.

Each turn:

1. Reads the connection state ``write_server_connection``
   (``omnigent.context_assembly.blindfold``) persisted at bridge prepare
   time; a session it was never written for, or one it marked as not
   blindfolded, exits immediately with ``handled=False`` — the caller runs
   the normal pane-typing turn.
2. Calls ``fetch_blindfold_turn_context`` (contract §2/§3) to learn what the
   CLI should see this turn.
3. Rebuilds a private Claude project transcript from the assembler's
   selected history using the *existing* rebuilder
   (``_claude_transcript_records_from_session_items``), fed only the
   selected items instead of the whole record (contract §5.4).
4. Runs ``claude -p <message> --resume <fresh id> --setting-sources ""
   --append-system-prompt <system + memory>`` under a fresh, disposable
   ``CLAUDE_CONFIG_DIR`` — vendor memory off (contract §5.2), new CLI
   session every turn (contract §5.3).
5. Reports the outcome via ``/v1/sessions/{id}/context/observe`` (contract
   §4), best-effort.
6. Deletes the whole disposable ``CLAUDE_CONFIG_DIR`` — nothing survives to
   the next turn (contract §5.4).

Fails closed (contract §7): once step 1 has confirmed this session is
blindfolded, every later failure (assemble, transcript build, even the
session-metadata fetch) still runs the turn — worst case with the new
message alone — rather than falling back to the normal warm/history-carrying
pane, which would leak full history into a session that promised none.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
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

# Per-turn CLI wall-clock budget. Generous: cold model calls plus a fresh
# process boot can take a while under load, but a turn must not hang forever.
_TURN_TIMEOUT_SECONDS = 180.0

_SESSION_FETCH_TIMEOUT_SECONDS = 5.0

_DEFAULT_MODEL = "claude-haiku-4-5-20251001"

_HISTORY_FORMAT = "claude_jsonl"


@dataclass(frozen=True)
class BlindfoldTurnResult:
    """Outcome of :func:`maybe_run_blindfold_turn`.

    :param handled: ``False`` means "this isn't a blindfold turn" — the
        caller must fall back to its normal behavior unchanged. ``True``
        means blindfold ran the turn to completion (possibly failing closed
        internally) and the caller should surface *response_text*/*error*
        instead of its usual path.
    :param response_text: The assistant's reply text, when the turn produced
        one.
    :param error: A user-facing error message, when the turn could not be
        completed at all (e.g. the CLI process failed to start).
    """

    handled: bool
    response_text: str | None = None
    error: str | None = None


async def maybe_run_blindfold_turn(
    *,
    bridge_dir: Path,
    session_id: str,
    new_message_text: str,
    command: str = "claude",
) -> BlindfoldTurnResult:
    """Run one turn under the blindfold contract, if this session opts in.

    :param bridge_dir: This session's native Claude bridge directory.
    :param session_id: Omnigent conversation id.
    :param new_message_text: The user's new message text for this turn.
    :param command: Claude CLI executable to run.
    :returns: ``BlindfoldTurnResult(handled=False)`` when this session isn't
        blindfold-labelled. Otherwise the turn's result, per contract §7
        always populated (fail-closed) rather than raising.
    """
    connection = read_server_connection(bridge_dir)
    if connection is None:
        return BlindfoldTurnResult(handled=False)
    # The common case (every non-blindfold session, i.e. almost all of them):
    # the flag was already known at bridge-prepare time — no network call.
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
            # Unknown from the local file (the prepare-time call site didn't
            # have labels cheaply available) — this is the only case where a
            # fetch failure means "act as if this isn't blindfold": we never
            # confirmed it one way or the other, so there is nothing to fail
            # *closed* about yet.
            if not is_blindfolded(labels if isinstance(labels, dict) else None):
                return BlindfoldTurnResult(handled=False)

        # From here on, this IS a blindfold session (confirmed locally, or
        # just confirmed over the network) — every further failure fails
        # closed within the one-shot turn below, never back out to the
        # normal pane-typing path (that would leak full history).
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
            harness_name="claude-native",
            model=model,
            context_window_tokens=200_000,
            history_format=_HISTORY_FORMAT,
            fallback_instructions=None,
        )

        turn_id = f"turn_{uuid.uuid4().hex[:16]}"
        result = await _run_one_shot(
            bridge_dir=bridge_dir,
            session_id=session_id,
            turn_id=turn_id,
            new_message_text=new_message_text,
            system_text=ctx.system_prompt,
            selected_items=ctx.prior_history_items,
            model=model,
            command=command,
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
    bridge_dir: Path,
    session_id: str,
    turn_id: str,
    new_message_text: str,
    system_text: str,
    selected_items: list[dict],
    model: str,
    command: str,
) -> BlindfoldTurnResult:
    """Run one disposable ``claude -p`` process and report the outcome."""
    # Measured separately from the process wall time below so a latency
    # table can tell "building the fresh config dir + transcript" apart from
    # "the CLI process itself" (dominated by the model call).
    setup_started = time.monotonic()
    # A fresh, disposable CLAUDE_CONFIG_DIR per turn is the whole point
    # (contract §5.2: vendor memory off) — plain system tempdir, deleted at
    # the end of this function regardless of outcome.
    fresh_config_dir = Path(tempfile.mkdtemp(prefix="omnigent-blindfold-claude-"))
    workspace = Path.cwd().resolve()
    fresh_external_id = str(uuid.uuid4())
    args = ["-p", new_message_text, "--output-format", "text", "--setting-sources", ""]
    if system_text:
        args += ["--append-system-prompt", system_text]
    if model:
        args += ["--model", model]

    if selected_items:
        from omnigent.harnesses.claude_native.main import (
            _claude_transcript_records_from_session_items,
            _sanitize_claude_project_name,
        )

        records = _claude_transcript_records_from_session_items(
            selected_items,
            session_id=session_id,
            external_session_id=fresh_external_id,
            cwd=workspace,
            bridge_dir=bridge_dir,
        )
        if records:
            project_name = _sanitize_claude_project_name(str(workspace))
            project_dir = fresh_config_dir / "projects" / project_name
            project_dir.mkdir(parents=True, exist_ok=True)
            transcript_path = project_dir / f"{fresh_external_id}.jsonl"
            with transcript_path.open("w", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record, separators=(",", ":")) + "\n")
            args += ["--resume", fresh_external_id]

    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(fresh_config_dir)
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
            error = f"claude -p timed out after {_TURN_TIMEOUT_SECONDS:.0f}s"
        else:
            if proc.returncode == 0:
                response_text = stdout.decode("utf-8", errors="replace").strip()
            else:
                error = (
                    f"claude -p exited {proc.returncode}: "
                    f"{stderr.decode('utf-8', errors='replace').strip()[:2000]}"
                )
    except OSError as exc:
        error = f"Could not start {resolved_command!r}: {exc}"
    duration_s = time.monotonic() - started

    _logger.info(
        "blindfold claude-native turn=%s session=%s setup_ms=%.0f process_s=%.2fs ok=%s "
        "has_memory_block=%s resumed_with_items=%d",
        turn_id,
        session_id,
        setup_ms,
        duration_s,
        error is None,
        "<long_term_memory>" in system_text,
        len(selected_items),
    )

    with contextlib.suppress(OSError):
        shutil.rmtree(fresh_config_dir, ignore_errors=True)

    if error is not None:
        return BlindfoldTurnResult(handled=True, error=error)
    return BlindfoldTurnResult(handled=True, response_text=response_text)
