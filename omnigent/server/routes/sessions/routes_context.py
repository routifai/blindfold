"""Context-assembly routes: ``POST /sessions/{id}/context`` (+ ``/observe``).

The successor of "today's /deployment-context" per the context-assembly
contract (``blindfold-mission/context-assembly-contract.md`` §8) — a
blindfolded turn's runner calls ``/context`` before the turn to learn what
the CLI should see, and ``/context/observe`` after the turn to report what
happened. Both routes are thin: they resolve this session's agent
instructions and record, then hand off to the pure
``omnigent.context_assembly`` module (contract §1: "Both run on the server,
... In-process in the Omnigent server first; HTTP later with the same
shapes" — this route *is* that HTTP transport).
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request

from omnigent.context_assembly import (
    AssembleRequest,
    AssembleResponse,
    ObserveRequest,
)
from omnigent.context_assembly import (
    assemble_or_fail_closed as _assemble_or_fail_closed,
)
from omnigent.context_assembly import (
    observe as _observe_turn,
)
from omnigent.context_assembly.assembler import _parse_max_messages
from omnigent.context_assembly.labels import MAX_MESSAGES_LABEL
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime.agent_cache import AgentCache
from omnigent.runtime.prompt import raw_author_instructions
from omnigent.server.auth import LEVEL_READ, AuthProvider
from omnigent.server.routes._auth_helpers import (
    require_access_and_level as _require_access_and_level,
)
from omnigent.server.routes._auth_helpers import (
    require_user as _require_user,
)
from omnigent.server.routes._errors import session_not_found as _session_not_found
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.permission_store import PermissionStore

_logger = logging.getLogger(__name__)

# assemble()'s "recent" policy only ever keeps a budget-bound tail, but it
# needs the full record to find that tail. This caps the fetch so one huge
# session can't turn a per-turn call into an unbounded read; a session with
# more items than this loses only its oldest history to the "recent" window,
# which the budget would have dropped anyway for any realistic budget.
_MAX_FETCH_ITEMS = 2000

# Contract §6.4: "p95 under 500 ms; hard timeout 2 s."
_ASSEMBLE_TIMEOUT_SECONDS = 2.0


def register_context_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    agent_cache: AgentCache | None = None,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> None:
    """Register the context-assembly routes on router."""

    async def _get_conversation_or_404(session_id: str, access: object) -> object:
        conv = getattr(access, "conversation", None)
        if conv is None:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conv is None:
            raise _session_not_found()
        return conv

    async def _fetch_recent_items(session_id: str) -> list[dict]:
        """This session's record, chronological, as flat API-dict items."""
        page = await asyncio.to_thread(
            conversation_store.list_items, session_id, limit=_MAX_FETCH_ITEMS, order="asc"
        )
        return [item.to_api_dict() for item in page.data]

    async def _agent_instructions_for(conv: object) -> str | None:
        """The bound agent's raw instructions text, or ``None``.

        Best-effort: a spec that fails to load falls back to ``None``
        (assemble()'s default policy then sends an empty system text rather
        than 500ing the whole turn — a broken agent bundle must not also
        break blindfold-mode context assembly).
        """
        agent_id = getattr(conv, "agent_id", None)
        if agent_id is None or agent_cache is None:
            return None
        agent = await asyncio.to_thread(agent_store.get, agent_id)
        if agent is None:
            return None
        try:
            loaded = await asyncio.to_thread(
                agent_cache.load,
                agent.id,
                agent.bundle_location,
                expand_env=agent.session_id is None,
            )
        except Exception:
            _logger.warning(
                "Could not load agent spec for context assembly: agent=%s",
                agent_id,
                exc_info=True,
            )
            return None
        return raw_author_instructions(loaded.spec)

    @router.post("/sessions/{session_id}/context", response_model=None)
    async def assemble_session_context(
        request: Request,
        session_id: str,
        body: AssembleRequest,
    ) -> AssembleResponse:
        """
        Assemble what a blindfolded turn's CLI should see (contract §2/§3).

        Read-only and owner-authorized like ``GET /sessions/{id}/items`` —
        this only reads the record to decide what to show the harness; it
        never mutates the session.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation id; must match ``body.session.id``.
        :param body: The turn's :class:`AssembleRequest`.
        :returns: The :class:`AssembleResponse`. Never a 5xx for a well-formed
            body with a valid session — an assembler failure or timeout still
            returns 200 with the fail-closed shape (contract §7); the runner
            reads ``audit.fallback`` to know a turn was flagged.
        :raises OmnigentError: 404 if the session doesn't exist; 400 if
            ``body.session.id`` doesn't match the path.
        """
        user_id = _require_user(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        conv = await _get_conversation_or_404(session_id, access)
        if body.session.id != session_id:
            raise OmnigentError(
                f"body.session.id {body.session.id!r} does not match path {session_id!r}",
                code=ErrorCode.INVALID_INPUT,
            )

        instructions = await _agent_instructions_for(conv)

        # A 1-message window never looks at the record (see
        # assembler.select_history_refs) — skip the fetch entirely so a
        # "blind" proving-test turn costs one DB round trip, not two.
        max_messages = _parse_max_messages(body.session.labels.get(MAX_MESSAGES_LABEL))
        if max_messages <= 1:
            items: list[dict] = []
        else:
            items = await _fetch_recent_items(session_id)

        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    _assemble_or_fail_closed,
                    body,
                    items_provider=lambda: items,
                    agent_instructions=instructions,
                ),
                timeout=_ASSEMBLE_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            # assemble_or_fail_closed() already catches assembler exceptions;
            # this is the other half of contract §7's "Timeout or error" —
            # the assembler ran too long, so build the same fail-closed shape
            # without waiting for it to finish.
            from omnigent.context_assembly.assembler import _digest
            from omnigent.context_assembly.models import (
                AssembleAudit,
                HistoryBlock,
                HistoryItemRef,
                MemoryBlock,
                SystemBlock,
            )

            _logger.warning(
                "context assembly timed out after %.1fs for turn=%s session=%s; failing closed",
                _ASSEMBLE_TIMEOUT_SECONDS,
                body.turn_id,
                session_id,
            )
            text = instructions or ""
            return AssembleResponse(
                turn_id=body.turn_id,
                system=SystemBlock(mode="append", text=text, digest=_digest(text)),
                memory=MemoryBlock(items=[], digest=_digest([])),
                history=HistoryBlock(
                    summary=None,
                    items=[HistoryItemRef(ref=body.new_item_id)],
                    digest=_digest([body.new_item_id]),
                ),
                audit=AssembleAudit(
                    memory_items=0, history_items=1, summary=False, estimated_tokens=0, fallback=True
                ),
            )

    @router.post("/sessions/{session_id}/context/observe", status_code=204, response_model=None)
    async def observe_session_context(
        request: Request,
        session_id: str,
        body: ObserveRequest,
    ) -> None:
        """
        Report a completed turn to the assembler (contract §4).

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation id; must match ``body.session_id``.
        :param body: The turn's :class:`ObserveRequest`.
        :raises OmnigentError: 404 if the session doesn't exist; 400 if
            ``body.session_id`` doesn't match the path.
        """
        user_id = _require_user(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        await _get_conversation_or_404(session_id, access)
        if body.session_id != session_id:
            raise OmnigentError(
                f"body.session_id {body.session_id!r} does not match path {session_id!r}",
                code=ErrorCode.INVALID_INPUT,
            )
        # contract §7: "observe fails -> retry in the background; never
        # blocks the next turn". v0.2's observe() is a synchronous, in-memory
        # log call (no store write yet), so there is nothing to retry.
        _observe_turn(body)
