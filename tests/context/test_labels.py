"""Unit tests for omnigent.context.labels."""

from __future__ import annotations

from omnigent.context.labels import (
    CONTEXT_MODE_LABEL,
    ROLLOVER_AT_TOKENS_LABEL,
    ROLLOVER_KEEP_TOKENS_LABEL,
    ROLLOVER_MODE_VALUE,
    ROLLOVER_SESSION_LABELS,
    SUPERSIDE_CHAT_MODE_VALUE,
    inheritable_context_labels,
    is_rollover,
    is_superside_chat,
    uses_omnigent_context,
)


def test_context_mode_label_shared_contract_value() -> None:
    """Fixed label name; other components key off it."""
    assert CONTEXT_MODE_LABEL == "omnigent.context.mode"
    assert ROLLOVER_MODE_VALUE == "rollover"


def test_is_rollover_true_only_for_exact_value() -> None:
    assert is_rollover({CONTEXT_MODE_LABEL: "rollover"}) is True
    assert is_rollover({CONTEXT_MODE_LABEL: "blindfold"}) is False
    assert is_rollover({CONTEXT_MODE_LABEL: "Rollover"}) is False
    assert is_rollover({"unrelated.label": "x"}) is False


def test_is_rollover_false_when_unset() -> None:
    assert is_rollover({}) is False
    assert is_rollover(None) is False
    assert is_rollover({"some.other.label": "1"}) is False


def test_rollover_session_labels_cover_every_rollover_label() -> None:
    assert (
        frozenset(
            {
                CONTEXT_MODE_LABEL,
                ROLLOVER_AT_TOKENS_LABEL,
                ROLLOVER_KEEP_TOKENS_LABEL,
            }
        )
        == ROLLOVER_SESSION_LABELS
    )


def test_superside_chat_mode_value_is_distinct_from_rollover() -> None:
    assert SUPERSIDE_CHAT_MODE_VALUE == "superside-chat"
    assert SUPERSIDE_CHAT_MODE_VALUE != ROLLOVER_MODE_VALUE


def test_is_superside_chat_true_only_for_exact_value() -> None:
    assert is_superside_chat({CONTEXT_MODE_LABEL: "superside-chat"}) is True
    assert is_superside_chat({CONTEXT_MODE_LABEL: "rollover"}) is False
    assert is_superside_chat({CONTEXT_MODE_LABEL: "Superside-Chat"}) is False
    assert is_superside_chat({"unrelated.label": "x"}) is False


def test_is_superside_chat_false_when_unset() -> None:
    assert is_superside_chat({}) is False
    assert is_superside_chat(None) is False


def test_uses_omnigent_context_true_for_either_mode() -> None:
    assert uses_omnigent_context({CONTEXT_MODE_LABEL: "rollover"}) is True
    assert uses_omnigent_context({CONTEXT_MODE_LABEL: "superside-chat"}) is True


def test_uses_omnigent_context_false_otherwise() -> None:
    assert uses_omnigent_context({}) is False
    assert uses_omnigent_context(None) is False
    assert uses_omnigent_context({CONTEXT_MODE_LABEL: "blindfold"}) is False


def test_inheritable_context_labels_keeps_only_the_rollover_label_set() -> None:
    labels = {
        CONTEXT_MODE_LABEL: "superside-chat",
        ROLLOVER_AT_TOKENS_LABEL: "50000",
        "omnigent.ui": "terminal",
        "omnigent.wrapper": "claude-native",
    }
    assert inheritable_context_labels(labels) == {
        CONTEXT_MODE_LABEL: "superside-chat",
        ROLLOVER_AT_TOKENS_LABEL: "50000",
    }


def test_inheritable_context_labels_empty_for_none_or_empty() -> None:
    assert inheritable_context_labels(None) == {}
    assert inheritable_context_labels({}) == {}
    assert inheritable_context_labels({"unrelated.label": "x"}) == {}
