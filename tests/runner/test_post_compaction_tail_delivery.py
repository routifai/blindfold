"""Tests for the once-per-compaction verbatim tail (``_apply_post_compaction_tail``).

Claude Code and Codex compact themselves and keep less than Pi's own
rollover tail (``rollover/README.md``). ``_apply_post_compaction_tail``
(``omnigent/runner/app.py``) prepends a verbatim recent-turns block, built by
``omnigent.context.rollover.build_post_compaction_tail``, to the first
message delivered to the CLI after a compaction — only for claude-native and
codex-native rollover sessions, and only once per compaction.

Dispatched with ``?stream=true`` so the runner forwards this turn's own
content blocks directly (the direct-stream path), rather than the
background path's full reconstructed history — simpler to assert on, and
the hook (``proxy_stream``) is identical either way.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.context.labels import CONTEXT_MODE_LABEL, ROLLOVER_MODE_VALUE
from omnigent.context.rollover import (
    _POST_COMPACTION_TAIL_FOOTER,
    _POST_COMPACTION_TAIL_HEADER,
)
from omnigent.runner import create_runner_app
from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient, _sse
from tests.runner.helpers import NullServerClient

_ROLLOVER_LABELS = {CONTEXT_MODE_LABEL: ROLLOVER_MODE_VALUE}


def _message_item(item_id: str, role: str, text: str) -> dict[str, Any]:
    block = "input_text" if role == "user" else "output_text"
    return {
        "id": item_id,
        "type": "message",
        "role": role,
        "content": [{"type": block, "text": text}],
    }


def _compaction_item(item_id: str = "comp_1") -> dict[str, Any]:
    return {"id": item_id, "type": "compaction", "summary": "…", "last_item_id": "u1"}


class _ScriptedItemsServerClient(NullServerClient):
    """Answers ``/labels`` and ``/items`` (desc) with scripted data.

    Everything else (comment-relay POSTs, etc.) falls through to
    :class:`NullServerClient`.
    """

    def __init__(
        self, *, labels: dict[str, str], items_newest_first: list[dict[str, Any]]
    ) -> None:
        self._labels = labels
        self._items_newest_first = items_newest_first

    async def get(self, url: str, **kwargs: Any) -> NullServerClient._Response:
        labels, items = self._labels, self._items_newest_first
        if url.endswith("/labels"):

            class _LabelsResponse(NullServerClient._Response):
                def json(self) -> dict[str, Any]:
                    return {"labels": labels}

            return _LabelsResponse()
        if url.endswith("/items"):

            class _ItemsResponse(NullServerClient._Response):
                def json(self) -> dict[str, Any]:
                    return {"data": items}

            return _ItemsResponse()
        return await super().get(url, **kwargs)


def _sse_frames() -> list[str]:
    return [
        _sse({"type": "response.created", "response": {"id": "resp_1"}}),
        _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
    ]


def _build_app(server_client: NullServerClient) -> tuple[Any, _ScriptedHarnessClient]:
    harness_client = _ScriptedHarnessClient(_sse_frames())
    app = create_runner_app(
        process_manager=_FakeProcessManager(harness_client),  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )
    return app, harness_client


async def _post(client: Any, session_id: str, harness: str, text: str) -> None:
    resp = await client.post(
        f"/v1/sessions/{session_id}/events?stream=true",
        json={
            "type": "message",
            "role": "user",
            "model": "test-agent",
            "content": [{"type": "input_text", "text": text}],
            "harness": harness,
        },
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_claude_native_rollover_gets_the_tail_once() -> None:
    server_client = _ScriptedItemsServerClient(
        labels=_ROLLOVER_LABELS,
        items_newest_first=[
            _compaction_item(),
            _message_item("a1", "assistant", "answer before compaction"),
            _message_item("u1", "user", "question before compaction"),
        ],
    )
    app, harness_client = _build_app(server_client)

    async with _runner_client(app) as client:
        await _post(client, "conv_a", "claude-native", "first message after compaction")
        await _post(client, "conv_a", "claude-native", "second message, same compaction")

    assert len(harness_client.posted_bodies) == 2
    first_text = harness_client.posted_bodies[0]["content"][0]["text"]
    assert _POST_COMPACTION_TAIL_HEADER in first_text
    assert _POST_COMPACTION_TAIL_FOOTER in first_text
    assert "question before compaction" in first_text
    assert "answer before compaction" in first_text
    # The user's own text still rides unchanged, after the tail block.
    assert first_text.endswith("first message after compaction")

    # Once per compaction: a second message, with the compaction still the
    # latest one, gets no tail block at all.
    second_text = harness_client.posted_bodies[1]["content"][0]["text"]
    assert _POST_COMPACTION_TAIL_HEADER not in second_text
    assert second_text == "second message, same compaction"


@pytest.mark.asyncio
async def test_second_compaction_re_arms_the_tail() -> None:
    """A later, different compaction gets its own tail delivered once more."""
    server_client = _ScriptedItemsServerClient(
        labels=_ROLLOVER_LABELS,
        items_newest_first=[
            _compaction_item("comp_2"),
            _message_item("a2", "assistant", "answer two"),
            _message_item("u2", "user", "question two"),
        ],
    )
    app, harness_client = _build_app(server_client)

    async with _runner_client(app) as client:
        await _post(client, "conv_a2", "codex-native", "after second compaction")

    text = harness_client.posted_bodies[0]["content"][0]["text"]
    assert _POST_COMPACTION_TAIL_HEADER in text
    assert "question two" in text


@pytest.mark.asyncio
async def test_non_rollover_session_is_untouched() -> None:
    """No ``omnigent.context.mode=rollover`` label → content passes through."""
    server_client = _ScriptedItemsServerClient(
        labels={},
        items_newest_first=[
            _compaction_item(),
            _message_item("a1", "assistant", "answer before compaction"),
            _message_item("u1", "user", "question before compaction"),
        ],
    )
    app, harness_client = _build_app(server_client)

    async with _runner_client(app) as client:
        await _post(client, "conv_b", "claude-native", "hello")

    assert harness_client.posted_bodies[0]["content"] == [{"type": "input_text", "text": "hello"}]


@pytest.mark.asyncio
async def test_non_native_harness_is_untouched() -> None:
    """Only claude-native/codex-native are gated; other harnesses pass through."""
    server_client = _ScriptedItemsServerClient(
        labels=_ROLLOVER_LABELS,
        items_newest_first=[
            _compaction_item(),
            _message_item("a1", "assistant", "answer before compaction"),
            _message_item("u1", "user", "question before compaction"),
        ],
    )
    app, harness_client = _build_app(server_client)

    async with _runner_client(app) as client:
        await _post(client, "conv_c", "openai-agents", "hello")

    assert harness_client.posted_bodies[0]["content"] == [{"type": "input_text", "text": "hello"}]


@pytest.mark.asyncio
async def test_no_compaction_yet_is_untouched() -> None:
    server_client = _ScriptedItemsServerClient(labels=_ROLLOVER_LABELS, items_newest_first=[])
    app, harness_client = _build_app(server_client)

    async with _runner_client(app) as client:
        await _post(client, "conv_d", "codex-native", "hello")

    assert harness_client.posted_bodies[0]["content"] == [{"type": "input_text", "text": "hello"}]
