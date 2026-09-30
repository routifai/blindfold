"""Unit tests for omnigent.context.labels."""

from __future__ import annotations

from omnigent.context.labels import (
    CONTEXT_MODE_LABEL,
    ROLLOVER_AT_TOKENS_LABEL,
    ROLLOVER_KEEP_MESSAGES_LABEL,
    ROLLOVER_SESSION_LABELS,
    is_rollover,
)


def test_is_rollover_true_only_for_exact_value() -> None:
    assert is_rollover({CONTEXT_MODE_LABEL: "rollover"}) is True
    assert is_rollover({CONTEXT_MODE_LABEL: "blindfold"}) is False
    assert is_rollover({CONTEXT_MODE_LABEL: "Rollover"}) is False


def test_is_rollover_false_when_unset() -> None:
    assert is_rollover({}) is False
    assert is_rollover(None) is False
    assert is_rollover({"some.other.label": "1"}) is False


def test_rollover_session_labels_cover_every_rollover_label() -> None:
    assert (
        frozenset({CONTEXT_MODE_LABEL, ROLLOVER_AT_TOKENS_LABEL, ROLLOVER_KEEP_MESSAGES_LABEL})
        == ROLLOVER_SESSION_LABELS
    )
