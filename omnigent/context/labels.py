"""Session-label keys the rollover super chat reacts to.

``omnigent.context.mode`` selects the lifecycle: unset means upstream
behavior (the CLI owns its own context) untouched; ``"rollover"`` means
Omnigent recycles the resident native pane from a summary + recent-messages
checkpoint once the session's context gets long. See ``rollover/DESIGN.md``.
"""

from __future__ import annotations

from collections.abc import Mapping

# Selects the rollover lifecycle for a session. Unset (the default) leaves
# the session on upstream behavior, byte-for-byte.
CONTEXT_MODE_LABEL = "omnigent.context.mode"
ROLLOVER_MODE_VALUE = "rollover"

# Token count at which a rollover session rolls over. Unset falls back to
# 45% of the session's context window, or 90,000 when the window is unknown
# (see rollover.resolve_rollover_threshold).
ROLLOVER_AT_TOKENS_LABEL = "omnigent.context.rollover_at_tokens"

# How many of the most recent messages a rollover keeps verbatim (tool items
# ride along with the message they belong to). Unset -> DEFAULT_KEEP_MESSAGES.
ROLLOVER_KEEP_MESSAGES_LABEL = "omnigent.context.rollover_keep_messages"
DEFAULT_KEEP_MESSAGES = 20


def is_rollover(labels: Mapping[str, str] | None) -> bool:
    """
    Whether *labels* configure a session for the rollover super-chat mode.

    :param labels: Session labels, or ``None``.
    :returns: ``True`` only when :data:`CONTEXT_MODE_LABEL` is exactly
        :data:`ROLLOVER_MODE_VALUE`.
    """
    if not labels:
        return False
    return labels.get(CONTEXT_MODE_LABEL) == ROLLOVER_MODE_VALUE


# Every label that configures rollover mode for a session; a side chat forked
# from the super chat drops all of them (routes_core.py fork handling).
ROLLOVER_SESSION_LABELS: frozenset[str] = frozenset(
    {CONTEXT_MODE_LABEL, ROLLOVER_AT_TOKENS_LABEL, ROLLOVER_KEEP_MESSAGES_LABEL}
)
