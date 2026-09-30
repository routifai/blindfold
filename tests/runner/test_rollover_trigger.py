"""Tests for the rollover trigger hook (post-turn, native sessions only).

Exercises ``_maybe_apply_rollover`` / ``_finish_turn_and_maybe_roll_over``
(exposed on ``app.state`` for testing) directly, rather than driving a full
native-pane turn — the counting/threshold/item-shape logic itself is unit
tested in ``tests/context/test_rollover.py`` against the real production
functions; this covers the runner-side wiring: the native + label gate, the
single-flight guard, and "never mid-turn".
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnigent.context.rollover import CHECKPOINT_HEADER
from omnigent.runner.app import create_runner_app
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient
from tests.runner.helpers import NullServerClient


class _FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeServerClient(NullServerClient):
    """Serves ``GET .../items`` from an in-memory item list, ``GET .../labels``
    from an in-memory label map (the durable rollover-label cache's fallback
    fetch when nothing has warmed it yet, as in these tests), and records
    ``POST .../events`` compaction writes — everything else falls back to
    :class:`NullServerClient`'s no-op stub."""

    def __init__(self, items: list[dict[str, Any]], labels: dict[str, str] | None = None) -> None:
        self._items = items
        self._labels = labels or {}
        self.posted_events: list[dict[str, Any]] = []
        self.posted_usage_events: list[dict[str, Any]] = []

    async def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        # A real yield point: without it, this fake never suspends and two
        # gathered callers would run start-to-finish back to back, hiding a
        # broken single-flight guard behind an accidental lack of interleaving.
        await asyncio.sleep(0)
        if url.endswith("/labels"):
            return _FakeResponse({"labels": self._labels})
        if not url.endswith("/items"):
            return await super().get(url, **kwargs)
        params = kwargs.get("params") or {}
        order = params.get("order", "asc")
        before = params.get("before")
        limit = int(params.get("limit", 100))
        items = list(self._items)
        if before is not None:
            idx = next((i for i, it in enumerate(items) if it["id"] == before), len(items))
            items = items[:idx]
        if order == "desc":
            items = list(reversed(items))
        page = items[:limit]
        has_more = len(items) > limit
        return _FakeResponse(
            {
                "data": page,
                "has_more": has_more,
                "last_id": page[-1]["id"] if page else None,
            }
        )

    async def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        body = kwargs.get("json") or {}
        if url.endswith("/events") and body.get("type") == "compaction":
            self.posted_events.append(body)
            return _FakeResponse({"id": "comp_new"}, status_code=201)
        if url.endswith("/events") and body.get("type") == "external_session_usage":
            self.posted_usage_events.append(body)
            return _FakeResponse({}, status_code=200)
        return await super().post(url, **kwargs)


class _FakeReaper:
    def __init__(self) -> None:
        self.reaped_ids: list[str] = []

    async def reap_now(self, conversation_id: str) -> bool:
        self.reaped_ids.append(conversation_id)
        return True


class _FakeLLMResponses:
    async def create(self, **kwargs: Any) -> Any:
        from omnigent.llms.types import MessageOutput, OutputText, Response

        return Response(
            output=[MessageOutput(content=[OutputText(text="ROLLING SUMMARY")])],
            model="test-model",
        )


class _FakeLLMClient:
    responses = _FakeLLMResponses()


def _msg(item_id: str, role: str, text: str) -> dict[str, Any]:
    return {
        "id": item_id,
        "type": "message",
        "status": "completed",
        "response_id": f"resp_{item_id}",
        "created_at": 1,
        "role": role,
        "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}],
    }


_NATIVE_SPEC = AgentSpec(
    spec_version=1,
    name="t",
    executor=ExecutorSpec(
        type="omnigent", config={"harness": "claude-native"}, model="test-model"
    ),
)


async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
    del agent_id, session_id
    return _NATIVE_SPEC


async def _create_native_session(app: Any, conv_id: str) -> None:
    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "some_agent_id"},
        )
        assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_no_trigger_when_label_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Upstream unchanged: a native session with no rollover label never
    fetches items or posts a compaction event, even after many messages."""
    conv_id = "conv_rollover_unset"
    items = [_msg(f"m{i}", "user" if i % 2 == 0 else "assistant", f"msg {i}") for i in range(10)]
    fake_client = _FakeServerClient(items, labels={})
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=fake_client,
    )
    await _create_native_session(app, conv_id)

    await app.state.maybe_apply_rollover(conv_id)

    assert fake_client.posted_events == []


@pytest.mark.asyncio
async def test_never_triggers_mid_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """clean=False (error/interrupt exit) must never roll over, even when
    the session is in rollover mode and long past its threshold."""
    conv_id = "conv_rollover_midturn"
    items = [_msg(f"m{i}", "user" if i % 2 == 0 else "assistant", f"msg {i}") for i in range(10)]
    labels = {
        "omnigent.context.mode": "rollover",
        "omnigent.context.rollover_at_tokens": "1",
    }
    fake_client = _FakeServerClient(items, labels=labels)
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=fake_client,
    )
    await _create_native_session(app, conv_id)
    monkeypatch.setattr("omnigent.runner.app._get_runner_llm_client", lambda: _FakeLLMClient())

    await app.state.finish_turn_and_maybe_roll_over(conv_id, clean=False)

    assert fake_client.posted_events == []


@pytest.mark.asyncio
async def test_triggers_and_recycles_pane_when_over_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rollover session over its (tiny) threshold posts a compaction item
    shaped like other producers' and recycles its native pane."""
    conv_id = "conv_rollover_trigger"
    items = [_msg(f"m{i}", "user" if i % 2 == 0 else "assistant", f"msg {i}") for i in range(10)]
    labels = {
        "omnigent.context.mode": "rollover",
        "omnigent.context.rollover_at_tokens": "1",
        "omnigent.context.rollover_keep_messages": "2",
    }
    fake_client = _FakeServerClient(items, labels=labels)
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=fake_client,
    )
    await _create_native_session(app, conv_id)
    monkeypatch.setattr("omnigent.runner.app._get_runner_llm_client", lambda: _FakeLLMClient())
    fake_reaper = _FakeReaper()
    app.state.native_pane_reaper = fake_reaper

    await app.state.maybe_apply_rollover(conv_id)

    assert len(fake_client.posted_events) == 1
    data = fake_client.posted_events[0]["data"]
    assert data["summary"] == f"{CHECKPOINT_HEADER}\n\nROLLING SUMMARY"
    assert data["last_item_id"] == "m9"
    compacted = data["compacted_messages"]
    # Summary pair first, then the last 2 kept messages (m8, m9).
    assert [m.get("id") for m in compacted if "id" in m] == ["m8", "m9"]
    assert fake_reaper.reaped_ids == [conv_id]
    # Bug fix (PLAN.md "A, revision 2" point 5): the reported-usage label is
    # refreshed to the real post-rollover figure so a stale pre-rollover
    # number can't immediately re-trigger on the next turn.
    assert len(fake_client.posted_usage_events) == 1
    assert fake_client.posted_usage_events[0]["data"]["context_tokens"] == data["token_count"]


@pytest.mark.asyncio
async def test_single_flight_guard_skips_concurrent_rollover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rollover already in flight for a session is never started twice."""
    conv_id = "conv_rollover_single_flight"
    items = [_msg("m0", "user", "hi")]
    labels = {"omnigent.context.mode": "rollover", "omnigent.context.rollover_at_tokens": "1"}
    fake_client = _FakeServerClient(items, labels=labels)
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=fake_client,
    )
    await _create_native_session(app, conv_id)

    # maybe_apply_rollover is called concurrently; the single-flight guard
    # must ensure it never double-posts regardless of interleaving.
    monkeypatch.setattr("omnigent.runner.app._get_runner_llm_client", lambda: _FakeLLMClient())
    await asyncio.gather(
        app.state.maybe_apply_rollover(conv_id),
        app.state.maybe_apply_rollover(conv_id),
    )

    assert len(fake_client.posted_events) <= 1
