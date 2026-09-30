"""The rollover super chat: token estimate, recent-window selection, and
the compaction item that recycles a resident native pane.

A rollover session is one long-running native CLI session (claude-native /
codex-native) that Omnigent, not the CLI, keeps bounded: once the estimated
context fill crosses a threshold, a ``compaction`` item is written with a
rolling summary plus the last few messages, and the native pane is recycled
so the next turn's resume rebuilder relaunches the CLI from exactly that
checkpoint (``rollover/DESIGN.md``).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from omnigent.context.labels import (
    DEFAULT_KEEP_MESSAGES,
    DEFAULT_KEEP_TOKENS,
    ROLLOVER_AT_TOKENS_LABEL,
    ROLLOVER_KEEP_MESSAGES_LABEL,
    ROLLOVER_KEEP_TOKENS_LABEL,
)
from omnigent.entities import NON_CONTENT_ITEM_TYPES, CompactionData
from omnigent.runtime.compaction import count_tokens, summarize_history
from omnigent.server.routes._sessions.common import (
    _LAST_CONTEXT_TOKENS_LABEL_KEY,
    _LAST_CONTEXT_WINDOW_LABEL_KEY,
)

# Fallback threshold when no window is known for the session's model —
# well under the smallest context window Omnigent routes to today.
DEFAULT_ROLLOVER_THRESHOLD_TOKENS = 90_000

# Fraction of the session's context window used as the default threshold,
# leaving headroom for a heavy tool turn before the CLI hits its own limit.
_DEFAULT_THRESHOLD_WINDOW_FRACTION = 0.45

# Marks the synthetic request/summary exchange prepended to a rollover's
# ``compacted_messages`` (and detected by build_summarization_prompt's
# progressive-summarization check) so the rolling summary stays cumulative.
_SUMMARY_REQUEST_TEXT = (
    "[This is an automatically generated summary of the prior conversation "
    "context. The original messages are available but not included in this "
    "prompt for brevity.]\n\nPlease provide a summary of our conversation so far."
)

# Fixed, code-authored preface for every rollover summary (PLAN.md "A,
# revision 2" point 3) — never written by the LLM, so its wording can't drift
# or be talked around by the summarization prompt.
CHECKPOINT_HEADER = (
    "This conversation grew past its context limit and earlier turns were "
    "compacted into the summary below. The work in it is your own; build on "
    "it instead of redoing it. Files, processes and jobs your tools created "
    "still exist. Standing instructions and memory are live every turn and "
    "are not part of this summary. When facts conflict, the latest evidence "
    "and user corrections win. The summary may omit details: recover exact "
    "earlier messages with the session_history tool before relying on them."
)


def _state_file_summarizer_instruction() -> str:
    """Build the "state file, not a narrative" summarizer instruction
    (PLAN.md "A, revision 2" point 4), with today's date filled in by code.
    """
    today = datetime.now(UTC).date().isoformat()
    return (
        f"Write the summary as a state file, not a narrative. Start with "
        f"'## Context checkpoint — {today}'. Organize by topic: the user's "
        "identity, preferences and constraints (including corrections and "
        '"do not" rules) first; each active task with its exact identifiers '
        "(ids, paths, URLs, numbers), a time-zoned timestamp, its status, "
        "and its next step; keep look-alike items in separate sections; "
        'state negative facts explicitly (e.g. "no message was sent"); '
        "record what the user was told about any failure; use only "
        "absolute dates, never relative ones. End with 'Current position / "
        "next step'. If the input already starts with a prior checkpoint, "
        "rewrite it into the new one — update entries in place and drop "
        "resolved noise — rather than appending to it."
    )


def _parse_positive_int(raw: str | None) -> int | None:
    """Parse *raw* as a positive int, or ``None`` when unset/invalid."""
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def estimate_context_tokens(
    items_since_last_compaction: list[dict[str, Any]],
    *,
    model: str,
    labels: Mapping[str, str] | None = None,
) -> int:
    """
    Estimate the model's current context-window fill, in tokens.

    Prefers the last real usage a native forwarder reported
    (:data:`_LAST_CONTEXT_TOKENS_LABEL_KEY`, set from the
    ``external_session_usage`` event) since it reflects the CLI's own
    tokenizer and system-prompt overhead. Falls back to a tiktoken estimate
    over *items_since_last_compaction* when no real usage has been reported
    yet (e.g. right after a rollover, or for a harness that reports none).

    :param items_since_last_compaction: This session's items after the
        latest ``compaction`` item (or the whole record when there is none),
        as flat ``ConversationItem.to_api_dict()`` dicts.
    :param model: LLM model string, used to pick a tokenizer for the
        fallback estimate.
    :param labels: The session's labels, or ``None``.
    :returns: An approximate token count for the model's current context.
    """
    if labels is not None:
        reported = _parse_positive_int(labels.get(_LAST_CONTEXT_TOKENS_LABEL_KEY))
        if reported is not None:
            return reported
    return count_tokens(items_since_last_compaction, model)


def resolve_rollover_threshold(labels: Mapping[str, str] | None) -> int:
    """
    Resolve the token count at which a rollover session rolls over.

    Priority: the :data:`~omnigent.context.labels.ROLLOVER_AT_TOKENS_LABEL`
    label (an explicit positive int) > 45% of the session's last-reported
    context window > :data:`DEFAULT_ROLLOVER_THRESHOLD_TOKENS`.

    :param labels: The session's labels, or ``None``.
    :returns: The threshold, in tokens.
    """
    labels = labels or {}
    explicit = _parse_positive_int(labels.get(ROLLOVER_AT_TOKENS_LABEL))
    if explicit is not None:
        return explicit
    window = _parse_positive_int(labels.get(_LAST_CONTEXT_WINDOW_LABEL_KEY))
    if window is not None:
        return int(window * _DEFAULT_THRESHOLD_WINDOW_FRACTION)
    return DEFAULT_ROLLOVER_THRESHOLD_TOKENS


def resolve_keep_messages(labels: Mapping[str, str] | None) -> int:
    """Resolve ``omnigent.context.rollover_keep_messages``, default 20."""
    labels = labels or {}
    value = _parse_positive_int(labels.get(ROLLOVER_KEEP_MESSAGES_LABEL))
    return value if value is not None else DEFAULT_KEEP_MESSAGES


def resolve_keep_tokens(labels: Mapping[str, str] | None) -> int:
    """Resolve ``omnigent.context.rollover_keep_tokens``, default 16,000."""
    labels = labels or {}
    value = _parse_positive_int(labels.get(ROLLOVER_KEEP_TOKENS_LABEL))
    return value if value is not None else DEFAULT_KEEP_TOKENS


def select_recent(
    items: list[dict[str, Any]],
    *,
    keep_messages: int,
    keep_tokens: int,
    model: str,
) -> list[dict[str, Any]]:
    """
    Select the trailing whole turns that fit both budgets.

    A turn is a user message plus everything after it up to (not including)
    the next user message. Turns are added whole, walking backward from the
    end, while the running total stays within *keep_tokens*
    (``count_tokens``) and *keep_messages* (user+assistant message count) —
    a turn is never split, so a kept ``function_call`` always keeps its
    output. The tail always starts at a user message; when even the single
    most recent turn alone exceeds a budget, the tail is empty (measured on
    real Muse compactions: this happened 2 of 5 times) and the summary must
    cover it instead.

    :param items: Chronological (oldest first) flat item dicts.
    :param keep_messages: Message-count budget (user+assistant).
    :param keep_tokens: Token budget for the kept tail.
    :param model: LLM model string, used to pick a tokenizer for the budget.
    :returns: The selected trailing whole turns, chronological, or ``[]``.
    """
    if not items:
        return []
    turn_starts = [
        i
        for i, item in enumerate(items)
        if item.get("type") == "message" and item.get("role") == "user"
    ]
    if not turn_starts:
        return []
    turn_bounds = [
        (start, turn_starts[i + 1] if i + 1 < len(turn_starts) else len(items))
        for i, start in enumerate(turn_starts)
    ]

    selected_start = len(items)
    message_count = 0
    for start, end in reversed(turn_bounds):
        turn_message_count = sum(1 for it in items[start:end] if it.get("type") == "message")
        candidate_message_count = message_count + turn_message_count
        if candidate_message_count > keep_messages:
            break
        if count_tokens(items[start:], model) > keep_tokens:
            break
        selected_start = start
        message_count = candidate_message_count
    return items[selected_start:] if selected_start < len(items) else []


# The Responses API input fields per item type. Anything else a harness adds
# (ids, stream ids, statuses) makes a strict provider reject the call.
_SUMMARIZER_INPUT_FIELDS: dict[str, tuple[str, ...]] = {
    "message": ("type", "role", "content"),
    "function_call": ("type", "call_id", "name", "arguments"),
    "function_call_output": ("type", "call_id", "output"),
}
_TEXT_BLOCK_TYPES = frozenset({"input_text", "output_text"})


def _content_for_summarizer(content: Any) -> Any:
    """Keep text blocks as bare ``{type, text}``; other blocks pass through."""
    if not isinstance(content, list):
        return content
    return [
        {"type": block["type"], "text": block.get("text", "")}
        if isinstance(block, dict) and block.get("type") in _TEXT_BLOCK_TYPES
        else block
        for block in content
    ]


def _items_for_summarizer(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project *items* onto the provider input schema for the summary call.

    Only messages and tool calls/results are sent, with only their schema
    fields; reasoning and harness-specific items are dropped. The kept tail
    (:func:`select_recent`) keeps the full dicts the resume rebuilders need.
    """
    projected: list[dict[str, Any]] = []
    for item in items:
        fields = _SUMMARIZER_INPUT_FIELDS.get(str(item.get("type")))
        if fields is None:
            continue
        out = {key: item[key] for key in fields if key in item}
        if "content" in out:
            out["content"] = _content_for_summarizer(out["content"])
        projected.append(out)
    return projected


def _summary_exchange(summary_text: str) -> list[dict[str, Any]]:
    """Build the synthetic user/assistant pair standing in for a summary."""
    return [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": _SUMMARY_REQUEST_TEXT}],
        },
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": summary_text}],
        },
    ]


async def build_rollover_item(
    items_since_previous_compaction: list[dict[str, Any]],
    *,
    previous_summary: str | None,
    keep_messages: int,
    keep_tokens: int = DEFAULT_KEEP_TOKENS,
    model: str,
    llm_client: Any = None,
    connection: dict[str, str] | None = None,
    runner_client: Any | None = None,
    conversation_id: str | None = None,
) -> CompactionData:
    """
    Build the next rollover compaction item: a rolling summary + recent tail.

    The summary covers everything in *items_since_previous_compaction*,
    merged with *previous_summary* (progressive summarization — see
    ``summarize_history``/``build_summarization_prompt``) so it stays
    cumulative across rollovers rather than restarting each time. The LLM's
    own text is asked for as a state file (``_state_file_summarizer_instruction``)
    and then given the fixed, code-authored :data:`CHECKPOINT_HEADER` — never
    written by the LLM itself. ``compacted_messages`` is shaped exactly like
    other producers' (``compaction_to_history_items``, the codex forwarder's
    ``replacement_history``): flat item dicts, summary pair first, so both
    the claude-native and codex-native resume rebuilders accept it directly.
    No memory/standing-instruction content goes in here — those are injected
    live every turn (see the header) — and ``token_count`` is the estimated
    size of summary + kept tail together (Claude's ``compact_boundary``
    needs a real post-compaction figure, not just the summary's own size).

    :param items_since_previous_compaction: This session's items after the
        latest compaction item (or the whole record when there is none),
        chronological, as flat item dicts. Must be non-empty.
    :param previous_summary: The prior rollover's summary text, or ``None``
        for the session's first rollover.
    :param keep_messages: Message-count budget for the kept tail.
    :param keep_tokens: Token budget for the kept tail.
    :param model: LLM model string for the summarization call and its
        token estimate.
    :param llm_client: LLM client for direct summarization. Ignored when
        *runner_client* is set.
    :param connection: Per-provider connection overrides, forwarded as-is.
    :param runner_client: When set, delegates the summarization LLM call to
        the runner's ``/v1/summarize`` so the runner's own credentials are
        used (the preferred path — see ``summarize_history``).
    :param conversation_id: Session id, forwarded to the runner delegation.
    :returns: A new :class:`CompactionData` ready to be posted as a
        ``compaction`` item.
    :raises ValueError: If *items_since_previous_compaction* is empty.
    """
    if not items_since_previous_compaction:
        raise ValueError("build_rollover_item requires at least one item to summarize")

    messages_to_summarize = _items_for_summarizer(items_since_previous_compaction)
    if previous_summary:
        messages_to_summarize = _summary_exchange(previous_summary) + messages_to_summarize

    summary = await summarize_history(
        messages_to_summarize,
        llm_client,
        model,
        connection,
        runner_client,
        conversation_id,
        extra_instructions=_state_file_summarizer_instruction(),
    )
    summary_text = f"{CHECKPOINT_HEADER}\n\n{summary['text']}"
    recent = select_recent(
        items_since_previous_compaction,
        keep_messages=keep_messages,
        keep_tokens=keep_tokens,
        model=model,
    )
    last_item_id = items_since_previous_compaction[-1]["id"]
    compacted_messages = _summary_exchange(summary_text) + recent
    return CompactionData(
        summary=summary_text,
        last_item_id=last_item_id,
        model=model,
        token_count=count_tokens(compacted_messages, model),
        compacted_messages=compacted_messages,
    )


async def build_side_chat_seed(
    items: list[dict[str, Any]],
    *,
    keep_messages: int,
    keep_tokens: int = DEFAULT_KEEP_TOKENS,
    model: str,
    llm_client: Any = None,
    connection: dict[str, str] | None = None,
    runner_client: Any | None = None,
    conversation_id: str | None = None,
) -> CompactionData:
    """
    Build the seed compaction item for a rollover side chat, Muse-style.

    A side chat forked from a rollover session stays a rollover session,
    but must not inherit the parent's full transcript — its CLI opens from
    exactly one checkpoint: the parent's latest summary (built now, over the
    whole record, if the parent never rolled over) plus the recent tail.

    :param items: The parent's full chronological record, as flat item
        dicts. Must be non-empty.
    :param keep_messages: Message-count budget for the kept tail.
    :param keep_tokens: Token budget for the kept tail.
    :param model: LLM model string for the summarization call, when one is
        needed.
    :param llm_client: LLM client for direct summarization. Ignored when
        *runner_client* is set.
    :param connection: Per-provider connection overrides, forwarded as-is.
    :param runner_client: Preferred path — delegates to the runner's own
        credentials, as in :func:`build_rollover_item`.
    :param conversation_id: The PARENT session's id (the summarization call
        runs against the parent's runner binding, not the not-yet-created
        fork's).
    :returns: A :class:`CompactionData` seed, ready to post as the fork's
        own ``compaction`` item.
    :raises ValueError: If *items* is empty.
    """
    # Raw session items include lifecycle entries the model never saw; keep the
    # parent's checkpoints, which the seed builds on.
    items = [
        item
        for item in items
        if item.get("type") == "compaction" or item.get("type") not in NON_CONTENT_ITEM_TYPES
    ]
    if not items:
        raise ValueError("build_side_chat_seed requires a non-empty parent record")

    last_compaction_index = next(
        (i for i in range(len(items) - 1, -1, -1) if items[i].get("type") == "compaction"),
        None,
    )
    if last_compaction_index is None:
        return await build_rollover_item(
            items,
            previous_summary=None,
            keep_messages=keep_messages,
            keep_tokens=keep_tokens,
            model=model,
            llm_client=llm_client,
            connection=connection,
            runner_client=runner_client,
            conversation_id=conversation_id,
        )

    checkpoint = items[last_compaction_index]
    items_since = items[last_compaction_index + 1 :]
    if not items_since:
        # Nothing new since the parent's last checkpoint — reuse it verbatim
        # rather than re-summarizing zero material.
        return CompactionData(
            summary=checkpoint.get("summary", ""),
            last_item_id=checkpoint.get("last_item_id") or items[-1]["id"],
            model=checkpoint.get("model"),
            token_count=checkpoint.get("token_count", 0),
            compacted_messages=checkpoint.get("compacted_messages"),
        )
    return await build_rollover_item(
        items_since,
        previous_summary=checkpoint.get("summary"),
        keep_messages=keep_messages,
        keep_tokens=keep_tokens,
        model=model,
        llm_client=llm_client,
        connection=connection,
        runner_client=runner_client,
        conversation_id=conversation_id,
    )
