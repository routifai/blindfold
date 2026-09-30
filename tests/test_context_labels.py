"""Tests for ``omnigent.context.labels.is_rollover``."""

from __future__ import annotations

from omnigent.context.labels import CONTEXT_MODE_LABEL, is_rollover


def test_is_rollover_true_for_rollover_label() -> None:
    assert is_rollover({CONTEXT_MODE_LABEL: "rollover"}) is True


def test_is_rollover_false_when_unset() -> None:
    """Label unset means upstream (CLI-owned) behaviour, byte-for-byte."""
    assert is_rollover(None) is False
    assert is_rollover({}) is False


def test_is_rollover_false_for_other_modes() -> None:
    """Only the exact ``"rollover"`` value opts in — e.g. blindfold stays off."""
    assert is_rollover({CONTEXT_MODE_LABEL: "blindfold"}) is False
    assert is_rollover({CONTEXT_MODE_LABEL: "ROLLOVER"}) is False
