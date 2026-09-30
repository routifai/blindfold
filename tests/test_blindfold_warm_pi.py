"""Unit tests for pi-native's warm ``pi --mode rpc`` blindfold lifecycle.

Two layers:

- ``TestRunWarmOrCold`` calls :func:`run_warm_or_cold` directly with a fake
  RPC CLI (a small python script speaking pi's RPC JSONL protocol on
  stdin/stdout) and a fake in-memory session record, giving each scenario
  (reuse, discard-on-change, discard-on-window-slide, discard-on-crash)
  precise control over what "the assembler selected this turn" is, without
  reimplementing the real assembler.
- ``TestGuardedWiring`` goes through the real entry point
  (``pi_blindfold.maybe_run_blindfold_turn``) end to end, against the real
  ``select_history_refs`` assembler logic, to prove the
  ``omnigent.context.lifecycle`` label actually gates which path runs and
  that turn 2 of a real multi-turn session reuses the warm process.

The fake RPC CLI's replies embed a per-process turn counter ("turn=1",
"turn=2", ...) that only advances within one subprocess's lifetime — the
simplest possible reuse signal: a fresh process always starts back at
"turn=1", so seeing "turn=2" proves the *same* subprocess handled both calls.
"""

from __future__ import annotations

import json
import stat
import sys
import textwrap
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.context_assembly.blindfold import write_server_connection
from omnigent.harnesses.pi_native import blindfold as pi_blindfold
from omnigent.harnesses.pi_native import blindfold_warm as warm


@pytest.fixture(autouse=True)
async def _reset_warm_registry():
    """Every test starts and ends with no leftover warm processes/sweeper."""
    await warm.reset_for_tests()
    yield
    await warm.reset_for_tests()


def _write_fake_pi_rpc(tmp_path: Path, name: str, *, crash_on_turn: int | None = None) -> str:
    """A stand-in ``pi --mode rpc`` process: reads JSONL prompt commands from
    stdin, replies with a ``message_end`` + ``agent_end`` pair whose text
    embeds a turn counter private to this process instance.

    :param crash_on_turn: If set, the process exits(1) without responding on
        that 1-indexed prompt, simulating a mid-turn crash.
    """
    path = tmp_path / name
    body = textwrap.dedent(f"""\
        #!{sys.executable}
        import json, sys

        def emit(obj):
            print(json.dumps(obj), flush=True)

        turn = 0
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                cmd = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if cmd.get("type") != "prompt":
                continue
            turn += 1
            if turn == {crash_on_turn!r}:
                sys.exit(1)
            emit({{"type": "response", "command": "prompt", "success": True}})
            text = f"turn={{turn}} msg={{cmd.get('message', '')}}"
            emit({{
                "type": "message_end",
                "message": {{"role": "assistant", "content": [{{"type": "text", "text": text}}]}},
            }})
            emit({{"type": "agent_end", "messages": []}})
        """)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


class _FakeRecord:
    """A minimal in-memory stand-in for a session's Omnigent record — just
    enough for ``run_warm_or_cold``'s own network calls (``GET .../items``,
    ``POST .../events``), not the full assemble contract.
    """

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self._next_id = 1

    def add_user_message(self, text: str) -> str:
        item_id = f"item_{self._next_id}"
        self._next_id += 1
        self.items.append(
            {
                "id": item_id,
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            }
        )
        return item_id

    def add_assistant_message(self, text: str) -> str:
        item_id = f"item_{self._next_id}"
        self._next_id += 1
        self.items.append(
            {
                "id": item_id,
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        )
        return item_id

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/events"):
            body = json.loads(request.content)
            if body.get("type") != "external_conversation_item":
                # e.g. external_session_status, posted by the outer
                # maybe_run_blindfold_turn caller — not this record's concern.
                return httpx.Response(200, json={})
            data = body.get("data", {})
            item_id = f"item_{self._next_id}"
            self._next_id += 1
            record = {"id": item_id, "type": data["item_type"], **data.get("item_data", {})}
            if data.get("response_id") is not None:
                record["response_id"] = data["response_id"]
            self.items.append(record)
            return httpx.Response(200, json={"id": item_id})
        if path.endswith("/items"):
            params = request.url.params
            limit = int(params.get("limit", "300"))
            order = params.get("order", "asc")
            data = list(reversed(self.items)) if order == "desc" else list(self.items)
            return httpx.Response(200, json={"data": data[:limit], "has_more": False})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")


@pytest.fixture
def record() -> _FakeRecord:
    return _FakeRecord()


@pytest.fixture
def client(record: _FakeRecord) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="http://server", transport=httpx.MockTransport(record.handle)
    )


class TestRunWarmOrCold:
    """Direct tests of the validity rule and reuse/discard behavior."""

    async def test_reuses_the_process_when_everything_matches(
        self, tmp_path: Path, record: _FakeRecord, client: httpx.AsyncClient
    ) -> None:
        fake_pi = _write_fake_pi_rpc(tmp_path, "fake_pi_rpc")
        session_id = "conv_reuse"

        new_id_1 = record.add_user_message("hello")
        result1 = await warm.run_warm_or_cold(
            session_id=session_id,
            turn_id="t1",
            new_message_text="hello",
            system_text="Rules.",
            selected_items=[],
            model="anthropic/claude-haiku-4.5",
            command=fake_pi,
            client=client,
            workspace=tmp_path,
        )
        assert result1.error is None
        assert result1.warm_reused is False
        assert result1.reason == "no_warm_process"
        assert "turn=1" in (result1.response_text or "")
        record.add_assistant_message(result1.response_text or "")
        del new_id_1

        # Turn 2: the assembler would now select [user1, assistant1] as prior
        # history — exactly what the warm process has already seen.
        prior = [record.items[0], record.items[1]]
        record.add_user_message("again")
        result2 = await warm.run_warm_or_cold(
            session_id=session_id,
            turn_id="t2",
            new_message_text="again",
            system_text="Rules.",
            selected_items=prior,
            model="anthropic/claude-haiku-4.5",
            command=fake_pi,
            client=client,
            workspace=tmp_path,
        )
        assert result2.error is None
        assert result2.warm_reused is True
        assert result2.reason is None
        # "turn=2" (not "turn=1") proves the SAME subprocess handled this —
        # a fresh spawn would always start its own counter back at 1.
        assert "turn=2" in (result2.response_text or "")

    async def test_discards_on_system_prompt_change(
        self, tmp_path: Path, record: _FakeRecord, client: httpx.AsyncClient
    ) -> None:
        fake_pi = _write_fake_pi_rpc(tmp_path, "fake_pi_rpc")
        session_id = "conv_system_change"

        record.add_user_message("hello")
        result1 = await warm.run_warm_or_cold(
            session_id=session_id,
            turn_id="t1",
            new_message_text="hello",
            system_text="Rules v1.",
            selected_items=[],
            model="m",
            command=fake_pi,
            client=client,
            workspace=tmp_path,
        )
        record.add_assistant_message(result1.response_text or "")
        prior = [record.items[0], record.items[1]]
        record.add_user_message("again")

        result2 = await warm.run_warm_or_cold(
            session_id=session_id,
            turn_id="t2",
            new_message_text="again",
            system_text="Rules v2 (memory changed).",  # digest differs
            selected_items=prior,
            model="m",
            command=fake_pi,
            client=client,
            workspace=tmp_path,
        )
        assert result2.error is None
        assert result2.warm_reused is False
        assert result2.reason == "system_or_memory_changed"
        assert "turn=1" in (result2.response_text or "")  # a fresh process

    async def test_discards_on_model_change(
        self, tmp_path: Path, record: _FakeRecord, client: httpx.AsyncClient
    ) -> None:
        fake_pi = _write_fake_pi_rpc(tmp_path, "fake_pi_rpc")
        session_id = "conv_model_change"

        record.add_user_message("hello")
        result1 = await warm.run_warm_or_cold(
            session_id=session_id,
            turn_id="t1",
            new_message_text="hello",
            system_text="Rules.",
            selected_items=[],
            model="model-a",
            command=fake_pi,
            client=client,
            workspace=tmp_path,
        )
        record.add_assistant_message(result1.response_text or "")
        prior = [record.items[0], record.items[1]]
        record.add_user_message("again")

        result2 = await warm.run_warm_or_cold(
            session_id=session_id,
            turn_id="t2",
            new_message_text="again",
            system_text="Rules.",
            selected_items=prior,
            model="model-b",
            command=fake_pi,
            client=client,
            workspace=tmp_path,
        )
        assert result2.warm_reused is False
        assert result2.reason == "model_changed"

    async def test_discards_when_the_window_slides(
        self, tmp_path: Path, record: _FakeRecord, client: httpx.AsyncClient
    ) -> None:
        """A sliding max_messages window that drops an old message from the
        front must never be silently under-forgotten by warm reuse.
        """
        fake_pi = _write_fake_pi_rpc(tmp_path, "fake_pi_rpc")
        session_id = "conv_window_slide"

        record.add_user_message("hello")
        result1 = await warm.run_warm_or_cold(
            session_id=session_id,
            turn_id="t1",
            new_message_text="hello",
            system_text="Rules.",
            selected_items=[],
            model="m",
            command=fake_pi,
            client=client,
            workspace=tmp_path,
        )
        record.add_assistant_message(result1.response_text or "")
        record.add_user_message("second")
        second_prior = [record.items[0], record.items[1]]
        result2 = await warm.run_warm_or_cold(
            session_id=session_id,
            turn_id="t2",
            new_message_text="second",
            system_text="Rules.",
            selected_items=second_prior,
            model="m",
            command=fake_pi,
            client=client,
            workspace=tmp_path,
        )
        assert result2.warm_reused is True
        record.add_assistant_message(result2.response_text or "")

        # Turn 3: a smaller window now selects only [user2, assistant2],
        # dropping [user1, assistant1] from the front — the process has
        # already seen more than that, so this must NOT be reused.
        third_prior = [record.items[2], record.items[3]]
        record.add_user_message("third")
        result3 = await warm.run_warm_or_cold(
            session_id=session_id,
            turn_id="t3",
            new_message_text="third",
            system_text="Rules.",
            selected_items=third_prior,
            model="m",
            command=fake_pi,
            client=client,
            workspace=tmp_path,
        )
        assert result3.warm_reused is False
        assert result3.reason == "history_window_changed"
        assert "turn=1" in (result3.response_text or "")

    async def test_discards_on_process_death(
        self, tmp_path: Path, record: _FakeRecord, client: httpx.AsyncClient
    ) -> None:
        fake_pi = _write_fake_pi_rpc(tmp_path, "fake_pi_rpc_crash", crash_on_turn=2)
        session_id = "conv_crash"

        record.add_user_message("hello")
        result1 = await warm.run_warm_or_cold(
            session_id=session_id,
            turn_id="t1",
            new_message_text="hello",
            system_text="Rules.",
            selected_items=[],
            model="m",
            command=fake_pi,
            client=client,
            workspace=tmp_path,
        )
        assert result1.error is None
        record.add_assistant_message(result1.response_text or "")
        prior = [record.items[0], record.items[1]]
        record.add_user_message("again")

        # Turn 2 crashes mid-turn (the fake CLI exits without responding).
        result2 = await warm.run_warm_or_cold(
            session_id=session_id,
            turn_id="t2",
            new_message_text="again",
            system_text="Rules.",
            selected_items=prior,
            model="m",
            command=fake_pi,
            client=client,
            workspace=tmp_path,
        )
        assert result2.warm_reused is True  # it WAS valid to reuse; the crash is what failed it
        assert result2.error is not None
        assert result2.response_text is None
        # The dead process must not be offered for reuse on the next turn.
        assert session_id not in warm._WARM_SESSIONS

        record.add_user_message("third")
        result3 = await warm.run_warm_or_cold(
            session_id=session_id,
            turn_id="t3",
            new_message_text="third",
            system_text="Rules.",
            selected_items=prior,
            model="m",
            command=fake_pi,
            client=client,
            workspace=tmp_path,
        )
        assert result3.warm_reused is False
        assert result3.reason == "no_warm_process"
        assert result3.error is None

    async def test_idle_expired_process_is_not_reused(
        self, tmp_path: Path, record: _FakeRecord, client: httpx.AsyncClient, monkeypatch
    ) -> None:
        fake_pi = _write_fake_pi_rpc(tmp_path, "fake_pi_rpc")
        session_id = "conv_idle"

        record.add_user_message("hello")
        result1 = await warm.run_warm_or_cold(
            session_id=session_id,
            turn_id="t1",
            new_message_text="hello",
            system_text="Rules.",
            selected_items=[],
            model="m",
            command=fake_pi,
            client=client,
            workspace=tmp_path,
        )
        record.add_assistant_message(result1.response_text or "")
        prior = [record.items[0], record.items[1]]

        # Fast-forward past the idle timeout instead of sleeping for real.
        state = warm._WARM_SESSIONS[session_id]
        state.last_used -= warm._IDLE_TIMEOUT_SECONDS + 1

        record.add_user_message("again")
        result2 = await warm.run_warm_or_cold(
            session_id=session_id,
            turn_id="t2",
            new_message_text="again",
            system_text="Rules.",
            selected_items=prior,
            model="m",
            command=fake_pi,
            client=client,
            workspace=tmp_path,
        )
        assert result2.warm_reused is False
        assert result2.reason == "idle_expired"


class TestGuardedWiring:
    """End to end through ``maybe_run_blindfold_turn``, against the real
    assembler, proving the label actually gates the warm path and that turn 2
    of a real session reuses the process.
    """

    def _server(
        self, *, model: str, system_text: str, labels: dict[str, str]
    ) -> tuple[httpx.MockTransport, _FakeRecord]:
        from omnigent.context_assembly.assembler import select_history_refs

        record = _FakeRecord()

        def _route(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/context/observe"):
                return httpx.Response(204)
            if path.endswith("/context"):
                body = json.loads(request.content)
                max_messages = int(labels.get("omnigent.context.max_messages", "20"))
                refs, _ = select_history_refs(
                    record.items,
                    new_item_id=body["new_item_id"],
                    max_messages=max_messages,
                    max_input_tokens=body["budget"]["max_input_tokens"],
                    model=body["harness"]["model"],
                )
                return httpx.Response(
                    200,
                    json={
                        "contract_version": "0.2",
                        "turn_id": body["turn_id"],
                        "system": {"mode": "append", "text": system_text, "digest": "sha256:x"},
                        "memory": {"items": [], "digest": "sha256:y"},
                        "history": {
                            "summary": None,
                            "items": [{"ref": r} for r in refs],
                            "digest": "sha256:z",
                        },
                        "audit": {
                            "memory_items": 0,
                            "history_items": len(refs),
                            "summary": False,
                            "estimated_tokens": 1,
                            "fallback": False,
                        },
                    },
                )
            if path.endswith("/events"):
                return record.handle(request)
            if path.endswith("/items"):
                return record.handle(request)
            return httpx.Response(200, json={"labels": labels, "owner": "local", "model": model})

        return httpx.MockTransport(_route), record

    def _patch_client(
        self, monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport
    ) -> None:
        real_async_client = httpx.AsyncClient

        def _factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            return real_async_client(*args, **{**kwargs, "transport": transport})

        monkeypatch.setattr(httpx, "AsyncClient", _factory)

    async def test_label_unset_still_runs_the_one_shot_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No behavior change for the (overwhelmingly common) default case."""
        write_server_connection(
            tmp_path, base_url="http://server", headers={}, labels={"omnigent.blindfold": "true"}
        )
        transport, _record = self._server(
            model="anthropic/claude-haiku-4.5",
            system_text="",
            labels={"omnigent.blindfold": "true"},
        )
        self._patch_client(monkeypatch, transport)
        fake_pi_oneshot = tmp_path / "fake_pi_oneshot"
        fake_pi_oneshot.write_text(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            "argv_text = ' '.join(sys.argv[1:])\n"
            "print(json.dumps({'type': 'message_end', 'message': {'role': 'assistant', "
            "'content': [{'type': 'text', 'text': argv_text}]}}))\n"
        )
        fake_pi_oneshot.chmod(fake_pi_oneshot.stat().st_mode | stat.S_IEXEC)

        result = await pi_blindfold.maybe_run_blindfold_turn(
            bridge_dir=tmp_path,
            session_id="conv_default",
            new_message_text="hello",
            command=str(fake_pi_oneshot),
        )
        assert result.handled is True
        assert result.error is None
        # The one-shot argv (--print --mode json), not the warm RPC argv.
        assert "--print" in (result.response_text or "")
        assert "--mode json" in (result.response_text or "")
        assert "conv_default" not in warm._WARM_SESSIONS

    async def test_opted_in_session_reuses_the_process_on_turn_two(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session_id = "conv_warm_e2e"
        labels = {
            "omnigent.blindfold": "true",
            "omnigent.context.lifecycle": "warm_if_valid",
            "omnigent.context.max_messages": "50",
        }
        write_server_connection(tmp_path, base_url="http://server", headers={}, labels=labels)
        transport, record = self._server(
            model="anthropic/claude-haiku-4.5", system_text="Rules.", labels=labels
        )
        self._patch_client(monkeypatch, transport)
        fake_pi = _write_fake_pi_rpc(tmp_path, "fake_pi_rpc")

        result1 = await pi_blindfold.maybe_run_blindfold_turn(
            bridge_dir=tmp_path,
            session_id=session_id,
            new_message_text="hello",
            command=fake_pi,
        )
        assert result1.handled is True
        assert result1.error is None
        assert "turn=1" in (result1.response_text or "")
        # The real caller (the runner's native turn-completion path) is what
        # normally records the final answer; simulate that one step so
        # turn 2's assembled history includes it.
        record.add_assistant_message(result1.response_text or "")

        result2 = await pi_blindfold.maybe_run_blindfold_turn(
            bridge_dir=tmp_path,
            session_id=session_id,
            new_message_text="again",
            command=fake_pi,
        )
        assert result2.handled is True
        assert result2.error is None
        assert "turn=2" in (result2.response_text or "")
        await warm.close_warm_session(session_id)
