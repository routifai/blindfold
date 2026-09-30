"""Unit tests for the per-harness blindfold turn modules.

Covers the "surgical, guarded branch" contract for all three harnesses:
- OFF (no connection file, or one marked not-blindfolded): handled=False,
  zero network calls, so the caller's existing turn path is untouched.
- ON: a full one-shot turn runs end to end against a mock server, using a
  harmless real binary (``echo``/``cat``) in place of claude/pi/codex so the
  subprocess plumbing (env, args, transcript file, cleanup, observe) is
  exercised without needing the real CLIs installed in this environment.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.context_assembly.blindfold import write_server_connection
from omnigent.harnesses.claude_native import blindfold as claude_blindfold
from omnigent.harnesses.codex_native import blindfold as codex_blindfold
from omnigent.harnesses.pi_native import blindfold as pi_blindfold

# (module, harness_name) — same shape across all three; iterated for the
# OFF-path tests that don't depend on harness-specific transcript rebuilding.
_MODULES = [
    (claude_blindfold, "claude-native"),
    (pi_blindfold, "pi-native"),
    (codex_blindfold, "codex-native"),
]


def _refuse_any_request(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"no network call expected, got: {request.method} {request.url}")


_RealAsyncClient = httpx.AsyncClient


def _patch_async_client(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> None:
    """Force every ``httpx.AsyncClient(...)`` the code under test builds onto
    *transport*, without recursing into the patched constructor itself."""

    def _factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return _RealAsyncClient(*args, **{**kwargs, "transport": transport})

    monkeypatch.setattr(httpx, "AsyncClient", _factory)


class TestOffPathNeverTouchesTheNetwork:
    """The common case (every non-blindfold session) must cost nothing."""

    @pytest.mark.parametrize(("module", "harness_name"), _MODULES, ids=[m[1] for m in _MODULES])
    async def test_no_connection_file_is_handled_false(
        self, module: Any, harness_name: str, tmp_path: Path
    ) -> None:
        del harness_name
        result = await module.maybe_run_blindfold_turn(
            bridge_dir=tmp_path, session_id="conv_1", new_message_text="hello"
        )
        assert result.handled is False

    @pytest.mark.parametrize(("module", "harness_name"), _MODULES, ids=[m[1] for m in _MODULES])
    async def test_connection_file_marked_not_blindfolded_is_handled_false(
        self, module: Any, harness_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        del harness_name
        write_server_connection(
            tmp_path, base_url="http://server", headers={}, labels={}
        )  # blindfolded=False
        # If maybe_run_blindfold_turn made any HTTP call here, it would hit
        # this transport and fail loudly instead of silently succeeding.
        _patch_async_client(monkeypatch, httpx.MockTransport(_refuse_any_request))
        result = await module.maybe_run_blindfold_turn(
            bridge_dir=tmp_path, session_id="conv_1", new_message_text="hello"
        )
        assert result.handled is False


def _session_and_context_handler(
    *, model: str, system_text: str, history_items: list[dict[str, Any]]
) -> httpx.MockTransport:
    """A mock server: GET /sessions/{id} (blindfolded), /items, POST /context, /context/observe."""
    observed: list[dict[str, Any]] = []

    def _route(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/context/observe"):
            observed.append(json.loads(request.content))
            return httpx.Response(204)
        if path.endswith("/context"):
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "contract_version": "0.2",
                    "turn_id": body["turn_id"],
                    "system": {"mode": "append", "text": system_text, "digest": "sha256:x"},
                    "memory": {"items": [], "digest": "sha256:y"},
                    "history": {
                        "summary": None,
                        "items": [{"ref": i["id"]} for i in history_items]
                        + [{"ref": body["new_item_id"]}],
                        "digest": "sha256:z",
                    },
                    "audit": {
                        "memory_items": 0,
                        "history_items": len(history_items) + 1,
                        "summary": False,
                        "estimated_tokens": 10,
                        "fallback": False,
                    },
                },
            )
        if path.endswith("/items"):
            data = [*history_items, {"id": "item_new", "type": "message", "role": "user"}]
            return httpx.Response(200, json={"data": list(reversed(data)), "has_more": False})
        # GET /sessions/{id}
        return httpx.Response(
            200,
            json={"labels": {"omnigent.blindfold": "true"}, "owner": "local", "model": model},
        )

    transport = httpx.MockTransport(_route)
    transport.observed = observed  # type: ignore[attr-defined]
    return transport


class TestOnPathRunsAOneShotProcess:
    """Blindfolded turns run to completion using a stand-in binary for the CLI."""

    async def test_claude_module_runs_echo_and_reports_completion(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_server_connection(
            tmp_path,
            base_url="http://server",
            headers={},
            labels={"omnigent.blindfold": "true"},
        )
        transport = _session_and_context_handler(
            model="claude-haiku-4-5-20251001", system_text="Rules.", history_items=[]
        )
        _patch_async_client(monkeypatch, transport)
        # A shell-free stand-in for `claude -p <msg> ...`: echo prints its
        # argv, proving the subprocess actually ran with the built args.
        result = await claude_blindfold.maybe_run_blindfold_turn(
            bridge_dir=tmp_path,
            session_id="conv_1",
            new_message_text="what is my codeword?",
            command="echo",
        )
        assert result.handled is True
        assert result.error is None
        assert "what is my codeword?" in (result.response_text or "")
        assert "--setting-sources" in (result.response_text or "")
        assert "Rules." in (result.response_text or "")
        assert len(transport.observed) == 1  # type: ignore[attr-defined]
        assert transport.observed[0]["outcome"] == "completed"  # type: ignore[attr-defined]

    async def test_pi_module_runs_echo_and_reports_completion(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_server_connection(
            tmp_path,
            base_url="http://server",
            headers={},
            labels={"omnigent.blindfold": "true"},
        )
        transport = _session_and_context_handler(
            model="anthropic/claude-haiku-4.5", system_text="", history_items=[]
        )
        _patch_async_client(monkeypatch, transport)
        result = await pi_blindfold.maybe_run_blindfold_turn(
            bridge_dir=tmp_path,
            session_id="conv_1",
            new_message_text="hello",
            command="echo",
        )
        assert result.handled is True
        assert "--no-context-files" in (result.response_text or "")

    async def test_codex_module_runs_echo_falls_back_to_stdout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # echo never writes --output-last-message's file, so this exercises
        # the stdout fallback path.
        write_server_connection(
            tmp_path,
            base_url="http://server",
            headers={},
            labels={"omnigent.blindfold": "true"},
        )
        transport = _session_and_context_handler(
            model="gpt-5-nano", system_text="Be terse.", history_items=[]
        )
        _patch_async_client(monkeypatch, transport)
        result = await codex_blindfold.maybe_run_blindfold_turn(
            bridge_dir=tmp_path,
            session_id="conv_1",
            new_message_text="hello",
            command="echo",
        )
        assert result.handled is True
        assert "exec" in (result.response_text or "")
        assert "--output-last-message" in (result.response_text or "")

    async def test_failed_process_is_reported_as_an_error_not_a_leak(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_server_connection(
            tmp_path,
            base_url="http://server",
            headers={},
            labels={"omnigent.blindfold": "true"},
        )
        transport = _session_and_context_handler(model="m", system_text="", history_items=[])
        _patch_async_client(monkeypatch, transport)
        result = await claude_blindfold.maybe_run_blindfold_turn(
            bridge_dir=tmp_path,
            session_id="conv_1",
            new_message_text="hello",
            command="false",  # a real binary that always exits 1
        )
        assert result.handled is True
        assert result.error is not None
        assert result.response_text is None
