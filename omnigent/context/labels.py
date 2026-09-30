"""Session labels for the rollover super chat (see ``rollover/DESIGN.md``).

Shared contract, fixed in ``rollover/PLAN.md``: every rollover agent codes
against these exact names. Label unset means byte-for-byte upstream behavior.
"""

from __future__ import annotations

from collections.abc import Mapping

# Session label selecting the rollover super-chat mode. Unset (the default)
# means upstream Omnigent behavior: the CLI owns its own context.
CONTEXT_MODE_LABEL = "omnigent.context.mode"

# The only recognized value that turns rollover on. ``"blindfold"`` is a
# separate, pre-existing mode and is not handled by this label.
ROLLOVER_MODE_VALUE = "rollover"

# Labels dropped from a side-chat fork of a rollover session, mirroring the
# existing blindfold fork-drop rule (``routes_core.py``). A fork should not
# silently inherit the parent's rollover behavior.
ROLLOVER_SESSION_LABELS = frozenset({CONTEXT_MODE_LABEL})


def is_rollover(labels: Mapping[str, str] | None) -> bool:
    """Return whether *labels* marks a session as running in rollover mode.

    :param labels: The session's label mapping, or ``None`` when unavailable.
    :returns: ``True`` iff ``labels[CONTEXT_MODE_LABEL] == "rollover"``.
    """
    if not labels:
        return False
    return labels.get(CONTEXT_MODE_LABEL) == ROLLOVER_MODE_VALUE
