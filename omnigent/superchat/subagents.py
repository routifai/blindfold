"""Sub-agents for ``superside-chat``: launch-by-Type, caps, Result delivery.

Pure decision logic for slice S3 (``rollover/SUPERSIDE-CHAT-PLAN.md``): the
async I/O (REST lookups, the wake POST) stays in ``omnigent/runner/app.py``
and ``omnigent/runner/tool_dispatch.py``; this module holds the gated,
testable rules those call sites apply. Everything here is a no-op outside
``superside-chat`` sessions — call sites gate on ``is_superside_chat``
before calling in, except where a function takes that fact as an argument.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping

from omnigent.util.session_lifecycle import is_session_closed

# ── Default concurrency cap (across all Sub-agent Types, per tree) ────────

#: Env var overriding the default sub-agent concurrency cap for a
#: superside-chat tree. A Sub-agent Type's own ``max_sessions`` (from its
#: ``config.yaml``, parsed by ``omnigent/spec/parser.py::parse``) applies on
#: top of this, not instead of it.
DEFAULT_CONCURRENCY_ENV = "OMNIGENT_SUBAGENT_MAX_CONCURRENT"
DEFAULT_CONCURRENCY_CAP = 10


def resolve_default_concurrency_cap(env: Mapping[str, str] | None = None) -> int:
    """Resolve the default cross-type concurrency cap for a superside-chat tree.

    :param env: Environment mapping to read; ``os.environ`` when ``None``
        (tests pass a fake mapping instead of mutating process env).
    :returns: The configured cap, or :data:`DEFAULT_CONCURRENCY_CAP` when
        unset, non-integer, or non-positive.
    """
    source = env if env is not None else os.environ
    raw = source.get(DEFAULT_CONCURRENCY_ENV)
    if raw is None:
        return DEFAULT_CONCURRENCY_CAP
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_CONCURRENCY_CAP
    return value if value > 0 else DEFAULT_CONCURRENCY_CAP


def is_live_child_row(row: Mapping[str, object]) -> bool:
    """Whether a ``child_sessions`` row (:class:`ChildSessionSummary`) is still live.

    Mirrors the busy/queued/in_progress reading the rail uses: a closed
    child is never live regardless of these fields.

    :param row: One row as returned by ``GET /v1/sessions/{id}/child_sessions``.
    :returns: ``True`` when the child's turn is running or about to run.
    """
    labels = row.get("labels")
    title = row.get("title")
    if is_session_closed(
        labels if isinstance(labels, dict) else None,
        title if isinstance(title, str) else None,
    ):
        return False
    if row.get("busy") is True:
        return True
    return row.get("current_task_status") in ("queued", "in_progress")


def count_live_children(
    rows: Iterable[Mapping[str, object]],
    *,
    agent_type: str | None = None,
) -> int:
    """Count live (non-terminal, non-closed) child rows, optionally by Type.

    :param rows: ``child_sessions`` rows for one parent.
    :param agent_type: When given, count only children whose ``tool``
        (Sub-agent Type) matches; ``None`` counts every type.
    :returns: Number of matching live rows.
    """
    count = 0
    for row in rows:
        if agent_type is not None and row.get("tool") != agent_type:
            continue
        if is_live_child_row(row):
            count += 1
    return count


def refuse_subagent_concurrency(
    *,
    live_count_of_type: int,
    live_count_total: int,
    type_cap: int | None,
    default_cap: int,
) -> str | None:
    """Return why a new sub-agent dispatch must be refused, or ``None`` if allowed.

    Two independent caps, either can refuse: the Sub-agent Type's own
    ``max_sessions`` (counted against sub-agents of that one Type under the
    caller), and the tree-wide default (counted against every Sub-agent
    Type together) — see ``rollover/SUPERSIDE-CHAT-PLAN.md`` slice S3.

    :param live_count_of_type: Live children of the Type about to be
        dispatched.
    :param live_count_total: Live children of any Type under the caller.
    :param type_cap: The Type's own ``max_sessions``, or ``None`` when the
        Type declares no cap.
    :param default_cap: The tree-wide default cap
        (:func:`resolve_default_concurrency_cap`).
    :returns: An error message, or ``None`` when the dispatch may proceed.
    """
    if type_cap is not None and live_count_of_type >= type_cap:
        return (
            f"sub-agent concurrency cap reached for this Sub-agent Type: "
            f"{live_count_of_type}/{type_cap} already running"
        )
    if live_count_total >= default_cap:
        return (
            f"sub-agent concurrency cap reached: {live_count_total}/{default_cap} "
            "already running under this chat"
        )
    return None


# ── Nesting cap: chat (depth 0) -> Sub-agent (1) -> coordinator's child (2) ──


def refuse_subagent_nesting(*, caller_kind: str | None, parent_kind: str | None) -> str | None:
    """Return why a sub-agent launch must be refused for nesting, or ``None``.

    ``rollover/CONTEXT.md`` Relationships: "A Sub-agent launched by a chat
    may launch its own Sub-agents (acting as a coordinator); those cannot
    launch any further. At most two levels below a chat." The chat itself
    (``caller_kind != "sub_agent"``) and its direct Sub-agent (one level
    down) may launch; a Sub-agent whose own parent is already a Sub-agent
    (two levels down) may not.

    :param caller_kind: The calling session's ``Conversation.kind``.
    :param parent_kind: The calling session's parent's ``Conversation.kind``,
        or ``None`` when the caller has no parent (it is the chat) or the
        parent is unknown.
    :returns: An error message, or ``None`` when the launch may proceed.
    """
    if caller_kind != "sub_agent":
        return None
    if parent_kind == "sub_agent":
        return (
            "nesting cap reached: a sub-agent launched by another sub-agent "
            "(a coordinator's child) cannot launch further sub-agents — only "
            "the chat and its direct sub-agents may"
        )
    return None


# ── Launch by Sub-agent Type only: no raw model/effort override ───────────


def refuse_subagent_dispatch_override(
    *, model: str | None, reasoning_effort: str | None
) -> str | None:
    """Return why a per-dispatch override must be refused, or ``None``.

    ``rollover/CONTEXT.md`` ("Sub-agent Type"): a superside-chat session
    launches a Sub-agent by its declared Type, never by naming a model —
    the Type's own ``config.yaml`` (model, reasoning effort) decides.

    :param model: The dispatch's requested ``model`` override, if any.
    :param reasoning_effort: The dispatch's requested ``reasoning_effort``
        override, if any.
    :returns: An error message, or ``None`` when neither override was given.
    """
    if model is not None:
        return (
            "model overrides are not allowed in superside-chat sessions; "
            "the Sub-agent Type's own config decides its model"
        )
    if reasoning_effort is not None:
        return (
            "reasoning_effort overrides are not allowed in superside-chat "
            "sessions; the Sub-agent Type's own config decides its "
            "reasoning effort"
        )
    return None


# ── Brief + Memory Profile (hook for slice S6) ─────────────────────────────


def memory_profile_for(user: str | None) -> str | None:
    """Hook: the Memory Profile block to prepend to a new sub-agent's Brief.

    ``rollover/CONTEXT.md`` ("Memory Profile", "Sub-agent"): every new
    sub-agent starts from its Brief plus the user's Memory Profile. Slice
    S6 implements the real lookup; until then this always returns ``None``
    so a sub-agent starts from its Brief alone, unchanged from before S3.

    :param user: Identity the profile would be looked up for (best-effort —
        whatever the dispatch call site has on hand, e.g. the dispatching
        human actor).
    :returns: The profile text, or ``None`` when unavailable.
    """
    del user  # unused until S6 wires the real lookup
    return None


def prepend_memory_profile(message: str, profile: str | None) -> str:
    """Prepend a Memory Profile block to a sub-agent's first message (Brief).

    :param message: The Brief text as written by the Originating Chat.
    :param profile: :func:`memory_profile_for`'s result; a no-op when
        ``None`` or empty.
    :returns: ``message``, with the profile block prepended when given.
    """
    if not profile:
        return message
    return f"Memory Profile:\n{profile}\n\n{message}"


# ── Result delivered in the wake, instead of only "N results waiting" ─────

#: Characters of a sub-agent's Result kept verbatim in the wake notice
#: before pointing the parent at the full transcript instead.
RESULT_PREVIEW_MAX_CHARS = 4000


def format_subagent_wake_notice_with_result(
    *,
    agent: str,
    title: str,
    status: str,
    child_session_id: str,
    result_text: str | None,
    max_chars: int = RESULT_PREVIEW_MAX_CHARS,
) -> str:
    """Build a superside-chat wake notice that inlines the Result itself.

    ``rollover/CONTEXT.md`` ("Result"): the sub-agent's final message is
    "the only thing a chat receives from a sub-agent without asking" — so
    for a superside-chat parent the wake carries that message directly
    rather than only "N results waiting in inbox".

    :param agent: Sub-agent Type name, e.g. ``"researcher"``.
    :param title: Sub-agent instance title, e.g. ``"auth"``.
    :param status: Terminal status, e.g. ``"completed"``.
    :param child_session_id: The sub-agent's session id, so the parent can
        read more when the Result was capped.
    :param result_text: The sub-agent's final message. ``None``/empty
        renders as ``"(no output)"``.
    :param max_chars: Cap on verbatim Result text kept in the notice.
    :returns: A ``[System: ...]`` notice string with the Result inlined.
    """
    text = (result_text or "").strip() or "(no output)"
    if len(text) > max_chars:
        body = (
            f"{text[:max_chars]}... [truncated; call sys_session_get_history "
            f"with conversation_id={child_session_id!r} to read the rest]"
        )
    else:
        body = text
    return f"[System: sub-agent {agent}/{title} finished ({status}) — result:\n{body}]"


# ── Archived Originating Side Chat: redirect the Result to its Super Chat ─


def resolve_wake_target(
    *,
    parent_id: str,
    archived: bool,
    is_side_chat: bool,
    fork_source_id: str | None,
) -> tuple[str, str | None]:
    """Resolve where a sub-agent's wake/Result should actually be delivered.

    ``rollover/CONTEXT.md`` ("Originating Chat", "Archived"): a Side Chat is
    hidden once archived, not deleted; the Result of work it launched still
    reaches the user, so it is redirected to the Super Chat instead
    (resolved from the Side Chat's ``omnigent.fork.source_id`` label).

    :param parent_id: The sub-agent's recorded Originating Chat id.
    :param archived: Whether that chat is currently archived.
    :param is_side_chat: Whether that chat carries the Side Chat label.
    :param fork_source_id: That chat's ``omnigent.fork.source_id`` label
        (its Super Chat), when present.
    :returns: ``(target_session_id, note)`` — ``target_session_id`` is
        ``parent_id`` unchanged unless redirected; ``note`` is ``None``
        unless a redirect happened, in which case it names the archived
        Side Chat for the Super Chat reader.
    """
    if archived and is_side_chat and fork_source_id:
        return fork_source_id, (
            f"[System: this result's Originating Chat ({parent_id}) is an "
            "archived Side Chat; delivering it here, to its Super Chat, "
            "instead.]"
        )
    return parent_id, None
