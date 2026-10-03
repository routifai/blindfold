"""Items and child-session routes."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import (
    APIRouter,
    Query,
    Request,
)

from omnigent.runtime.policies.approval import _ELICITATION_MODE
from omnigent.server._elicitation_registry import (
    _harness_elicitation_owners,
    _harness_elicitation_registry,
    _harness_parked_elicitations,
    _harness_pre_resolved_elicitations,
    _ParkedHarnessElicitation,
    _PreResolvedHarnessElicitation,
)
from omnigent.server.auth import (
    LEVEL_READ,
    AuthProvider,
)
from omnigent.server.routes._auth_helpers import (
    get_user_id as _get_user_id,
)
from omnigent.server.routes._auth_helpers import (
    require_access_and_level as _require_access_and_level,
)
from omnigent.server.routes._errors import (
    STALE_CURSOR_RESPONSE,
)
from omnigent.server.routes._errors import session_not_found as _session_not_found
from omnigent.server.routes._sessions.common import (
    get_server_runner_router,
    set_server_runner_router,
)
from omnigent.server.routes._sessions.orchestration import (
    _child_session_summaries_from_conversations,
)
from omnigent.server.schemas import (
    ChildSessionList,
    PaginatedList,
)
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.superchat.activity import activity_to_dict, get_activity, list_activities


def register_items_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> None:
    """Register the items routes on router."""

    @router.get(
        "/sessions/{session_id}/items",
        response_model=None,
        responses={200: {"model": PaginatedList}, **STALE_CURSOR_RESPONSE},
    )
    async def list_session_items(
        request: Request,
        session_id: str,
        limit: int = Query(default=100, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="asc", pattern="^(asc|desc)$"),
    ) -> PaginatedList:
        """
        List items in a session with cursor-based pagination.

        Delegates to the conversation items store — session_id is
        the conversation_id. Same pagination contract as
        ``GET /v1/conversations/{id}/items``.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param limit: Maximum number of items to return
            (1-1000, default 100).
        :param after: Cursor — return items after this item ID,
            e.g. ``"msg_abc123"``.
        :param before: Cursor — return items before this item ID.
        :param order: Sort order, ``"asc"`` (chronological,
            default) or ``"desc"``.
        :returns: A :class:`PaginatedList` of conversation items.
        :raises OmnigentError: 404 if no session exists.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        if access.conversation is None:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if conv is None:
                raise _session_not_found()
        page = await asyncio.to_thread(
            conversation_store.list_items,
            session_id,
            limit=limit,
            after=after,
            before=before,
            order=order,
        )
        data = [m.to_api_dict() for m in page.data]
        return PaginatedList(
            data=data,
            first_id=page.first_id,
            last_id=page.last_id,
            has_more=page.has_more,
        )

    # ── GET /sessions/{session_id}/items/search ──────────────────
    # Session-scoped full-text search over conversation items — the REST
    # counterpart to ``ConversationStore.search(query, conversation_id=...)``
    # for callers (the runner's native-relay tool dispatch) with no
    # in-process store. Mirrors ``list_session_items`` above, scoped by
    # the same access check.

    @router.get(
        "/sessions/{session_id}/items/search",
        response_model=None,
        responses={200: {"model": PaginatedList}},
    )
    async def search_session_items(
        request: Request,
        session_id: str,
        query: str = Query(min_length=1),
        limit: int = Query(default=10, ge=1, le=20),
    ) -> PaginatedList:
        """
        Full-text search over one session's own conversation items.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param query: The search query string.
        :param limit: Maximum number of results (1-20, default 10).
        :returns: A :class:`PaginatedList` of matching item dicts,
            ranked by relevance (``first_id``/``last_id``/``has_more``
            unset — search results are not cursor-paginated).
        :raises OmnigentError: 404 if no session exists.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        if access.conversation is None:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if conv is None:
                raise _session_not_found()
        items = await asyncio.to_thread(
            conversation_store.search,
            query,
            conversation_id=session_id,
            limit=limit,
        )
        return PaginatedList(data=[m.to_api_dict() for m in items])

    # ── GET /sessions/{session_id}/related_chats ──────────────────
    # Side chats related to session_id for ``session_history``'s
    # ``list_chats`` action: forked FROM it (carrying the side-chat label),
    # plus its own parent when session_id is itself a side chat. Owner-
    # checked by ``list_related_chats`` itself (never trusts the caller's
    # identity beyond the READ check below), the same scoping the runner's
    # native-relay dispatch reaches over this route for.

    @router.get(
        "/sessions/{session_id}/related_chats",
        response_model=None,
        responses={200: {"model": PaginatedList}},
    )
    async def list_related_chats_route(
        request: Request,
        session_id: str,
    ) -> PaginatedList:
        """
        List the side chats related to one session.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: A :class:`PaginatedList` of chat summary dicts
            (``id``, ``title``, ``created_at``, ``updated_at``,
            ``last_message_preview``); not cursor-paginated.
        :raises OmnigentError: 404 if no session exists.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        if access.conversation is None:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if conv is None:
                raise _session_not_found()
        from omnigent.context.rollover import list_related_chats

        chats = await asyncio.to_thread(list_related_chats, conversation_store, session_id)
        return PaginatedList(data=chats)

    # ── GET /sessions/{session_id}/child_sessions ────────────────

    @router.get(
        "/sessions/{session_id}/child_sessions",
        response_model=None,
        responses={200: {"model": ChildSessionList}, **STALE_CURSOR_RESPONSE},
    )
    async def list_child_sessions(
        request: Request,
        session_id: str,
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
        tool: str | None = Query(default=None),
        session_name: str | None = Query(default=None),
    ) -> PaginatedList:
        """
        List sub-agent (child) sessions under a parent session.

        Returns a page of :class:`ChildSessionSummary` objects
        derived from child conversations (``kind="sub_agent"``,
        ``parent_conversation_id=session_id``) plus each child's
        latest task. Powers the web / REPL debug surfaces' "child
        sessions" panel without parsing parent
        ``function_call_output`` JSON handles. Pagination contract
        matches :func:`list_session_items` so existing client code
        can reuse the same cursor logic.

        :param request: Inbound HTTP request; carries the caller
            identity used to authorize READ on the parent session.
        :param session_id: Parent session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param limit: Maximum number of children to return
            (1-1000, default 20 — sub-agent fan-out is typically
            sparse compared to conversation items).
        :param after: Cursor — return children whose id appears
            after this one in sort order,
            e.g. ``"conv_child123"``.
        :param before: Cursor — return children before this one.
        :param order: Sort direction, ``"desc"`` (newest-first,
            default) or ``"asc"``. Sort column is ``created_at``.
        :param tool: When set, only return children whose title
            starts with this agent type (the segment before the
            ``":"``). Combined with ``session_name`` to form the
            exact title ``"{tool}:{session_name}"`` for server-side
            filtering.
        :param session_name: When set alongside ``tool``, only
            return children whose title matches
            ``"{tool}:{session_name}"`` exactly.
        :returns: A :class:`PaginatedList` of
            :class:`ChildSessionSummary` objects.
        :raises OmnigentError: 403 if the caller lacks READ on
            ``session_id``; 404 if no session exists there.
        """
        user_id = _get_user_id(request, auth_provider)
        # Require READ on the parent before listing its children (no cross-user enumeration).
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        parent = access.conversation
        if parent is None:
            parent = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if parent is None:
            raise _session_not_found()
        title_filter: str | None = None
        if tool and session_name:
            title_filter = f"{tool}:{session_name}"
        page = await asyncio.to_thread(
            conversation_store.list_conversations,
            limit=limit,
            after=after,
            before=before,
            kind="sub_agent",
            parent_conversation_id=session_id,
            order=order,
            sort_by="created_at",
            title=title_filter,
        )
        data = await _child_session_summaries_from_conversations(
            page.data,
            session_id,
            conversation_store,
        )
        return PaginatedList(
            data=data,
            first_id=page.first_id,
            last_id=page.last_id,
            has_more=page.has_more,
        )

    # ── GET /sessions/{session_id}/activities ────────────────────
    # The Activity Feed: every Activity under session_id's Super Chat family
    # (the Super Chat itself, its Side Chats, and their Sub-agents). Derived
    # on read from conversations/items — see omnigent/superchat/activity.py.
    # session_id may be the Super Chat or one of its Side Chats.

    @router.get(
        "/sessions/{session_id}/activities",
        response_model=None,
        responses={200: {"model": PaginatedList}},
    )
    async def list_session_activities(
        request: Request,
        session_id: str,
        limit: int = Query(default=20, ge=1, le=100),
        before: int | None = Query(default=None),
    ) -> PaginatedList:
        """
        List the Activity Feed for ``session_id``'s Super Chat family.

        :param session_id: A Super Chat or Side Chat session id.
        :param limit: Maximum Activities to return, newest-first (1-100).
        :param before: Only Activities started strictly before this epoch
            timestamp.
        :returns: A :class:`PaginatedList` of Activity summary dicts (no
            per-step detail — see ``GET .../activities/{activity_id}``),
            each carrying a ``date`` field for day-grouping. Empty when
            ``session_id`` isn't a ``superside-chat`` session.
        :raises OmnigentError: 403 if the caller lacks READ on
            ``session_id``; 404 if no session exists there.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        if access.conversation is None:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if conv is None:
                raise _session_not_found()
        activities = await asyncio.to_thread(
            list_activities,
            conversation_store,
            session_id,
            before=before,
            limit=limit,
        )
        return PaginatedList(data=[activity_to_dict(activity) for activity in activities])

    # ── GET /sessions/{session_id}/activities/{activity_id} ──────
    # One Activity in full: steps carry their capped call/result detail.

    @router.get(
        "/sessions/{session_id}/activities/{activity_id}",
        response_model=None,
    )
    async def get_session_activity(
        request: Request,
        session_id: str,
        activity_id: str,
    ) -> dict[str, Any]:
        """
        Return one Activity with full step detail.

        :param session_id: A Super Chat or Side Chat session id.
        :param activity_id: An id previously returned by
            ``GET .../activities``.
        :returns: The Activity dict, steps including each call's
            arguments/output (capped).
        :raises OmnigentError: 403 if the caller lacks READ on
            ``session_id``; 404 if no session exists there, the session
            isn't a ``superside-chat`` session, or ``activity_id`` doesn't
            resolve within its family.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        if access.conversation is None:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if conv is None:
                raise _session_not_found()
        activity = await asyncio.to_thread(
            get_activity,
            conversation_store,
            session_id,
            activity_id,
        )
        if activity is None:
            raise _session_not_found()
        return activity_to_dict(activity)
