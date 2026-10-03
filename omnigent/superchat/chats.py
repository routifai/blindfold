"""Side Chats: open, discover, and auto-archive them.

A Side Chat is a short-lived branch of the Super Chat (``rollover/CONTEXT.md``).
Only the Super Chat may open one — never a Side Chat, never a Sub-agent — and
it starts ``with_context`` (a rollover fork, seeded from the parent's
checkpoint) or ``blank`` (a fresh top-level session carrying the mode label
and the same discovery labels, with no copied transcript). Side Chats are
archived after inactivity, never deleted, and unarchive on the next user
message (see :func:`maybe_unarchive_on_user_message`).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable, Mapping
from contextlib import suppress

from omnigent.context.labels import (
    CONTEXT_MODE_LABEL,
    SUPERSIDE_CHAT_MODE_VALUE,
    is_superside_chat,
)
from omnigent.stores.conversation_store import (
    FORK_SOURCE_LABEL_KEY,
    SIDE_CHAT_LABEL_KEY,
    ConversationStore,
)

_logger = logging.getLogger(__name__)

# ── side_chat_open: eligibility + request bodies ──────────────────────────

SIDE_CHAT_START_WITH_CONTEXT = "with_context"
SIDE_CHAT_START_BLANK = "blank"
SIDE_CHAT_START_VALUES = frozenset({SIDE_CHAT_START_WITH_CONTEXT, SIDE_CHAT_START_BLANK})


def refuse_side_chat_open(
    *,
    labels: Mapping[str, str] | None,
    kind: str | None,
    parent_session_id: str | None,
) -> str | None:
    """Return why ``side_chat_open`` must be refused, or ``None`` if allowed.

    Side Chats branch only from the Super Chat (``rollover/CONTEXT.md``
    Relationships): never from another Side Chat, never from a Sub-agent.
    Tool registration already gates ``side_chat_open`` to superside-chat
    sessions (:class:`omnigent.tools.manager.ToolManager`); this call-time
    check refuses the two callers that pass that gate but aren't the Super
    Chat itself.

    :param labels: The calling session's own labels.
    :param kind: The calling session's ``Conversation.kind`` (``"sub_agent"``
        for a Sub-agent).
    :param parent_session_id: The calling session's
        ``parent_conversation_id``, when any — set for every Sub-agent.
    :returns: An error message, or ``None`` when the caller is the Super
        Chat and may open a Side Chat.
    """
    if not is_superside_chat(labels):
        return "side_chat_open is only available in superside-chat sessions"
    if SIDE_CHAT_LABEL_KEY in (labels or {}):
        return "side_chat_open can only be called from the Super Chat, not from a Side Chat"
    if kind == "sub_agent" or parent_session_id:
        return "side_chat_open can only be called from the Super Chat, not from a Sub-agent"
    return None


def build_side_chat_fork_body(title: str | None) -> dict[str, object]:
    """``POST /v1/sessions/{super_chat_id}/fork`` body for ``with_context``.

    ``side_chat: true`` makes the server stamp :data:`SIDE_CHAT_LABEL_KEY`,
    keep the rollover-mode labels, and — since the source is a rollover
    session — seed the fork from the parent's checkpoint instead of its full
    transcript (``routes_core.fork_session`` / ``_seed_rollover_side_chat``).
    """
    body: dict[str, object] = {"side_chat": True}
    if title:
        body["title"] = title
    return body


def build_side_chat_blank_create_body(
    *,
    agent_id: str,
    title: str | None,
    source_conversation_id: str,
) -> dict[str, object]:
    """``POST /v1/sessions`` body for a ``blank`` Side Chat.

    Not a fork: a fresh top-level session with no copied transcript, bound
    to the same agent as the Super Chat. Stamped with the same two labels
    ``list_related_chats`` (``omnigent/context/rollover.py``) reads to
    discover a Side Chat — :data:`FORK_SOURCE_LABEL_KEY` pointing at the
    Super Chat plus :data:`SIDE_CHAT_LABEL_KEY` — so a blank Side Chat is
    just as discoverable from ``session_history``'s ``list_chats`` as a
    forked one, plus the superside-chat mode label so it carries the same
    capability.
    """
    body: dict[str, object] = {
        "agent_id": agent_id,
        "labels": {
            CONTEXT_MODE_LABEL: SUPERSIDE_CHAT_MODE_VALUE,
            SIDE_CHAT_LABEL_KEY: "1",
            FORK_SOURCE_LABEL_KEY: source_conversation_id,
        },
    }
    if title:
        body["title"] = title
    return body


# ── Auto-archive after inactivity ─────────────────────────────────────────

ARCHIVE_AFTER_SECONDS_ENV = "OMNIGENT_SIDE_CHAT_ARCHIVE_AFTER_SECONDS"
#: 30 days in production. Set to 3600 (one hour) for testing.
DEFAULT_ARCHIVE_AFTER_SECONDS = 30 * 24 * 60 * 60


def resolve_archive_after_seconds() -> int:
    """Resolve the Side Chat inactivity period before auto-archive.

    Reads :data:`ARCHIVE_AFTER_SECONDS_ENV`, default
    :data:`DEFAULT_ARCHIVE_AFTER_SECONDS` (30 days); set it to ``3600`` (one
    hour) for testing. A missing, non-integer, or non-positive value falls
    back to the default rather than archiving everything on the next sweep.
    """
    raw = os.environ.get(ARCHIVE_AFTER_SECONDS_ENV)
    if raw is None:
        return DEFAULT_ARCHIVE_AFTER_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_ARCHIVE_AFTER_SECONDS
    return value if value > 0 else DEFAULT_ARCHIVE_AFTER_SECONDS


_DEFAULT_SWEEP_PAGE_SIZE = 200
#: Safety cap on conversations scanned per sweep call so one call never
#: runs unbounded even in a workspace far larger than the inactivity window
#: would suggest.
_SWEEP_MAX_SCANNED = 5_000


def is_stale_side_chat(
    *,
    labels: Mapping[str, str] | None,
    archived: bool,
    updated_at: int,
    now: int,
    after_seconds: int,
) -> bool:
    """Whether a conversation is a Side Chat due for auto-archive.

    Never the Super Chat (no :data:`SIDE_CHAT_LABEL_KEY`) and never an
    already-archived chat (re-archiving would be a no-op write).
    """
    if archived:
        return False
    if not is_superside_chat(labels):
        return False
    if SIDE_CHAT_LABEL_KEY not in (labels or {}):
        return False
    return (now - updated_at) >= after_seconds


def sweep_side_chats(
    conv_store: ConversationStore,
    *,
    now: int | None = None,
    after_seconds: int | None = None,
    page_size: int = _DEFAULT_SWEEP_PAGE_SIZE,
) -> int:
    """Archive Side Chats idle for ``after_seconds``; never the Super Chat.

    A cheap, bounded sweep: pages ``conv_store.list_conversations`` oldest
    ``updated_at`` first, so stale candidates sort first, and stops as soon
    as a row's own idle time is under the threshold — every later row (by
    sort order) is even fresher, so none of them can be stale either.  Only
    Side Chats (:data:`SIDE_CHAT_LABEL_KEY`) are archived; a Sub-agent or the
    Super Chat itself never matches. Archiving reuses the existing
    mechanism (``ConversationStore.update_conversation(archived=True)``,
    which stamps ``ARCHIVED_AT_LABEL_KEY``) — Side Chats are archived, never
    deleted.

    :param conv_store: Store to sweep, in the caller's current workspace
        scope.
    :param now: Reference time (epoch seconds); ``time.time()`` when
        ``None``.
    :param after_seconds: Inactivity threshold;
        :func:`resolve_archive_after_seconds` when ``None``.
    :param page_size: Conversations fetched per page.
    :returns: Number of Side Chats archived.
    """
    reference_time = int(time.time()) if now is None else now
    threshold = resolve_archive_after_seconds() if after_seconds is None else after_seconds
    archived_count = 0
    scanned = 0
    after: str | None = None
    while scanned < _SWEEP_MAX_SCANNED:
        page = conv_store.list_conversations(
            limit=page_size,
            after=after,
            kind="default",
            order="asc",
            sort_by="updated_at",
            include_archived=False,
        )
        if not page.data:
            break
        for conversation in page.data:
            scanned += 1
            if (reference_time - conversation.updated_at) < threshold:
                # Ascending by updated_at: every remaining row (this page
                # and every later page) is at least this fresh. Done.
                return archived_count
            if is_stale_side_chat(
                labels=conversation.labels,
                archived=conversation.archived,
                updated_at=conversation.updated_at,
                now=reference_time,
                after_seconds=threshold,
            ):
                conv_store.update_conversation(conversation.id, archived=True)
                archived_count += 1
        if not page.has_more:
            break
        after = page.data[-1].id
    return archived_count


def maybe_unarchive_on_user_message(
    conv_store: ConversationStore,
    conversation_id: str,
) -> bool:
    """Unarchive a Side Chat that just received a new user message.

    Decision (documented, not just implemented): a message to an archived
    Side Chat unarchives it — the user is resuming it on purpose, and
    Archived only means "hidden from the list, not ended"
    (``rollover/CONTEXT.md``, **Archived**). No-op for the Super Chat, a
    Sub-agent, an already-active Side Chat, or an unknown conversation.

    :returns: ``True`` iff this call unarchived the conversation.
    """
    conversation = conv_store.get_conversation(conversation_id)
    if conversation is None or not conversation.archived:
        return False
    if SIDE_CHAT_LABEL_KEY not in conversation.labels:
        return False
    conv_store.update_conversation(conversation_id, archived=False)
    return True


_DEFAULT_SWEEP_INTERVAL_SECONDS = 300.0


class SideChatArchiveSweeper:
    """Periodic loop that archives idle Side Chats (never the Super Chat).

    Mirrors the minimal start/shutdown shape of
    :class:`omnigent.server.managed_sandbox_reaper.ManagedSandboxReaper`: a
    single background task, cheap bounded work per tick, failures logged
    and retried rather than propagated.
    """

    def __init__(
        self,
        conv_store: ConversationStore,
        *,
        sweep_interval_s: float = _DEFAULT_SWEEP_INTERVAL_SECONDS,
        clock: Callable[[], int] = lambda: int(time.time()),
    ) -> None:
        self._conv_store = conv_store
        self._sweep_interval_s = sweep_interval_s
        self._clock = clock
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start this server process's sweep loop."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="side-chat-archive-sweeper")

    async def shutdown(self) -> None:
        """Stop the sweep loop and wait for cancellation to settle."""
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    def sweep_once(self) -> int:
        """Run one sweep and return the number of Side Chats archived."""
        return sweep_side_chats(self._conv_store, now=self._clock())

    async def _run(self) -> None:
        while True:
            try:
                archived = await asyncio.to_thread(self.sweep_once)
                if archived:
                    _logger.info("Side chat archive sweeper archived %s side chat(s)", archived)
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("Side chat archive sweep failed; retrying later")
            await asyncio.sleep(self._sweep_interval_s)
