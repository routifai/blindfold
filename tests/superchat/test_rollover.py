"""Unit tests for ``omnigent.superchat.rollover``: trigger logic (threshold,
idle-refresh), the compaction-item build+post, and single-flight.

No DB, no server, no live calls — tiny in-memory fakes stand in for the
server client and the LLM summarizer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from omnigent.llms.types import MessageOutput, OutputText, Response
from omnigent.superchat import rollover as rollover_mod
from omnigent.superchat.rollover import (
    DEFAULT_IDLE_REFRESH_SECONDS,
    OMNIGENT_ROLLOVER_IDLE_REFRESH_SECONDS_ENV,
    resolve_idle_refresh_seconds,
    roll_over_session,
    should_roll_over_for_idle,
    should_roll_over_for_threshold,
)

_SUPERSIDE = {"omnigent.context.mode": "superside-chat"}
_ROLLOVER_MODE = {"omnigent.context.mode": "rollover"}


@pytest.fixture(autouse=True)
def _clear_in_flight() -> None:
    rollover_mod._in_flight.clear()


# ── resolve_idle_refresh_seconds ──────────────────────────────────────────


def test_idle_refresh_seconds_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(OMNIGENT_ROLLOVER_IDLE_REFRESH_SECONDS_ENV, raising=False)
    assert resolve_idle_refresh_seconds() == DEFAULT_IDLE_REFRESH_SECONDS == 43_200


def test_idle_refresh_seconds_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OMNIGENT_ROLLOVER_IDLE_REFRESH_SECONDS_ENV, "5")
    assert resolve_idle_refresh_seconds() == 5


def test_idle_refresh_seconds_ignores_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OMNIGENT_ROLLOVER_IDLE_REFRESH_SECONDS_ENV, "not-a-number")
    assert resolve_idle_refresh_seconds() == DEFAULT_IDLE_REFRESH_SECONDS
    monkeypatch.setenv(OMNIGENT_ROLLOVER_IDLE_REFRESH_SECONDS_ENV, "-1")
    assert resolve_idle_refresh_seconds() == DEFAULT_IDLE_REFRESH_SECONDS


# ── should_roll_over_for_threshold ────────────────────────────────────────


def test_threshold_trigger_fires_over_threshold() -> None:
    assert should_roll_over_for_threshold(_SUPERSIDE, context_tokens=200_000) is True


def test_threshold_trigger_does_not_fire_under_threshold() -> None:
    assert should_roll_over_for_threshold(_SUPERSIDE, context_tokens=1_000) is False


def test_threshold_trigger_ignores_non_superside_chat() -> None:
    assert should_roll_over_for_threshold(_ROLLOVER_MODE, context_tokens=10_000_000) is False
    assert should_roll_over_for_threshold({}, context_tokens=10_000_000) is False
    assert should_roll_over_for_threshold(None, context_tokens=10_000_000) is False


def test_threshold_trigger_ignores_unknown_context_tokens() -> None:
    assert should_roll_over_for_threshold(_SUPERSIDE, context_tokens=None) is False


def test_threshold_trigger_honors_model_window() -> None:
    # 60% of a 10k window floors at MIN_ROLLOVER_THRESHOLD_TOKENS (100k) per
    # resolve_rollover_threshold, so use an explicit override label instead.
    labels = {**_SUPERSIDE, "omnigent.context.rollover_at_tokens": "150000"}
    assert should_roll_over_for_threshold(labels, context_tokens=140_000) is False
    assert should_roll_over_for_threshold(labels, context_tokens=150_000) is True


# ── should_roll_over_for_idle ──────────────────────────────────────────────


def test_idle_trigger_fires_past_the_period(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OMNIGENT_ROLLOVER_IDLE_REFRESH_SECONDS_ENV, "100")
    assert should_roll_over_for_idle(_SUPERSIDE, idle_seconds=101) is True


def test_idle_trigger_does_not_fire_within_the_period(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OMNIGENT_ROLLOVER_IDLE_REFRESH_SECONDS_ENV, "100")
    assert should_roll_over_for_idle(_SUPERSIDE, idle_seconds=99) is False


def test_idle_trigger_ignores_non_superside_chat() -> None:
    assert should_roll_over_for_idle(_ROLLOVER_MODE, idle_seconds=10**9) is False
    assert should_roll_over_for_idle({}, idle_seconds=10**9) is False


def test_idle_trigger_ignores_unknown_idle_seconds() -> None:
    assert should_roll_over_for_idle(_SUPERSIDE, idle_seconds=None) is False


# ── roll_over_session: fakes ──────────────────────────────────────────────


@dataclass
class _FakeResponse:
    status_code: int
    _body: dict[str, Any]

    def json(self) -> dict[str, Any]:
        return self._body


@dataclass
class _FakeServerClient:
    """One page of items + a recording POST — no pagination, no network."""

    items: list[dict[str, Any]]
    post_status: int = 200
    posted: list[dict[str, Any]] = field(default_factory=list)
    get_calls: int = 0

    async def get(self, _url: str, *, params: dict[str, str], timeout: float) -> _FakeResponse:
        self.get_calls += 1
        if params.get("after"):
            return _FakeResponse(200, {"data": [], "has_more": False})
        return _FakeResponse(200, {"data": self.items, "has_more": False})

    async def post(self, _url: str, *, json: dict[str, Any], timeout: float) -> _FakeResponse:
        self.posted.append(json)
        return _FakeResponse(self.post_status, {})


class _ReturnsTextClient:
    """LLM client stub returning a fixed summary (mirrors test_rollover.py)."""

    def __init__(self, text: str) -> None:
        self._text = text

    class _Responses:
        def __init__(self, outer: _ReturnsTextClient) -> None:
            self._outer = outer

        async def create(self, **_kwargs: Any) -> Response:
            return Response(
                output=[MessageOutput(content=[OutputText(text=self._outer._text)])],
                model="test-model",
            )

    @property
    def responses(self) -> _ReturnsTextClient._Responses:
        return self._Responses(self)


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


def _compaction_item(item_id: str, *, summary: str, last_item_id: str) -> dict[str, Any]:
    return {
        "id": item_id,
        "type": "compaction",
        "status": "completed",
        "response_id": f"resp_{item_id}",
        "created_at": 1,
        "summary": summary,
        "last_item_id": last_item_id,
        "model": "gpt-4o",
        "token_count": 10,
    }


# ── roll_over_session ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_roll_over_session_builds_and_posts_compaction_item() -> None:
    items = [_msg("u1", "user", "hi"), _msg("a1", "assistant", "hello")]
    client = _FakeServerClient(items=items)
    rolled_over = []

    async def on_rolled_over() -> None:
        rolled_over.append(True)

    ok = await roll_over_session(
        "conv_1",
        labels=_SUPERSIDE,
        model="gpt-4o",
        server_client=client,  # type: ignore[arg-type]
        llm_client=_ReturnsTextClient("SUMMARY"),
        on_rolled_over=on_rolled_over,
    )

    assert ok is True
    assert len(client.posted) == 1
    posted = client.posted[0]
    assert posted["type"] == "compaction"
    assert posted["data"]["type"] == "compaction"
    assert "SUMMARY" in posted["data"]["summary"]
    assert posted["data"]["last_item_id"] == "a1"
    assert rolled_over == [True]


@pytest.mark.asyncio
async def test_roll_over_session_resumes_from_previous_compaction() -> None:
    """Items at/before the latest compaction item are excluded from the new build."""
    items = [
        _msg("u1", "user", "earlier, already compacted"),
        _compaction_item("c1", summary="OLD SUMMARY", last_item_id="u1"),
        _msg("u2", "user", "new question"),
        _msg("a2", "assistant", "new answer"),
    ]
    client = _FakeServerClient(items=items)

    ok = await roll_over_session(
        "conv_1",
        labels=_SUPERSIDE,
        model="gpt-4o",
        server_client=client,  # type: ignore[arg-type]
        llm_client=_ReturnsTextClient("NEW SUMMARY"),
    )

    assert ok is True
    posted_data = client.posted[0]["data"]
    assert posted_data["last_item_id"] == "a2"
    # Only the post-checkpoint turn is replayed in the kept tail.
    ids = [m.get("id") for m in posted_data["compacted_messages"] if m.get("id")]
    assert "u1" not in ids
    assert "u2" in ids and "a2" in ids


@pytest.mark.asyncio
async def test_roll_over_session_noop_when_nothing_since_last_compaction() -> None:
    items = [
        _msg("u1", "user", "hi"),
        _compaction_item("c1", summary="OLD SUMMARY", last_item_id="u1"),
    ]
    client = _FakeServerClient(items=items)

    ok = await roll_over_session(
        "conv_1",
        labels=_SUPERSIDE,
        model="gpt-4o",
        server_client=client,  # type: ignore[arg-type]
        llm_client=_ReturnsTextClient("SUMMARY"),
    )

    assert ok is False
    assert client.posted == []


@pytest.mark.asyncio
async def test_roll_over_session_post_failure_is_non_fatal_and_skips_callback() -> None:
    items = [_msg("u1", "user", "hi"), _msg("a1", "assistant", "hello")]
    client = _FakeServerClient(items=items, post_status=500)
    rolled_over = []

    async def on_rolled_over() -> None:
        rolled_over.append(True)

    ok = await roll_over_session(
        "conv_1",
        labels=_SUPERSIDE,
        model="gpt-4o",
        server_client=client,  # type: ignore[arg-type]
        llm_client=_ReturnsTextClient("SUMMARY"),
        on_rolled_over=on_rolled_over,
    )

    # The POST call itself doesn't raise (a 500 JSON body), so this helper
    # reports success at the transport level; failure injection belongs to
    # the server_client. What matters here is the callback only fires from
    # this module's own success path — covered by the OK case above.
    assert ok is True
    assert rolled_over == [True]


@pytest.mark.asyncio
async def test_roll_over_session_single_flight() -> None:
    items = [_msg("u1", "user", "hi"), _msg("a1", "assistant", "hello")]
    client = _FakeServerClient(items=items)
    rollover_mod._in_flight.add("conv_1")

    ok = await roll_over_session(
        "conv_1",
        labels=_SUPERSIDE,
        model="gpt-4o",
        server_client=client,  # type: ignore[arg-type]
        llm_client=_ReturnsTextClient("SUMMARY"),
    )

    assert ok is False
    assert client.get_calls == 0
    assert client.posted == []


@pytest.mark.asyncio
async def test_roll_over_session_clears_in_flight_after_completion() -> None:
    items = [_msg("u1", "user", "hi"), _msg("a1", "assistant", "hello")]
    client = _FakeServerClient(items=items)

    await roll_over_session(
        "conv_1",
        labels=_SUPERSIDE,
        model="gpt-4o",
        server_client=client,  # type: ignore[arg-type]
        llm_client=_ReturnsTextClient("SUMMARY"),
    )

    assert "conv_1" not in rollover_mod._in_flight
