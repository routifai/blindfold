"""Unit tests for omnigent.context.labels — the rollover mode label."""

from __future__ import annotations

from omnigent.context.labels import (
    CONTEXT_MODE_LABEL,
    ROLLOVER_MODE_VALUE,
    ROLLOVER_SESSION_LABELS,
    is_rollover,
)


def test_context_mode_label_shared_contract_value() -> None:
    """Fixed name from rollover/PLAN.md's shared contract — nobody renames it."""
    assert CONTEXT_MODE_LABEL == "omnigent.context.mode"
    assert ROLLOVER_MODE_VALUE == "rollover"


def test_is_rollover_true_only_for_exact_value() -> None:
    assert is_rollover({CONTEXT_MODE_LABEL: "rollover"}) is True
    assert is_rollover({CONTEXT_MODE_LABEL: "blindfold"}) is False
    assert is_rollover({CONTEXT_MODE_LABEL: "Rollover"}) is False
    assert is_rollover({"unrelated.label": "x"}) is False


def test_is_rollover_false_for_none_or_empty() -> None:
    assert is_rollover(None) is False
    assert is_rollover({}) is False


def test_rollover_session_labels_includes_mode_key() -> None:
    """The fork-drop set (side chats never inherit rollover) must at least
    cover the mode switch itself."""
    assert CONTEXT_MODE_LABEL in ROLLOVER_SESSION_LABELS
