"""A blindfolded session must never launch a resident native pane/process.

Blindfold turns (see ``omnigent.harnesses.*_native.blindfold``) run as
disposable one-shot CLI processes that read only the bridge-local
server-connection file. Launching a resident, interactive TUI anyway — as
``_auto_create_claude_terminal`` / ``_auto_create_pi_terminal`` /
``_auto_create_codex_terminal`` used to do unconditionally — gives a person
opening the terminal-view UI a live CLI with its own vendor memory (breaking
blindfold's "sees only what the assembler hands it" guarantee), burns a
process per session that blindfold never drives, and that unused pane's own
eventual exit used to spuriously fail the active one-shot turn and stall the
next one's dispatch.

These tests pin:
(a) a blindfolded session's ``_auto_create_*_terminal`` prepares the bridge
    dir and writes the server-connection file, but never calls
    ``launch_required_terminal`` (claude/pi) or boots the app-server (codex),
    and returns ``None``;
(b) OFF (no confirmed-blindfold labels) is covered by the existing
    ``test_app_sessions_native_terminals_autocreate.py`` /
    ``test_app_sessions_native_terminals_runtime.py`` suites, unchanged;
(c) consecutive blindfolded turns dispatch without ever calling into the
    terminal-lifecycle path at all (``_ensure_native_terminal_for_turn``'s
    per-turn self-heal skips it entirely);
(d) the terminal-ensure REST endpoint (``POST .../resources/terminals``,
    which the server's own pre-flight calls before every native message and
    treats as authoritative) returns a 2xx for a blindfolded session's
    absent pane — a non-2xx there reads as "the native terminal failed to
    start" and fails the whole user turn server-side.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.context_assembly.blindfold import read_server_connection, write_server_connection
from omnigent.entities.session_resources import SessionResourceView
from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.harnesses.claude_native.bridge import bridge_dir_for_conversation_id
from omnigent.harnesses.codex_native import bridge as codex_native_bridge
from omnigent.harnesses.pi_native import bridge as pi_native_bridge
from omnigent.runner import create_runner_app
from omnigent.runner.app import (
    _auto_create_claude_terminal,
    _auto_create_codex_terminal,
    _auto_create_pi_terminal,
    _PiNativeLaunchConfig,
)
from omnigent.runner.session_init_protocol import RunnerSessionInitEnvelope
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.terminals import TerminalRegistry
from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient
from tests.runner.helpers import NullServerClient


class _RaisingRequiredTerminalRegistry:
    """A resource registry that fails the test if a terminal launch is attempted."""

    terminal_registry = None

    async def launch_required_terminal(self, **kwargs: Any) -> SessionResourceView:
        raise AssertionError(
            f"blindfolded session must never launch a resident terminal; got {kwargs!r}"
        )


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_skips_pane_when_blindfolded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blindfolded claude-native session preps the bridge but launches no pane."""
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")

    session_id = "c1a2b3c4d5e6f70819293a4b5c6d7e8f"
    session_init = RunnerSessionInitEnvelope.model_validate(
        {
            "protocol_version": 2,
            "server_version": "0.6.0.dev0",
            "session_id": session_id,
            "agent_id": "agent",
            "snapshot": {
                "created_at": 10,
                "updated_at": 11,
                "workspace": str(tmp_path),
                "labels": {"omnigent.blindfold": "true"},
            },
        }
    )

    result = await _auto_create_claude_terminal(
        session_id,
        _RaisingRequiredTerminalRegistry(),
        lambda _sid, _evt: None,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        session_init=session_init,
    )

    assert result is None
    bridge_dir = bridge_dir_for_conversation_id(session_id)
    connection = read_server_connection(bridge_dir)
    assert connection is not None
    assert connection.blindfolded is True


@pytest.mark.asyncio
async def test_auto_create_pi_terminal_skips_pane_when_blindfolded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blindfolded pi-native session preps the bridge but launches no pane."""
    monkeypatch.setattr(pi_native_bridge, "_BRIDGE_ROOT", tmp_path / "pi-bridge")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")

    session_id = "d1e2f3a4b5c6d7e8f90a1b2c3d4e5f60"

    async def _fake_launch_config(**_kwargs: Any) -> _PiNativeLaunchConfig:
        return _PiNativeLaunchConfig(
            workspace=tmp_path,
            server_url="http://127.0.0.1:8000",
            terminal_launch_args=None,
            external_session_id=None,
            labels={"omnigent.blindfold": "true"},
        )

    monkeypatch.setattr("omnigent.runner.app._pi_native_launch_config", _fake_launch_config)

    result = await _auto_create_pi_terminal(
        session_id,
        _RaisingRequiredTerminalRegistry(),
        lambda _sid, _evt: None,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    assert result is None
    bridge_dir = pi_native_bridge.bridge_dir_for_session_id(session_id)
    connection = read_server_connection(bridge_dir)
    assert connection is not None
    assert connection.blindfolded is True


@pytest.mark.asyncio
async def test_auto_create_codex_terminal_skips_app_server_when_blindfolded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blindfolded codex-native session preps the bridge but boots no app-server."""
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")

    session_id = "e1f2a3b4c5d6e7f809a1b2c3d4e5f607"

    def _handle_request(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v1/sessions/{session_id}"
        return httpx.Response(
            200,
            json={"workspace": str(tmp_path), "labels": {"omnigent.blindfold": "true"}},
        )

    fake_client = httpx.AsyncClient(
        base_url="http://test-server",
        transport=httpx.MockTransport(_handle_request),
    )

    result = await _auto_create_codex_terminal(
        session_id,
        _RaisingRequiredTerminalRegistry(),
        lambda _sid, _evt: None,
        server_client=fake_client,
    )

    assert result is None
    bridge_dir = codex_native_bridge.bridge_dir_for_bridge_id(session_id)
    connection = read_server_connection(bridge_dir)
    assert connection is not None
    assert connection.blindfolded is True


def _native_spec(harness: str) -> AgentSpec:
    """Return a native agent spec for session create."""
    return AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": harness}),
    )


@pytest.mark.asyncio
async def test_consecutive_blindfolded_turns_never_touch_terminal_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only session-create calls pane auto-create; per-turn dispatch never does.

    Regression guard for the fixed bug: the runner used to auto-create a
    resident claude-native pane at session init AND re-attempt it on every
    turn dispatch (``_ensure_native_terminal_for_turn``'s self-heal), even
    for a blindfolded session that never uses it. That pane's own eventual
    exit (it is genuinely unused) used to spuriously fail the active
    one-shot turn and, via the shared per-conversation harness-release lock,
    stall the *next* turn's dispatch for however long the dead pane's
    process tree took to tear down.

    The real ``_auto_create_claude_terminal`` runs unstubbed (its own
    blindfold guard, pinned by
    ``test_auto_create_claude_terminal_skips_pane_when_blindfolded`` above,
    safely returns ``None`` with no pane) — only counted, so this test
    isolates the property under test: it must be called exactly once, at
    session create, never again from the three turns dispatched after.
    """
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")

    from omnigent.runner.native import orchestration as orchestration_mod

    real_auto_create = orchestration_mod._auto_create_claude_terminal
    auto_create_calls: list[str] = []

    async def _counting_auto_create(session_id: str, *args: object, **kwargs: object) -> Any:
        auto_create_calls.append(session_id)
        return await real_auto_create(session_id, *args, **kwargs)

    monkeypatch.setattr(
        "omnigent.runner.native.orchestration._auto_create_claude_terminal",
        _counting_auto_create,
    )
    monkeypatch.setattr(
        "omnigent.runner.native._auto_create_claude_terminal",
        _counting_auto_create,
    )

    async def _counting_launch_claude(ctx: Any) -> Any:
        return await _counting_auto_create(
            ctx.session_id,
            ctx.resource_registry,
            ctx.publish_event,
            server_client=ctx.server_client,
            bundle_dir=ctx.bundle_dir,
            agent_name=ctx.agent_name,
            agent_spec=ctx.agent_spec,
            skills_filter=ctx.skills_filter,
            session_init=ctx.session_init,
            auth_token_factory=ctx.auth_token_factory,
            resolve_launch_config=ctx.resolve_launch_config,
            record_launch_config=ctx.record_launch_config,
        )

    monkeypatch.setattr(
        "omnigent.runner.native.orchestration._launch_claude",
        _counting_launch_claude,
    )
    monkeypatch.setattr("omnigent.runner.native._launch_claude", _counting_launch_claude)

    conv_id = "f1a2b3c4d5e6f708192a3b4c5d6e7f80"

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return _native_spec("claude-native")

    # An empty scripted stream ends immediately on every dispatch (real
    # content is irrelevant here — what matters is whether turn dispatch
    # ever reaches for the terminal-lifecycle path), letting each of the
    # three turns below settle before the next is sent.
    harness_client = _ScriptedHarnessClient([])
    registry = TerminalRegistry()
    app = create_runner_app(
        process_manager=_FakeProcessManager(harness_client),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=registry,
    )
    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": conv_id,
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
                "labels": {"omnigent.blindfold": "true"},
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        for i in range(3):
            resp = await client.post(
                f"/v1/sessions/{conv_id}/events",
                json={
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": f"message {i}"}],
                    },
                },
            )
            assert resp.status_code in (200, 202), resp.text

    assert auto_create_calls == [conv_id], (
        f"pane auto-create must run exactly once (session create) and never again "
        f"from turn dispatch/self-heal; got {auto_create_calls!r}"
    )
    # No terminal was ever registered for this session.
    assert registry.get(conv_id, "claude", "main") is None


@pytest.mark.asyncio
async def test_ensure_terminal_endpoint_succeeds_for_a_blindfolded_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``POST .../resources/terminals`` must be a 2xx for a blindfolded session.

    Regression guard: the server's own ``_ensure_native_terminal_ready``
    (omnigent/server/routes/_sessions/orchestration.py) calls this exact
    endpoint before forwarding EVERY native user message and treats it as
    "the authoritative readiness check ... any non-2xx response ... fails
    this user turn quickly with a durable error item." A first version of
    this fix returned 409 for a blindfolded session's absent pane, which the
    server correctly (per its own contract) read as "the terminal failed to
    start" and failed every turn from the second one onward with a
    ``blindfold_no_terminal`` error item instead of ever dispatching it —
    the same symptom as the original bug, from a different cause. "No
    terminal" must read as success for a blindfolded session, not failure.

    :param tmp_path: Temporary directory for fake terminal paths.
    """
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")

    conv_id = "a0b1c2d3e4f5061728394a5b6c7d8e9f"

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return _native_spec("claude-native")

    registry = TerminalRegistry()
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=registry,
    )
    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": conv_id,
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
                "labels": {"omnigent.blindfold": "true"},
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        # Session create's own bridge-prepare call may or may not carry
        # labels depending on how much of the envelope this bare test
        # client's request populated — irrelevant to what's under test here
        # (the ensure endpoint's response code), so pin the ground truth the
        # same unconditional way a labels-bearing caller always does.
        write_server_connection(
            bridge_dir_for_conversation_id(conv_id),
            base_url="http://127.0.0.1:8000",
            headers={},
            labels={"omnigent.blindfold": "true"},
        )

        # Exactly what the server's _ensure_native_terminal_ready posts
        # before forwarding a native message.
        ensure_resp = await client.post(
            f"/v1/sessions/{conv_id}/resources/terminals",
            json={
                "terminal": "claude",
                "session_key": "main",
                "ensure_native_terminal": True,
                "persist_resource_event": True,
            },
        )

    assert ensure_resp.status_code < 400, (
        f"the server's pre-flight ensure treats any non-2xx as a definitive terminal-"
        f"start failure and fails the turn; got {ensure_resp.status_code}: {ensure_resp.text}"
    )
    assert registry.get(conv_id, "claude", "main") is None
