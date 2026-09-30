"""Default context-assembly policy (contract v0.2).

``assemble()``/``observe()`` are the two calls the contract defines
(``blindfold-mission/context-assembly-contract.md``). Both are pure
computation over data the caller already fetched — no I/O here — so they run
in-process today and can move behind HTTP later with the exact same shapes
(the contract's own transport decision).

Default policy:

- ``system.text``: the agent's own instructions, unchanged (``mode:
  "append"`` — the CLI keeps its base prompt).
- ``memory.items``: ``[]`` (no memory store exists yet).
- ``history.items``: the last ``max_messages`` messages (user + assistant
  ``message`` items — a message's attached tool-call/tool-result items ride
  along, uncounted), oldest dropped first, always ending with the new
  message. ``budget.max_input_tokens`` is still a hard ceiling on top of that
  window. No summary, no compaction — v0.2 keeps this minimal on purpose;
  summarization is reserved for a later version (contract §5/"Reserved").

Session labels can override the policy for tests (see ``labels.py``); this
is the only branching in the default policy.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from typing import Any

from omnigent.context_assembly.labels import (
    DEFAULT_MAX_MESSAGES,
    MAX_MESSAGES_LABEL,
    MEMORY_FIXTURE_LABEL,
)
from omnigent.context_assembly.models import (
    AssembleAudit,
    AssembleRequest,
    AssembleResponse,
    HistoryBlock,
    HistoryItemRef,
    MemoryBlock,
    MemoryItem,
    ObserveRequest,
    SystemBlock,
)
from omnigent.runtime.compaction import count_tokens

_logger = logging.getLogger(__name__)

# A pathological session (tens of thousands of items) would make the
# group-boundary scan below expensive; the contract's own p95/timeout budget
# (500ms / 2s) means we should never spend that on a single turn. The window
# is message-count bounded anyway, so scanning further back than this can't
# change the outcome for any realistic max_messages.
_MAX_SCAN_ITEMS = 2000

ItemsProvider = Callable[[], list[dict[str, Any]]]


def _digest(value: object) -> str:
    """Return a ``sha256:<hex>`` digest of *value* (contract §3's `digest`)."""
    payload = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _item_by_id(items: list[dict[str, Any]], item_id: str) -> dict[str, Any] | None:
    for item in items:
        if item.get("id") == item_id:
            return item
    return None


def _parse_max_messages(raw: str | None) -> int:
    """Parse the ``omnigent.context.max_messages`` label, falling back safely.

    :param raw: The label's string value, or ``None`` when unset.
    :returns: A positive int — the parsed value, or
        :data:`DEFAULT_MAX_MESSAGES` when unset, non-numeric, or non-positive.
    """
    if raw is None:
        return DEFAULT_MAX_MESSAGES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_MESSAGES
    return value if value > 0 else DEFAULT_MAX_MESSAGES


def select_history_refs(
    items: list[dict[str, Any]],
    *,
    new_item_id: str,
    max_messages: int,
    max_input_tokens: int,
    model: str,
) -> tuple[list[str], bool]:
    """Pick which item ids belong in ``history.items`` (contract §3/§6/§7).

    The window counts only ``type == "message"`` items (user/assistant
    turns); a message's ``function_call`` / ``function_call_output`` /
    ``native_tool`` / ``reasoning`` items are not counted but ride along with
    whichever message they precede in the record (tool calls/results always
    appear before the assistant message they belong to in item order), so a
    kept assistant message brings its whole tool round-trip with it.

    :param items: This session's record, chronological (oldest first), as
        flat ``ConversationItem.to_api_dict()`` dicts.
    :param new_item_id: The id of the new user message. Contract §6.2:
        "every ref exists in this session's record" — when this id is
        missing from *items* there is no safe anchor, so the selection is
        empty (fail-closed at the caller, which always has at least the raw
        new message to fall back to).
    :param max_messages: How many messages the window keeps, the last always
        being the new message. Clamped to at least 1.
    :param max_input_tokens: A hard ceiling on top of the message window
        (contract §2/§6.2) — whole message-groups are dropped, oldest first,
        until the selection fits, but the new message's own group is never
        dropped.
    :param model: Harness model id, used only to pick a tokenizer encoding
        for the estimate (contract §3's `estimated_tokens` is explicitly an
        estimate, not authoritative).
    :returns: ``(ordered item ids oldest-to-newest, still_over_budget)``.
        ``still_over_budget`` is ``True`` only when the new message's own
        group alone already exceeds the budget.
    """
    if new_item_id not in {item.get("id") for item in items}:
        return [], False

    new_index = next(i for i, item in enumerate(items) if item.get("id") == new_item_id)
    window = items[: new_index + 1][-_MAX_SCAN_ITEMS:]

    message_indices = [i for i, item in enumerate(window) if item.get("type") == "message"]
    if not message_indices:
        # The new item is always a message, so this only fires on malformed
        # input; fail safe to the bare anchor rather than an empty selection.
        return [new_item_id], False

    kept_message_indices = message_indices[-max(max_messages, 1) :]
    group_count = len(kept_message_indices)
    start = min(kept_message_indices)
    # Extend left over any items that precede the earliest kept message and
    # share its response_id — its attached tool calls/results.
    boundary_response_id = window[start].get("response_id")
    while (
        start > 0
        and window[start - 1].get("type") != "message"
        and window[start - 1].get("response_id") == boundary_response_id
    ):
        start -= 1
    selected = window[start:]

    # The budget is a hard ceiling on top of the message window: drop whole
    # message-groups from the front until it fits, but never the new
    # message's own (last) group.
    while group_count > 1 and count_tokens(selected, model) > max_input_tokens:
        first_message_pos = next(i for i, it in enumerate(selected) if it.get("type") == "message")
        selected = selected[first_message_pos + 1 :]
        group_count -= 1

    over_budget = bool(selected) and count_tokens(selected, model) > max_input_tokens
    return [item["id"] for item in selected if isinstance(item.get("id"), str)], over_budget


def render_system_text(response: AssembleResponse) -> str:
    """Render the final system prompt: ``system.text`` + the memory block.

    Contract §3: the `<long_term_memory>` block is appended to the system
    text (not inlined in `system.text` itself, so the digest of the stable
    rules stays independent of memory that "changes rarely" on its own
    schedule). Harness integration code calls this — never `system.text`
    directly — to build what actually goes to the CLI.

    :param response: An `assemble()` result.
    :returns: The system prompt text to hand the CLI's system-prompt
        channel (e.g. ``--append-system-prompt``).
    """
    if not response.memory.items:
        return response.system.text
    lines = ["<long_term_memory>"]
    lines.extend(f"- ({item.kind}) {item.text}" for item in response.memory.items)
    lines.append("</long_term_memory>")
    block = "\n".join(lines)
    return f"{response.system.text}\n\n{block}" if response.system.text else block


def assemble(
    request: AssembleRequest,
    *,
    items_provider: ItemsProvider,
    agent_instructions: str | None,
) -> AssembleResponse:
    """Build the v0.2 `assemble` response for one turn.

    :param request: The turn's `AssembleRequest`.
    :param items_provider: Returns this session's full record, chronological,
        as flat item dicts. Called at most once. Kept as a callable (rather
        than requiring the caller to always fetch) so a 1-message window can
        skip the fetch entirely — see the lazy-call ordering below.
    :param agent_instructions: The bound agent's raw instructions text (the
        default policy's `system.text`), or ``None`` for an agent with none.
    :returns: The `AssembleResponse`. Never raises for a well-formed request
        with a working *items_provider* — deterministic per contract §6.1.
    :raises Exception: Whatever *items_provider* raises. Callers must treat
        that (and a timeout around this whole call) as "fail closed" per
        contract §7 — see :func:`assemble_or_fail_closed`.
    """
    labels = request.session.labels
    max_messages = _parse_max_messages(labels.get(MAX_MESSAGES_LABEL))
    memory_fixture = labels.get(MEMORY_FIXTURE_LABEL)

    system_text = agent_instructions or ""
    system = SystemBlock(mode="append", text=system_text, digest=_digest(system_text))

    memory_items: list[MemoryItem] = []
    if memory_fixture:
        memory_items = [
            MemoryItem(
                id="mem_fixture", kind="fact", text=memory_fixture, updated_at=int(time.time())
            )
        ]
    memory = MemoryBlock(
        items=memory_items,
        digest=_digest([item.model_dump() for item in memory_items]),
    )

    # A 1-message window is always just the new item itself (it has no
    # attached tool calls yet — nothing has responded to it) — skip the
    # (potentially large) record fetch.
    if max_messages <= 1:
        refs: list[str] = [request.new_item_id]
        estimated_tokens = 0
    else:
        items = items_provider()
        refs, _ = select_history_refs(
            items,
            new_item_id=request.new_item_id,
            max_messages=max_messages,
            max_input_tokens=request.budget.max_input_tokens,
            model=request.harness.model,
        )
        selected_items = [i for i in (_item_by_id(items, r) for r in refs) if i is not None]
        estimated_tokens = (
            count_tokens(selected_items, request.harness.model) if selected_items else 0
        )

    history = HistoryBlock(
        summary=None,
        items=[HistoryItemRef(ref=r) for r in refs],
        digest=_digest(refs),
    )

    audit = AssembleAudit(
        memory_items=len(memory_items),
        history_items=len(refs),
        summary=False,
        estimated_tokens=estimated_tokens,
    )
    return AssembleResponse(
        turn_id=request.turn_id, system=system, memory=memory, history=history, audit=audit
    )


def assemble_or_fail_closed(
    request: AssembleRequest,
    *,
    items_provider: ItemsProvider,
    agent_instructions: str | None,
) -> AssembleResponse:
    """Call :func:`assemble`, falling back to the fail-closed shape on error.

    Contract §7: "Timeout or error -> base prompt + the new message only;
    turn flagged in the audit". This never falls back to "all history" — the
    one failure mode the contract explicitly forbids.

    :param request: The turn's `AssembleRequest`.
    :param items_provider: See :func:`assemble`.
    :param agent_instructions: See :func:`assemble`.
    :returns: The normal `assemble()` result, or the fail-closed shape with
        ``audit.fallback = True``.
    """
    try:
        return assemble(
            request, items_provider=items_provider, agent_instructions=agent_instructions
        )
    except Exception:
        _logger.exception(
            "context assembly failed for turn=%s session=%s; failing closed",
            request.turn_id,
            request.session.id,
        )
        text = agent_instructions or ""
        return AssembleResponse(
            turn_id=request.turn_id,
            system=SystemBlock(mode="append", text=text, digest=_digest(text)),
            memory=MemoryBlock(items=[], digest=_digest([])),
            history=HistoryBlock(
                summary=None,
                items=[HistoryItemRef(ref=request.new_item_id)],
                digest=_digest([request.new_item_id]),
            ),
            audit=AssembleAudit(
                memory_items=0, history_items=1, summary=False, estimated_tokens=0, fallback=True
            ),
        )


def observe(request: ObserveRequest) -> None:
    """Record a completed turn's outcome (contract §4).

    v0.2 has no memory store or summarizer to feed — this is the hook they
    attach to later ("the assembler may ignore it" per the contract). Logged
    so an eval run has a record even before that lands.

    :param request: The turn's `ObserveRequest`.
    """
    _logger.info(
        "context_assembly.observe turn=%s session=%s outcome=%s new_items=%d "
        "input_tokens=%d output_tokens=%d",
        request.turn_id,
        request.session_id,
        request.outcome,
        len(request.new_item_ids),
        request.usage.input_tokens,
        request.usage.output_tokens,
    )
