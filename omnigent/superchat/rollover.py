"""Rollover for superside-chat sessions on the Claude SDK engine (S2).

A superside-chat session (Super Chat, Side Chat, or Sub-agent alike) rolls
over at two points — see ``rollover/SUPERSIDE-CHAT-PLAN.md`` S2 and the
**Rollover** term in ``rollover/CONTEXT.md``:

- the engine-reported context size crosses :func:`~omnigent.context.rollover.
  resolve_rollover_threshold` after a turn (:func:`should_roll_over_for_threshold`);
- the session's first message after being idle past
  :func:`resolve_idle_refresh_seconds` (:func:`should_roll_over_for_idle`).

Either trigger runs the same action (:func:`roll_over_session`): build the
next rollover item over this session's record since the latest ``compaction``
item (reusing :func:`~omnigent.context.rollover.build_rollover_item`'s
progressive summarization), POST it as a new ``compaction`` item, then signal
the caller to drop the warm engine client so the next turn cold-starts from
it (``ADR 0001``: Omnigent decides when, the engine only runs the model loop).
Never during a turn — both call sites in ``inner/claude_sdk_executor.py`` and
``runner/app.py`` trigger only between turns.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx

from omnigent.context.labels import is_superside_chat
from omnigent.context.rollover import (
    build_rollover_item,
    resolve_keep_tokens,
    resolve_rollover_threshold,
)
from omnigent.entities import CompactionData

logger = logging.getLogger(__name__)

#: Idle period before a superside-chat session refreshes on return. Lower it
#: (e.g. a few seconds) for testing.
OMNIGENT_ROLLOVER_IDLE_REFRESH_SECONDS_ENV = "OMNIGENT_ROLLOVER_IDLE_REFRESH_SECONDS"

#: 12 hours.
DEFAULT_IDLE_REFRESH_SECONDS = 43_200


def resolve_idle_refresh_seconds() -> int:
    """Resolve the idle period before a superside-chat session refreshes on return.

    Reads :data:`OMNIGENT_ROLLOVER_IDLE_REFRESH_SECONDS_ENV`, default
    :data:`DEFAULT_IDLE_REFRESH_SECONDS` (12 hours). A missing, non-integer,
    or non-positive value falls back to the default.
    """
    raw = os.environ.get(OMNIGENT_ROLLOVER_IDLE_REFRESH_SECONDS_ENV)
    if raw is None:
        return DEFAULT_IDLE_REFRESH_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_IDLE_REFRESH_SECONDS
    return value if value > 0 else DEFAULT_IDLE_REFRESH_SECONDS


def should_roll_over_for_threshold(
    labels: Mapping[str, str] | None,
    *,
    context_tokens: int | None,
    model_window: int | None = None,
) -> bool:
    """Whether a superside-chat turn's reported context size hit the rollover threshold.

    ``False`` for a non-superside-chat session or when *context_tokens* is
    ``None`` (the turn carried no window-fill signal).

    :param labels: The session's labels.
    :param context_tokens: The turn's reported window fill (e.g.
        ``usage.context_tokens`` from a claude-sdk ``ResultMessage``), or
        ``None``.
    :param model_window: The session's model's context window, forwarded to
        :func:`~omnigent.context.rollover.resolve_rollover_threshold`.
    """
    if not is_superside_chat(labels) or context_tokens is None:
        return False
    return context_tokens >= resolve_rollover_threshold(labels, model_window=model_window)


def should_roll_over_for_idle(
    labels: Mapping[str, str] | None,
    *,
    idle_seconds: float | None,
) -> bool:
    """Whether a superside-chat session has been idle long enough to refresh on return.

    ``False`` for a non-superside-chat session or when *idle_seconds* is
    ``None`` (no prior activity to compare against — e.g. the session's
    first ever turn).

    :param labels: The session's labels.
    :param idle_seconds: Seconds since the session's last recorded activity,
        or ``None``.
    """
    if not is_superside_chat(labels) or idle_seconds is None:
        return False
    return idle_seconds >= resolve_idle_refresh_seconds()


# ---------------------------------------------------------------------------
# Build + persist the next rollover item
# ---------------------------------------------------------------------------

# Sessions with a rollover build+post currently in flight. Module-level,
# process-lifetime: a second trigger for the same session (the threshold
# check and the idle-refresh check can both fire around the same turn) is a
# no-op rather than racing the first — see :func:`roll_over_session`.
_in_flight: set[str] = set()


async def _fetch_items_since_last_compaction(
    server_client: httpx.AsyncClient,
    conversation_id: str,
) -> tuple[list[dict[str, Any]], str | None]:
    """Page this session's full record and split it at the latest compaction item.

    Mirrors the pagination ``_load_history_as_input`` (``runner/app.py``)
    uses to cold-start a session, kept local here so this module has no
    dependency on the runner's closures.

    :returns: ``(items_since, previous_summary)`` — the chronological items
        after the latest ``compaction`` item (the whole record when there is
        none), and that item's summary text (``None`` for the session's
        first rollover).
    """
    items: list[dict[str, Any]] = []
    after: str | None = None
    while True:
        params: dict[str, str] = {"limit": "100", "order": "asc"}
        if after is not None:
            params["after"] = after
        resp = await server_client.get(
            f"/v1/sessions/{conversation_id}/items", params=params, timeout=10.0
        )
        if resp.status_code != 200:
            break
        page = resp.json()
        page_items = page.get("data", [])
        if not page_items:
            break
        items.extend(page_items)
        after = page_items[-1].get("id")
        if not page.get("has_more", False):
            break
    last_compaction_index = next(
        (i for i in range(len(items) - 1, -1, -1) if items[i].get("type") == "compaction"),
        None,
    )
    if last_compaction_index is None:
        return items, None
    checkpoint = items[last_compaction_index]
    return items[last_compaction_index + 1 :], checkpoint.get("summary")


def _compaction_event_body(compaction: CompactionData) -> dict[str, Any]:
    """Shape a :class:`CompactionData` into the ``POST .../events`` ``data`` body.

    Matches the shape ``runner/app.py``'s ``_handle_harness_compaction``
    posts for a native CLI's own compaction, so both producers write the
    same ``compaction`` item shape.
    """
    body: dict[str, Any] = {
        "type": "compaction",
        "summary": compaction.summary,
        "last_item_id": compaction.last_item_id,
        "model": compaction.model,
        "token_count": compaction.token_count,
    }
    if compaction.compacted_messages:
        body["compacted_messages"] = compaction.compacted_messages
    return body


async def roll_over_session(
    conversation_id: str,
    *,
    labels: Mapping[str, str] | None,
    model: str,
    server_client: httpx.AsyncClient,
    llm_client: Any = None,
    connection: dict[str, str] | None = None,
    on_rolled_over: Callable[[], Awaitable[None]] | None = None,
) -> bool:
    """Build and persist the next rollover compaction item for *conversation_id*.

    Fetches this session's record since the latest ``compaction`` item
    (progressive summarization reuses that item's summary —
    :func:`~omnigent.context.rollover.build_rollover_item`), builds the next
    rollover item, and POSTs it as a new ``compaction`` item
    (``POST /v1/sessions/{id}/events``). On success, calls *on_rolled_over* —
    the caller's hook to drop the warm engine client so the next turn
    cold-starts from the new item (e.g. releasing the claude-sdk harness
    subprocess, which runs ``ClaudeSDKExecutor.close_session`` on teardown).

    Single-flight per *conversation_id*: a call that arrives while another is
    already building for the same session returns ``False`` immediately
    rather than racing it. Every failure (history load, summarization, the
    POST, or *on_rolled_over* itself) is logged and swallowed — the turn that
    triggered this call has already been delivered, so a rollover failure
    must never fail it.

    :returns: ``True`` iff a new compaction item was posted.
    """
    if conversation_id in _in_flight:
        return False
    _in_flight.add(conversation_id)
    try:
        try:
            items_since, previous_summary = await _fetch_items_since_last_compaction(
                server_client, conversation_id
            )
        except (httpx.HTTPError, ValueError):
            logger.warning(
                "superside-chat rollover: failed to load history for %s",
                conversation_id,
                exc_info=True,
            )
            return False
        if not items_since:
            return False
        try:
            compaction = await build_rollover_item(
                items_since,
                previous_summary=previous_summary,
                keep_tokens=resolve_keep_tokens(labels),
                model=model,
                llm_client=llm_client,
                connection=connection,
                conversation_id=conversation_id,
            )
        except Exception:  # noqa: BLE001 — a rollover failure must never fail the turn
            logger.warning(
                "superside-chat rollover: failed to build the rollover item for %s",
                conversation_id,
                exc_info=True,
            )
            return False
        try:
            await server_client.post(
                f"/v1/sessions/{conversation_id}/events",
                json={"type": "compaction", "data": _compaction_event_body(compaction)},
                timeout=10.0,
            )
        except (httpx.HTTPError, RuntimeError):
            logger.warning(
                "superside-chat rollover: failed to persist the rollover item for %s",
                conversation_id,
                exc_info=True,
            )
            return False
        if on_rolled_over is not None:
            try:
                await on_rolled_over()
            except Exception:  # noqa: BLE001 — the item is already persisted; log and move on
                logger.warning(
                    "superside-chat rollover: failed to drop the warm client for %s",
                    conversation_id,
                    exc_info=True,
                )
        return True
    finally:
        _in_flight.discard(conversation_id)
