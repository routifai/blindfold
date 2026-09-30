"""Unit tests for claude-native's warm-if-valid blindfold lifecycle.

Companion to ``tests/test_blindfold_harness_turn.py`` (the fresh, per-turn
one-shot path, unaffected by this module). Exercises
``omnigent.harnesses.claude_native.blindfold_warm`` end to end through the
same public entrypoint, ``maybe_run_blindfold_turn``, against:

- a small stateful mock Omnigent server (``_FakeServer``) that keeps a real
  session record and runs the REAL ``omnigent.context_assembly.assembler``
  over it, so ``max_messages`` windowing behaves exactly as production does
  (this is what makes the "sliding window invalidates reuse" case
  meaningful rather than asserted against a fake);
- a small stand-in ``claude`` CLI (a persistent Python script reading
  stream-json lines from stdin in a loop, like the real ``--input-format
  stream-json`` process this module drives) in place of the real binary.
"""

from __future__ import annotations

import json
import stat
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.context_assembly.assembler import assemble
from omnigent.context_assembly.blindfold import write_server_connection
from omnigent.context_assembly.models import AssembleRequest
from omnigent.harnesses.claude_native import blindfold as claude_blindfold
from omnigent.harnesses.claude_native import blindfold_warm


def _write_fake_streaming_cli(
    tmp_path: Path, name: str, *, spawn_log: Path, die_on_turn: int | None = None
) -> str:
    """A persistent stand-in for ``claude -p --input-format stream-json``.

    Records one line to *spawn_log* on startup (so a test can count how many
    processes actually got spawned), then loops reading stream-json user
    messages from stdin, echoing each back as one assistant `text` event
    plus a `result` event -- the same two events
    :func:`parse_claude_stream_json` reads a turn's answer from.

    :param die_on_turn: When set, the process exits the instant it reads its
        *die_on_turn*-th message -- before writing anything back -- to
        exercise a crash mid-turn (the write into a still-alive process
        succeeds; the read of its reply then hits EOF).
    """
    path = tmp_path / name
    body = f"""#!{sys.executable}
import json, sys

with open({str(spawn_log)!r}, "a") as f:
    f.write("spawned\\n")

count = 0
for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    try:
        req = json.loads(raw)
    except ValueError:
        continue
    if req.get("type") != "user":
        continue
    count += 1
    if {die_on_turn!r} is not None and count == {die_on_turn!r}:
        sys.exit(0)
    text = req["message"]["content"]
    block = {{"type": "text", "text": text}}
    assistant_event = {{"type": "assistant", "message": {{"content": [block]}}}}
    result_event = {{"type": "result", "subtype": "success", "result": text}}
    print(json.dumps(assistant_event))
    print(json.dumps(result_event))
    sys.stdout.flush()
"""
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


class _FakeServer:
    """A minimal, stateful stand-in for the Omnigent server's session/context
    API, backed by a real in-memory record and the real assembler."""

    def __init__(
        self, *, labels: dict[str, str], model: str, agent_instructions: str = "Rules."
    ) -> None:
        self.labels = labels
        self.model = model
        self.agent_instructions = agent_instructions
        self.items: list[dict[str, Any]] = []
        self._next_id = 0

    def _new_id(self) -> str:
        self._next_id += 1
        return f"item_{self._next_id}"

    def seed_message(self, *, role: str, text: str) -> str:
        """Pre-populate the record (simulating turns before this test cares about)."""
        item_id = self._new_id()
        self.items.append(
            {
                "id": item_id,
                "type": "message",
                "role": role,
                "content": [
                    {"type": "input_text" if role == "user" else "output_text", "text": text}
                ],
            }
        )
        return item_id

    def append_assistant_response(self, *, turn_id: str, text: str) -> str:
        """What the (untested-here) Session layer does after a turn: persist
        the final answer. Callers must do this themselves between turns, same
        as production -- see post_oneshot_items' docstring on why blindfold's
        own code never posts this item itself."""
        item_id = self._new_id()
        self.items.append(
            {
                "id": item_id,
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
                "response_id": turn_id,
            }
        )
        return item_id

    def route(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/items"):
            limit = int(request.url.params.get("limit", "50"))
            tail = self.items[-limit:]
            return httpx.Response(
                200, json={"data": list(reversed(tail)), "has_more": len(tail) < len(self.items)}
            )
        if request.method == "POST" and path.endswith("/context/observe"):
            return httpx.Response(204)
        if request.method == "POST" and path.endswith("/context"):
            body = json.loads(request.content)
            request_model = AssembleRequest.model_validate(body)
            response = assemble(
                request_model,
                items_provider=lambda: self.items,
                agent_instructions=self.agent_instructions,
            )
            return httpx.Response(200, json=response.model_dump())
        if request.method == "POST" and path.endswith("/events"):
            body = json.loads(request.content)
            if body.get("type") == "external_conversation_item":
                data = body["data"]
                item_id = self._new_id()
                item = {"id": item_id, "type": data["item_type"], **data["item_data"]}
                if data.get("response_id") is not None:
                    item["response_id"] = data["response_id"]
                self.items.append(item)
                return httpx.Response(200, json={"id": item_id})
            return httpx.Response(200, json={})
        if request.method == "GET":  # GET /v1/sessions/{id}
            return httpx.Response(
                200, json={"labels": self.labels, "owner": "local", "model": self.model}
            )
        return httpx.Response(404)


_RealAsyncClient = httpx.AsyncClient


def _patch_async_client(monkeypatch: pytest.MonkeyPatch, server: _FakeServer) -> None:
    transport = httpx.MockTransport(server.route)

    def _factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return _RealAsyncClient(*args, **{**kwargs, "transport": transport})

    monkeypatch.setattr(httpx, "AsyncClient", _factory)


async def _run_turn(
    bridge_dir: Path, *, text: str, command: str
) -> claude_blindfold.BlindfoldTurnResult:
    return await claude_blindfold.maybe_run_blindfold_turn(
        bridge_dir=bridge_dir, session_id="conv_warm", new_message_text=text, command=command
    )


def _warm_labels(**extra: str) -> dict[str, str]:
    return {
        "omnigent.blindfold": "true",
        "omnigent.context.lifecycle": "warm_if_valid",
        "omnigent.context.max_messages": "50",
        **extra,
    }


@pytest.fixture(autouse=True)
async def _clean_registry() -> Any:
    # The registry is a module-level singleton (mirrors production: one per
    # process). Each test starts with none of its own sessions in it, and
    # cleans up after itself so warm processes never leak across tests.
    yield
    await blindfold_warm.discard_warm_session("conv_warm", reason="test_teardown")


class TestWarmReuse:
    async def test_reuses_process_when_everything_matches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_server_connection(
            tmp_path, base_url="http://server", headers={}, labels={"omnigent.blindfold": "true"}
        )
        server = _FakeServer(labels=_warm_labels(), model="claude-haiku-4-5-20251001")
        _patch_async_client(monkeypatch, server)
        spawn_log = tmp_path / "spawns.log"
        fake_claude = _write_fake_streaming_cli(tmp_path, "fake_claude", spawn_log=spawn_log)

        turn1 = await _run_turn(tmp_path, text="hello", command=fake_claude)
        assert turn1.handled is True
        assert turn1.error is None
        assert turn1.warm_reused is False
        assert turn1.warm_reason == "no_warm_process"
        server.append_assistant_response(turn_id="t1", text=turn1.response_text or "")

        turn2 = await _run_turn(tmp_path, text="again", command=fake_claude)
        assert turn2.error is None
        assert turn2.warm_reused is True
        assert turn2.warm_reason == "valid"
        server.append_assistant_response(turn_id="t2", text=turn2.response_text or "")

        turn3 = await _run_turn(tmp_path, text="third", command=fake_claude)
        assert turn3.warm_reused is True

        assert spawn_log.read_text().count("spawned") == 1

    async def test_discards_on_system_or_memory_change(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_server_connection(
            tmp_path, base_url="http://server", headers={}, labels={"omnigent.blindfold": "true"}
        )
        server = _FakeServer(labels=_warm_labels(), model="claude-haiku-4-5-20251001")
        _patch_async_client(monkeypatch, server)
        spawn_log = tmp_path / "spawns.log"
        fake_claude = _write_fake_streaming_cli(tmp_path, "fake_claude", spawn_log=spawn_log)

        turn1 = await _run_turn(tmp_path, text="hello", command=fake_claude)
        assert turn1.warm_reused is False
        server.append_assistant_response(turn_id="t1", text=turn1.response_text or "")

        # A memory fixture starting mid-session changes the rendered system
        # text (contract §3's <long_term_memory> block) -- must not reuse.
        server.labels = _warm_labels(**{"omnigent.context.memory_fixture": "The codeword is X"})
        turn2 = await _run_turn(tmp_path, text="again", command=fake_claude)
        assert turn2.error is None
        assert turn2.warm_reused is False
        assert turn2.warm_reason == "system_or_memory_changed"
        assert spawn_log.read_text().count("spawned") == 2

    async def test_discards_on_model_change(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_server_connection(
            tmp_path, base_url="http://server", headers={}, labels={"omnigent.blindfold": "true"}
        )
        server = _FakeServer(labels=_warm_labels(), model="claude-haiku-4-5-20251001")
        _patch_async_client(monkeypatch, server)
        spawn_log = tmp_path / "spawns.log"
        fake_claude = _write_fake_streaming_cli(tmp_path, "fake_claude", spawn_log=spawn_log)

        turn1 = await _run_turn(tmp_path, text="hello", command=fake_claude)
        assert turn1.warm_reused is False
        server.append_assistant_response(turn_id="t1", text=turn1.response_text or "")

        server.model = "claude-opus-4-6"
        turn2 = await _run_turn(tmp_path, text="again", command=fake_claude)
        assert turn2.error is None
        assert turn2.warm_reused is False
        assert turn2.warm_reason == "model_changed"

    async def test_discards_when_the_window_slides(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A small max_messages means the process 'remembers' more than the
        assembler now selects -- must invalidate rather than leak the extra
        history. This is claude-native's version of the E2E window_edge
        proof, done at the unit level with a deterministic mock server."""
        write_server_connection(
            tmp_path, base_url="http://server", headers={}, labels={"omnigent.blindfold": "true"}
        )
        server = _FakeServer(
            labels=_warm_labels(**{"omnigent.context.max_messages": "1"}),
            model="claude-haiku-4-5-20251001",
        )
        _patch_async_client(monkeypatch, server)
        spawn_log = tmp_path / "spawns.log"
        fake_claude = _write_fake_streaming_cli(tmp_path, "fake_claude", spawn_log=spawn_log)

        turn1 = await _run_turn(tmp_path, text="my codeword is PAPAYA-42", command=fake_claude)
        assert turn1.warm_reused is False  # first turn always cold-starts
        server.append_assistant_response(turn_id="t1", text=turn1.response_text or "")

        turn2 = await _run_turn(tmp_path, text="what is my codeword?", command=fake_claude)
        assert turn2.error is None
        assert turn2.warm_reused is False
        assert turn2.warm_reason in {"history_mismatch", "history_untraceable"}
        # A brand-new process was cold-started for turn 2 -- proof this
        # never silently answered from the process's own over-long memory.
        assert spawn_log.read_text().count("spawned") == 2

    async def test_discards_on_process_death(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_server_connection(
            tmp_path, base_url="http://server", headers={}, labels={"omnigent.blindfold": "true"}
        )
        server = _FakeServer(labels=_warm_labels(), model="claude-haiku-4-5-20251001")
        _patch_async_client(monkeypatch, server)
        spawn_log = tmp_path / "spawns.log"
        # Alive (and valid to reuse) through turn 1; exits the instant it
        # reads turn 2's message, without responding -- a crash mid-turn.
        fake_claude = _write_fake_streaming_cli(
            tmp_path, "fake_claude", spawn_log=spawn_log, die_on_turn=2
        )

        turn1 = await _run_turn(tmp_path, text="hello", command=fake_claude)
        assert turn1.error is None
        server.append_assistant_response(turn_id="t1", text=turn1.response_text or "")

        turn2 = await _run_turn(tmp_path, text="again", command=fake_claude)
        assert turn2.handled is True
        assert turn2.error is not None
        assert turn2.response_text is None

        # The dead process must be gone, not left registered for a third
        # turn to trip over again.
        assert await blindfold_warm._REGISTRY.get("conv_warm") is None

    async def test_fresh_lifecycle_is_unaffected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No omnigent.context.lifecycle label -> today's one-shot-per-turn
        path, unchanged; warm_reused/warm_reason stay at their off defaults."""
        write_server_connection(
            tmp_path, base_url="http://server", headers={}, labels={"omnigent.blindfold": "true"}
        )
        server = _FakeServer(
            labels={"omnigent.blindfold": "true"}, model="claude-haiku-4-5-20251001"
        )
        _patch_async_client(monkeypatch, server)
        # The fresh-path fake CLI shape (from test_blindfold_harness_turn.py):
        # non-streaming, prints its whole reply immediately.
        path = tmp_path / "fake_claude_fresh"
        path.write_text(
            f'#!{sys.executable}\nimport json, sys\nargv_text = " ".join(sys.argv[1:])\n'
            "print(json.dumps({'type': 'assistant', "
            "'message': {'content': [{'type': 'text', 'text': argv_text}]}}))\n"
            "print(json.dumps({'type': 'result', 'subtype': 'success', 'result': argv_text}))\n"
        )
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

        result = await _run_turn(tmp_path, text="hello", command=str(path))
        assert result.handled is True
        assert result.error is None
        assert result.warm_reused is False
        assert result.warm_reason is None
        assert await blindfold_warm._REGISTRY.get("conv_warm") is None


class TestValidityCheckUnit:
    """Direct unit coverage of _check_validity's reasons, without spawning
    any process -- documents the exact rule (contract v0.2's reserved
    lifecycle row, spelled out in this module's docstring)."""

    def _process(self, **overrides: Any) -> blindfold_warm.WarmProcess:
        class _FakeProc:
            returncode = None

        # _check_validity never touches stderr_task/config_dir; plain
        # placeholders keep these as synchronous, loop-free unit tests.
        defaults: dict[str, Any] = {
            "session_id": "s",
            "model": "m",
            "system_text": "sys",
            "seen_item_ids": ["a", "b"],
            "proc": _FakeProc(),
            "config_dir": Path("/tmp/x"),
            "stderr_task": None,
        }
        defaults.update(overrides)
        return blindfold_warm.WarmProcess(**defaults)

    def test_valid_when_everything_matches_and_extends_seen_ids(self) -> None:
        # seen_item_ids=["a","b"]: the process was fed message "b" last
        # turn. Since then the record grew by "c" (that turn's own
        # response) and now "d" (this turn's brand-new message, excluded
        # from prior_ids per contract). Reuse is valid, and "c" -- ground
        # truth from the record, never tracked locally -- gets folded in.
        process = self._process(seen_item_ids=["a", "b"])
        reason = blindfold_warm._check_validity(
            process,
            system_text="sys",
            model="m",
            prior_ids=["a", "b", "c"],
            recent_ids_ascending=["a", "b", "c", "d"],
            new_item_id="d",
        )
        assert reason == "valid"
        assert process.seen_item_ids == ["a", "b", "c"]

    def test_process_dead(self) -> None:
        process = self._process()
        process.proc.returncode = 1
        reason = blindfold_warm._check_validity(
            process,
            system_text="sys",
            model="m",
            prior_ids=["a", "b"],
            recent_ids_ascending=["a", "b"],
            new_item_id="b",
        )
        assert reason == "process_dead"

    def test_model_changed(self) -> None:
        process = self._process(model="old")
        reason = blindfold_warm._check_validity(
            process,
            system_text="sys",
            model="new",
            prior_ids=["a", "b"],
            recent_ids_ascending=["a", "b"],
            new_item_id="b",
        )
        assert reason == "model_changed"

    def test_system_or_memory_changed(self) -> None:
        process = self._process(system_text="old")
        reason = blindfold_warm._check_validity(
            process,
            system_text="new",
            model="m",
            prior_ids=["a", "b"],
            recent_ids_ascending=["a", "b"],
            new_item_id="b",
        )
        assert reason == "system_or_memory_changed"

    def test_history_mismatch_when_window_dropped_the_front(self) -> None:
        # The process was fed "b" last turn (after having already seen
        # "a"). This turn's new message is "c"; the assembler's window
        # (small max_messages) now selects only ["b"] as prior history --
        # "a" fell off the front. The process still remembers "a" and can't
        # un-remember it, so this must invalidate.
        process = self._process(seen_item_ids=["a", "b"])
        reason = blindfold_warm._check_validity(
            process,
            system_text="sys",
            model="m",
            prior_ids=["b"],
            recent_ids_ascending=["a", "b", "c"],
            new_item_id="c",
        )
        assert reason == "history_mismatch"

    def test_history_untraceable_when_anchor_not_in_recent_fetch(self) -> None:
        process = self._process(seen_item_ids=["a", "b"])
        reason = blindfold_warm._check_validity(
            process,
            system_text="sys",
            model="m",
            prior_ids=[],
            recent_ids_ascending=["c"],
            new_item_id="c",
        )
        assert reason == "history_untraceable"


def test_is_warm_lifecycle() -> None:
    assert (
        blindfold_warm.is_warm_lifecycle({"omnigent.context.lifecycle": "warm_if_valid"}) is True
    )
    assert blindfold_warm.is_warm_lifecycle({}) is False
    assert blindfold_warm.is_warm_lifecycle(None) is False
    assert blindfold_warm.is_warm_lifecycle({"omnigent.context.lifecycle": "fresh"}) is False


async def test_discard_warm_session_is_a_safe_noop_when_nothing_registered() -> None:
    await blindfold_warm.discard_warm_session(str(uuid.uuid4()), reason="never_existed")
