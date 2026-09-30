"""Blindfold-mode per-turn execution for codex-native.

Mirrors ``omnigent.harnesses.claude_native.blindfold`` for the ``codex`` CLI
— see that module's docstring for why blindfold turns run as disposable
one-shot processes instead of using the resident app-server thread (killing
the shared app-server thread is what the runner's reconnect loop treats as
end-of-session, the same hazard as the tmux-pane harnesses).

Each turn:

1. Reads the connection state ``write_server_connection`` persisted at bridge
   prepare time; not blindfolded (or never written for this session) exits
   immediately with ``handled=False``.
2. Calls ``fetch_blindfold_turn_context`` (contract §2/§3).
3. Rebuilds a private Codex rollout from the assembler's selected history
   using the existing rebuilder (``_codex_rollout_records_from_session_items``),
   fed only the selected items instead of the whole record.
4. Writes the system text into a fresh ``config.toml``'s top-level
   ``developer_instructions`` key (Codex's system-prompt channel — additive
   to its own built-in instructions, matching contract mode ``"append"``;
   this is the same key the resident app-server path syncs per-turn, see
   ``app_server.py:_sync_codex_developer_instructions``, but a one-shot fresh
   ``CODEX_HOME`` never had a prior value to preserve, so this module writes
   it directly instead of reusing that function's journaling).
5. Runs ``codex exec [resume <fresh id>] <message> --model <model>`` under a
   fresh, disposable ``CODEX_HOME`` — no global ``AGENTS.md`` link (the
   directory is empty tempdir, so there is nothing to unlink), vendor memory
   off, new CLI session every turn.
6. Reports the outcome via ``/v1/sessions/{id}/context/observe``, best-effort.
7. Deletes the whole disposable ``CODEX_HOME``.

Fails closed (contract §7) exactly like the claude-native/pi-native modules:
once a session is confirmed blindfolded, every later failure stays inside
the one-shot turn rather than falling back to the resident app-server.
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
    BlindfoldTurnContext,
    fetch_blindfold_turn_context,
    is_blindfolded,
    read_server_connection,
    record_user_message,
)

_logger = logging.getLogger(__name__)

_TURN_TIMEOUT_SECONDS = 180.0
_SESSION_FETCH_TIMEOUT_SECONDS = 5.0
_DEFAULT_MODEL = "gpt-5-nano"
_HISTORY_FORMAT = "codex_rollout"


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
    command: str = "codex",
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
                harness_name="codex-native",
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
) -> BlindfoldTurnResult:
    """Run one disposable ``codex exec`` process and report the outcome."""
    # Measured separately from the process wall time below so a latency
    # table can tell "building the fresh config dir/history file" apart
    # from "the CLI process itself" (dominated by the model call).
    setup_started = time.monotonic()
    # Codex refuses to create its helper binaries under the system tempdir
    # ("Refusing to create helper binaries under temporary dir") — verified
    # against the real CLI (0.159.2) — so the disposable CODEX_HOME lives
    # under the process's own home instead of tempfile's default /tmp base.
    _blindfold_tmp_root = Path.home() / ".omnigent-blindfold"
    _blindfold_tmp_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    fresh_codex_home = Path(
        tempfile.mkdtemp(prefix="omnigent-blindfold-codex-", dir=str(_blindfold_tmp_root))
    )
    workspace = Path.cwd().resolve()
    fresh_external_id = str(uuid.uuid4())

    # Codex's own CLI-key auth reads CODEX_HOME/auth.json, not the
    # OPENAI_API_KEY env var directly (verified against the real CLI: an
    # inherited env var alone 401s) — mirrors what the resident app-server
    # path's auth.json bridging does for a real login, but for the one-shot
    # API-key case there is nothing to bridge from, so this writes it fresh.
    _api_key = os.environ.get("OPENAI_API_KEY")
    if _api_key:
        (fresh_codex_home / "auth.json").write_text(
            json.dumps({"OPENAI_API_KEY": _api_key}), encoding="utf-8"
        )

    resumed = False
    if selected_items:
        from omnigent.harnesses.codex_native.main import (
            _codex_resume_rollout_path,
            _codex_rollout_records_from_session_items,
        )

        records = _codex_rollout_records_from_session_items(
            selected_items,
            session_id=session_id,
            external_session_id=fresh_external_id,
            cwd=workspace,
            model_provider="openai",
            cli_version="0.0.0",
        )
        if records:
            target = _codex_resume_rollout_path(fresh_codex_home, fresh_external_id)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with target.open("w", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record, separators=(",", ":")) + "\n")
            resumed = True

    if system_text:
        # Codex's system-prompt channel: a top-level developer_instructions
        # key in config.toml, additive to its own built-in instructions
        # (contract mode "append"). tomlkit serializes the string safely
        # (quotes/newlines/unicode) — a fresh CODEX_HOME has no prior config
        # to preserve, so this writes it directly rather than reusing the
        # resident-thread path's journaling (app_server.py's
        # _sync_codex_developer_instructions), which exists to restore a
        # user's own config.toml value across many turns — moot here.
        import tomlkit

        doc = tomlkit.document()
        doc["developer_instructions"] = system_text
        (fresh_codex_home / "config.toml").write_text(tomlkit.dumps(doc), encoding="utf-8")

    # codex exec's plain stdout is a human-readable transcript (reasoning,
    # tool-call summaries), not just the final answer; --output-last-message
    # isolates exactly that, the same clean signal claude's `--output-format
    # text` and pi's `--print` give directly on stdout.
    last_message_path = fresh_codex_home / "last-message.txt"
    args = ["exec", "resume", fresh_external_id, new_message_text] if resumed else [
        "exec",
        new_message_text,
    ]
    args += ["--output-last-message", str(last_message_path)]
    # The one-shot workspace is whatever the executor's own cwd is, which is
    # not necessarily a trusted git repo from Codex's point of view; skip
    # that check rather than force every blindfolded workspace to be one.
    args.append("--skip-git-repo-check")
    # Vendor memory off (contract §5.2): a fresh CODEX_HOME has no
    # $CODEX_HOME/AGENTS.md to link away, but Codex separately auto-discovers
    # a workspace-level AGENTS.md/PROJECT.md by walking up from cwd — verified
    # leaking a planted /root/AGENTS.md into a blindfolded turn's answer
    # without this override. project_doc_max_bytes=0 disables that discovery
    # (distinct from developer_instructions above, which is this turn's own
    # assembled system text, not vendor project-doc auto-loading).
    args += ["-c", "project_doc_max_bytes=0"]
    if model:
        args += ["--model", model]

    env = dict(os.environ)
    env["CODEX_HOME"] = str(fresh_codex_home)
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
            error = f"codex exec timed out after {_TURN_TIMEOUT_SECONDS:.0f}s"
        else:
            if proc.returncode == 0:
                try:
                    response_text = last_message_path.read_text(encoding="utf-8").strip()
                except OSError:
                    response_text = stdout.decode("utf-8", errors="replace").strip()
            else:
                error = (
                    f"codex exec exited {proc.returncode}: "
                    f"{stderr.decode('utf-8', errors='replace').strip()[:2000]}"
                )
    except OSError as exc:
        error = f"Could not start {resolved_command!r}: {exc}"
    duration_s = time.monotonic() - started

    _logger.info(
        "blindfold codex-native turn=%s session=%s setup_ms=%.0f process_s=%.2fs ok=%s "
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
        shutil.rmtree(fresh_codex_home, ignore_errors=True)

    if error is not None:
        return BlindfoldTurnResult(handled=True, error=error)
    return BlindfoldTurnResult(handled=True, response_text=response_text)
