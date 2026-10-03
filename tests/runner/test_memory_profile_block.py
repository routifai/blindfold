"""Tests for ``_apply_memory_profile_to_body`` (per-turn Memory Profile delivery, S6).

Pure composition: given this turn's outgoing harness body and the rendered
(delimiter-wrapped) Memory Profile block, prepend it to the content the
engine sees, without mutating the input. The caller (``_run_turn_bg_setup_
and_stream``) only reaches this for ``superside-chat`` sessions and never
persists the result — covered by inspection (the persisted user item is
built from ``msg_body``, a separate dict the mutation never touches) and by
these shape tests, which mirror ``_prefix_content_with_tail``'s existing
coverage pattern for the analogous post-compaction tail.
"""

from __future__ import annotations

from omnigent.runner.app import _apply_memory_profile_to_body

_BLOCK = (
    "[Standing memory about the user — provided by the system, not a message "
    "from the user]\n\nPreferences:\n- Prefers figures in CAD\n\n"
    "[End of standing memory]"
)


def test_no_block_leaves_the_body_untouched() -> None:
    body = {"content": "hello"}
    assert _apply_memory_profile_to_body(body, None) is body


def test_empty_block_leaves_the_body_untouched() -> None:
    body = {"content": "hello"}
    assert _apply_memory_profile_to_body(body, "") is body


def test_prepends_to_a_plain_string_content() -> None:
    body = {"content": "hello"}
    result = _apply_memory_profile_to_body(body, _BLOCK)
    assert result["content"] == f"{_BLOCK}\n\nhello"
    # The input dict itself is never mutated.
    assert body["content"] == "hello"


def test_prepends_to_a_flat_content_blocks_list() -> None:
    body = {"content": [{"type": "input_text", "text": "hello"}]}
    result = _apply_memory_profile_to_body(body, _BLOCK)
    assert result["content"][0]["text"] == f"{_BLOCK}\n\nhello"
    assert body["content"][0]["text"] == "hello"


def test_prepends_to_the_latest_user_message_in_a_history_list() -> None:
    body = {
        "content": [
            {"type": "message", "role": "user", "content": [{"type": "text", "text": "first"}]},
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "reply"}],
            },
            {"type": "message", "role": "user", "content": [{"type": "text", "text": "second"}]},
        ]
    }
    result = _apply_memory_profile_to_body(body, _BLOCK)
    messages = result["content"]
    assert messages[0]["content"][0]["text"] == "first"  # earlier turns untouched
    assert messages[1]["content"][0]["text"] == "reply"
    assert messages[2]["content"][0]["text"] == f"{_BLOCK}\n\nsecond"


def test_preserves_other_body_fields() -> None:
    body = {"content": "hello", "model": "test-model", "instructions": "be nice"}
    result = _apply_memory_profile_to_body(body, _BLOCK)
    assert result["model"] == "test-model"
    assert result["instructions"] == "be nice"
