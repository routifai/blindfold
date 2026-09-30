"""Unit tests for omnigent.context_assembly.oneshot_events.

Each sample below is a sanitized, trimmed-down version of a real one-shot
run captured via ``docker exec`` against the real CLIs inside
``omnigent-runner-test`` (Claude Code 2.1.285, Pi 0.99.1, codex-cli 0.159.2)
using cheap models (claude-haiku-4-5-20251001, OpenRouter
anthropic/claude-haiku-4.5, gpt-5-nano) — the huge/irrelevant fields
(thinking signatures, full tool/agent catalogs, token costs) are stripped,
but every event's ``type`` and the field names the parser reads are exactly
what those CLIs emitted.
"""

from __future__ import annotations

from omnigent.context_assembly.oneshot_events import (
    OneShotItem,
    parse_claude_stream_json,
    parse_codex_exec_json,
    parse_pi_json_events,
)


class TestParseClaudeStreamJson:
    """`claude -p ... --output-format stream-json --verbose` stdout."""

    # One assistant event per content block is how the real CLI streams it
    # (a thinking-only event, then a tool_use-only event, ...), captured
    # reading a planted note.txt via the Read tool.
    _RAW = "\n".join(
        [
            '{"type": "system", "subtype": "init", "session_id": "s1"}',
            '{"type": "assistant", "message": {"role": "assistant", '
            '"content": [{"type": "thinking", "thinking": "I should read the file."}]}}',
            '{"type": "assistant", "message": {"role": "assistant", "content": '
            '[{"type": "tool_use", "id": "toolu_01", "name": "Read", '
            '"input": {"file_path": "/ws/note.txt"}}]}}',
            '{"type": "user", "message": {"role": "user", "content": '
            '[{"type": "tool_result", "tool_use_id": "toolu_01", '
            '"content": "1\\thello world\\n"}]}}',
            '{"type": "assistant", "message": {"role": "assistant", "content": '
            '[{"type": "text", "text": "The file says hello world."}]}}',
            '{"type": "result", "subtype": "success", "result": '
            '"The file says hello world.", "is_error": false}',
        ]
    )

    def test_parses_reasoning_function_call_output_and_message_in_order(self) -> None:
        items, final_text = parse_claude_stream_json(self._RAW, agent="Claude")
        assert [item.item_type for item in items] == [
            "reasoning",
            "function_call",
            "function_call_output",
            "message",
        ]
        assert final_text == "The file says hello world."

    def test_function_call_item_shape(self) -> None:
        items, _ = parse_claude_stream_json(self._RAW, agent="Claude")
        call = next(item for item in items if item.item_type == "function_call")
        assert call.item_data == {
            "agent": "Claude",
            "name": "Read",
            "arguments": '{"file_path":"/ws/note.txt"}',
            "call_id": "toolu_01",
        }

    def test_function_call_output_item_shape(self) -> None:
        items, _ = parse_claude_stream_json(self._RAW, agent="Claude")
        output = next(item for item in items if item.item_type == "function_call_output")
        assert output.item_data == {"call_id": "toolu_01", "output": "1\thello world\n"}

    def test_assistant_message_item_shape(self) -> None:
        items, _ = parse_claude_stream_json(self._RAW, agent="Claude")
        message = next(item for item in items if item.item_type == "message")
        assert message.item_data == {
            "role": "assistant",
            "agent": "Claude",
            "content": [{"type": "output_text", "text": "The file says hello world."}],
        }

    def test_no_result_event_leaves_final_text_none(self) -> None:
        items, final_text = parse_claude_stream_json(
            '{"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}'
        )
        assert final_text is None
        assert len(items) == 1

    def test_malformed_and_blank_lines_are_skipped(self) -> None:
        raw = "\n".join(["", "not json", '{"type": "result", "result": "ok"}', "  "])
        items, final_text = parse_claude_stream_json(raw)
        assert items == []
        assert final_text == "ok"


class TestParseCodexExecJson:
    """`codex exec ... --json` stdout."""

    # Sanitized from a real run: the model narrated intent, ran a shell
    # command, reasoned about the (sandboxed) result, then answered.
    _RAW = "\n".join(
        [
            '{"type": "thread.started", "thread_id": "t1"}',
            '{"type": "turn.started"}',
            '{"type": "item.completed", "item": {"id": "item_1", '
            '"type": "agent_message", "text": "I will check note.txt."}}',
            '{"type": "item.started", "item": {"id": "item_2", '
            '"type": "command_execution", "command": "cat note.txt", "status": "in_progress"}}',
            '{"type": "item.completed", "item": {"id": "item_2", '
            '"type": "command_execution", "command": "cat note.txt", '
            '"aggregated_output": "hello world\\n", "exit_code": 0, "status": "completed"}}',
            '{"type": "item.completed", "item": {"id": "item_3", '
            '"type": "reasoning", "text": "The file says hello world."}}',
            '{"type": "item.completed", "item": {"id": "item_4", '
            '"type": "agent_message", "text": "It says hello world."}}',
            '{"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}}',
        ]
    )

    def test_parses_in_order_ignoring_started_and_turn_events(self) -> None:
        items, final_text = parse_codex_exec_json(self._RAW, agent="Codex")
        assert [item.item_type for item in items] == [
            "message",  # item_1: "I will check note.txt."
            "function_call",  # item_2 call
            "function_call_output",  # item_2 output
            "reasoning",  # item_3
            "message",  # item_4: final answer
        ]
        assert final_text == "It says hello world."

    def test_shell_call_and_output_shape(self) -> None:
        items, _ = parse_codex_exec_json(self._RAW, agent="Codex")
        call = next(item for item in items if item.item_type == "function_call")
        output = next(item for item in items if item.item_type == "function_call_output")
        assert call.item_data == {
            "agent": "Codex",
            "name": "shell",
            "arguments": '{"command":"cat note.txt"}',
            "call_id": "item_2",
        }
        assert output.item_data == {"call_id": "item_2", "output": "hello world\n"}

    def test_nonzero_exit_code_is_appended_to_output(self) -> None:
        raw = (
            '{"type": "item.completed", "item": {"id": "c1", "type": "command_execution", '
            '"command": "false", "aggregated_output": "", "exit_code": 1}}'
        )
        items, _ = parse_codex_exec_json(raw)
        output = next(item for item in items if item.item_type == "function_call_output")
        assert output.item_data["output"] == "[exit code: 1]"

    def test_file_change_item(self) -> None:
        raw = (
            '{"type": "item.completed", "item": {"id": "fc1", "type": "file_change", '
            '"changes": [{"path": "/ws/a.py", "kind": {"type": "add"}}]}}'
        )
        items, _ = parse_codex_exec_json(raw, agent="Codex")
        call, output = items
        assert call.item_data["name"] == "apply_patch"
        assert output.item_data == {"call_id": "fc1", "output": "add /ws/a.py"}

    def test_web_search_item(self) -> None:
        raw = (
            '{"type": "item.completed", "item": {"id": "ws1", "type": "web_search", '
            '"action": {"queries": ["python latest version"]}}}'
        )
        items, _ = parse_codex_exec_json(raw, agent="Codex")
        call, output = items
        assert call.item_data["arguments"] == '{"query":"python latest version"}'
        assert output.item_data == {"call_id": "ws1", "output": "python latest version"}

    def test_unmapped_item_types_are_skipped(self) -> None:
        raw = '{"type": "item.completed", "item": {"id": "m1", "type": "mcp_tool_call"}}'
        items, final_text = parse_codex_exec_json(raw)
        assert items == []
        assert final_text is None


class TestParsePiJsonEvents:
    """`pi --print --mode json` stdout."""

    # Sanitized from a real run: assistant thinks + calls the read tool,
    # gets a toolResult, then answers — message_update deltas and
    # tool_execution_start/end are the real CLI's noise around this and are
    # intentionally not in this sample (see the module docstring for why
    # they're ignored).
    _RAW = "\n".join(
        [
            '{"type": "session", "id": "s1"}',
            '{"type": "message_start", "message": {"role": "assistant", "content": []}}',
            '{"type": "message_update", "delta": "ignored"}',
            '{"type": "message_end", "message": {"role": "assistant", "content": '
            '[{"type": "thinking", "thinking": "Let me read the file."}, '
            '{"type": "toolCall", "id": "toolu_1", "name": "read", '
            '"arguments": {"path": "note.txt"}}]}}',
            '{"type": "message_end", "message": {"role": "toolResult", '
            '"toolCallId": "toolu_1", "toolName": "read", "content": '
            '[{"type": "text", "text": "hello world\\n"}], "isError": false}}',
            '{"type": "message_end", "message": {"role": "assistant", "content": '
            '[{"type": "text", "text": "The file says hello world."}]}}',
            '{"type": "agent_end"}',
        ]
    )

    def test_parses_reasoning_function_call_output_and_message_in_order(self) -> None:
        items, final_text = parse_pi_json_events(self._RAW, agent="Pi")
        assert [item.item_type for item in items] == [
            "reasoning",
            "function_call",
            "function_call_output",
            "message",
        ]
        assert final_text == "The file says hello world."

    def test_function_call_item_shape(self) -> None:
        items, _ = parse_pi_json_events(self._RAW, agent="Pi")
        call = next(item for item in items if item.item_type == "function_call")
        assert call.item_data == {
            "agent": "Pi",
            "name": "read",
            "arguments": '{"path":"note.txt"}',
            "call_id": "toolu_1",
        }

    def test_function_call_output_item_shape(self) -> None:
        items, _ = parse_pi_json_events(self._RAW, agent="Pi")
        output = next(item for item in items if item.item_type == "function_call_output")
        assert output.item_data == {"call_id": "toolu_1", "output": "hello world\n"}

    def test_message_update_deltas_are_ignored(self) -> None:
        # Only message_end is authoritative; a message_start/message_update
        # with no matching message_end must not leak a partial item.
        raw = "\n".join(
            [
                '{"type": "message_start", "message": {"role": "assistant", "content": []}}',
                '{"type": "message_update", "contentIndex": 0, "delta": "partial"}',
            ]
        )
        items, final_text = parse_pi_json_events(raw)
        assert items == []
        assert final_text is None

    def test_tool_result_without_call_id_is_dropped(self) -> None:
        raw = '{"type": "message_end", "message": {"role": "toolResult", "content": []}}'
        items, _ = parse_pi_json_events(raw)
        assert items == []


def test_one_shot_item_is_a_plain_frozen_shape() -> None:
    item = OneShotItem("message", {"role": "assistant", "agent": "X", "content": []})
    assert item.item_type == "message"
    assert item.item_data["role"] == "assistant"
