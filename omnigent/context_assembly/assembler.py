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
- ``history.items``: the most recent record items that fit
  ``budget.max_input_tokens``, oldest dropped first, always ending with the
  new message. No summary.

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
    HISTORY_POLICY_LABEL,
    HISTORY_POLICY_NONE,
    HISTORY_POLICY_RECENT,
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
# backward budget scan below O(n^2) in `count_tokens` calls; the contract's
# own p95/timeout budget (500ms / 2s) means we should never spend that on a
# single turn. Recent items overwhelmingly decide the outcome anyway, so
# scanning further back than this can't change which items fit the budget
# for any realistic max_input_tokens.
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


def select_history_refs(
    items: list[dict[str, Any]],
    *,
    new_item_id: str,
    policy: str,
    max_input_tokens: int,
    model: str,
) -> tuple[list[str], bool]:
    """Pick which item ids belong in ``history.items`` (contract §3/§6/§7).

    :param items: This session's record, chronological (oldest first), as
        flat ``ConversationItem.to_api_dict()`` dicts.
    :param new_item_id: The id of the new user message. Contract §6.2:
        "every ref exists in this session's record" — when this id is
        missing from *items* there is no safe anchor, so the selection is
        empty (fail-closed at the caller, which always has at least the raw
        new message to fall back to).
    :param policy: ``"none"`` (only the new message) or ``"recent"`` (the
        most recent items that fit the budget). Any other value is treated
        as ``"recent"``, the contract's default.
    :param max_input_tokens: Budget from the request (contract §2).
    :param model: Harness model id, used only to pick a tokenizer encoding
        for the estimate (contract §3's `estimated_tokens` is explicitly an
        estimate, not authoritative).
    :returns: ``(ordered item ids oldest-to-newest, still_over_budget)``.
        ``still_over_budget`` is ``True`` only when the new message alone
        already exceeds the budget — every other case fits by construction
        (older items are dropped until it does).
    """
    if new_item_id not in {item.get("id") for item in items}:
        return [], False

    if policy == HISTORY_POLICY_NONE:
        anchor = _item_by_id(items, new_item_id)
        selected = [anchor] if anchor is not None else []
        over = bool(selected) and count_tokens(selected, model) > max_input_tokens
        return [new_item_id], over

    # HISTORY_POLICY_RECENT (default): walk backward from the new message,
    # keeping the most recent items that still fit — oldest dropped first.
    new_index = next(i for i, item in enumerate(items) if item.get("id") == new_item_id)
    window = items[: new_index + 1][-_MAX_SCAN_ITEMS:]

    selected: list[dict[str, Any]] = []
    for item in reversed(window):
        candidate = [item, *selected]
        # The new message itself (selected == []) is never dropped, even
        # over budget — the contract's own proving test sends it alone.
        if selected and count_tokens(candidate, model) > max_input_tokens:
            break
        selected = candidate
    over_budget = count_tokens(selected, model) > max_input_tokens if selected else False
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
        than requiring the caller to always fetch) so a caller that only
        needs the "none" test-hook policy can skip the fetch entirely — see
        the lazy-call ordering below.
    :param agent_instructions: The bound agent's raw instructions text (the
        default policy's `system.text`), or ``None`` for an agent with none.
    :returns: The `AssembleResponse`. Never raises for a well-formed request
        with a working *items_provider* — deterministic per contract §6.1.
    :raises Exception: Whatever *items_provider* raises. Callers must treat
        that (and a timeout around this whole call) as "fail closed" per
        contract §7 — see :func:`assemble_or_fail_closed`.
    """
    labels = request.session.labels
    history_policy = labels.get(HISTORY_POLICY_LABEL, HISTORY_POLICY_RECENT)
    memory_fixture = labels.get(MEMORY_FIXTURE_LABEL)

    system_text = agent_instructions or ""
    system = SystemBlock(mode="append", text=system_text, digest=_digest(system_text))

    memory_items: list[MemoryItem] = []
    if memory_fixture:
        memory_items = [
            MemoryItem(id="mem_fixture", kind="fact", text=memory_fixture, updated_at=int(time.time()))
        ]
    memory = MemoryBlock(
        items=memory_items,
        digest=_digest([item.model_dump() for item in memory_items]),
    )

    # "none" never needs the full record — the anchor is the new item itself,
    # which the request already names. Skip the (potentially large) fetch.
    if history_policy == HISTORY_POLICY_NONE:
        refs: list[str] = [request.new_item_id]
        estimated_tokens = 0
    else:
        items = items_provider()
        refs, _ = select_history_refs(
            items,
            new_item_id=request.new_item_id,
            policy=history_policy,
            max_input_tokens=request.budget.max_input_tokens,
            model=request.harness.model,
        )
        selected_items = [i for i in (_item_by_id(items, r) for r in refs) if i is not None]
        estimated_tokens = count_tokens(selected_items, request.harness.model) if selected_items else 0

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
    return AssembleResponse(turn_id=request.turn_id, system=system, memory=memory, history=history, audit=audit)


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
        return assemble(request, items_provider=items_provider, agent_instructions=agent_instructions)
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
            audit=AssembleAudit(memory_items=0, history_items=1, summary=False, estimated_tokens=0, fallback=True),
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
