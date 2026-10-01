"""Routes for long-term memory (``rollover/MEMORY-PLAN.md`` Phase 1).

All endpoints live under ``/v1/sessions/{session_id}/memory`` and require at
least ``LEVEL_READ`` on the session, same posture as
``routes_items.search_session_items``. The user memory is scoped to is
**always** the session's owner (``conversation_store.get_session_owner``),
never a value from the request body or query string — the same rule the
``memory_*`` built-in tools apply when called in-process
(``omnigent.tools.builtins.memory.resolve_memory_user``), so native-relay
dispatch (which reaches this API over REST) and in-process dispatch agree on
whose memory is being read or written.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.memory.service import MemoryService
from omnigent.server.auth import LEVEL_READ, RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id, require_access
from omnigent.server.routes._errors import session_not_found
from omnigent.server.schemas import MemoryForgetRequest, MemoryRememberRequest
from omnigent.stores import ConversationStore
from omnigent.stores.permission_store import PermissionStore

_SEARCH_DEFAULT_LIMIT = 10
_SEARCH_MAX_LIMIT = 20


def create_session_memory_router(
    memory_service: MemoryService,
    conversation_store: ConversationStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Build the session memory router.

    :param memory_service: The shared :class:`MemoryService` instance.
    :param conversation_store: Used to check the session exists and to
        resolve the session's owner — the user memory is scoped to.
    :param auth_provider: Auth provider used to identify the requesting
        user for the session-access check. ``None`` in single-user mode.
    :param permission_store: Permission store used to check session-level
        access grants. ``None`` disables permission enforcement.
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()

    async def _resolve_user(request: Request, session_id: str) -> str:
        """Require session access, then resolve the owner memory is scoped to.

        A session with no owner grant (no permission store configured — a
        single-user server) falls back to the reserved single-user identity,
        same as ``omnigent.tools.builtins.memory.resolve_memory_user``.
        """
        user_id = get_user_id(request, auth_provider)
        if permission_store is not None:
            await require_access(
                user_id, session_id, LEVEL_READ, permission_store, conversation_store
            )
        conversation = conversation_store.get_conversation(session_id)
        if conversation is None:
            raise session_not_found()
        return conversation_store.get_session_owner(session_id) or RESERVED_USER_LOCAL

    @router.post("/sessions/{session_id}/memory/remember")
    async def remember(
        request: Request,
        session_id: str,
        body: MemoryRememberRequest,
    ) -> dict[str, Any]:
        """Write a durable claim, immediately indexed; reinforce/supersede a near-duplicate."""
        owner = await _resolve_user(request, session_id)
        from omnigent.entities import MemoryEvidenceLink

        evidence = [MemoryEvidenceLink(session_id=session_id, item_id="")]
        return memory_service.remember(
            owner, body.text, kind=body.kind, quote=body.quote, evidence=evidence
        )

    @router.get("/sessions/{session_id}/memory/search")
    async def search(
        request: Request,
        session_id: str,
        query: str = Query(min_length=1),
        kind: str | None = Query(default=None),
        limit: int = Query(default=_SEARCH_DEFAULT_LIMIT, ge=1, le=_SEARCH_MAX_LIMIT),
    ) -> dict[str, Any]:
        """Hybrid search over the session owner's active claims."""
        owner = await _resolve_user(request, session_id)
        results = memory_service.search(owner, query, kind=kind, limit=limit)
        return {"results": results}

    @router.get("/sessions/{session_id}/memory/claims/{claim_id}")
    async def get_claim(
        request: Request,
        session_id: str,
        claim_id: str,
    ) -> dict[str, Any]:
        """Fetch one claim by id, scoped to the session owner."""
        owner = await _resolve_user(request, session_id)
        claim = memory_service.get(owner, claim_id)
        if claim is None:
            raise OmnigentError("Claim not found", code=ErrorCode.NOT_FOUND)
        return claim

    @router.get("/sessions/{session_id}/memory/claims/{claim_id}/explain")
    async def explain_claim(
        request: Request,
        session_id: str,
        claim_id: str,
    ) -> dict[str, Any]:
        """Evidence and supersession chain for one claim."""
        owner = await _resolve_user(request, session_id)
        explanation = memory_service.explain(owner, claim_id)
        if explanation is None:
            raise OmnigentError("Claim not found", code=ErrorCode.NOT_FOUND)
        return explanation

    @router.post("/sessions/{session_id}/memory/forget")
    async def forget(
        request: Request,
        session_id: str,
        body: MemoryForgetRequest,
    ) -> dict[str, Any]:
        """Two-step forget: ``confirm=false`` plans, ``confirm=true`` executes."""
        owner = await _resolve_user(request, session_id)
        if body.claim_id is None and not body.query:
            raise OmnigentError("forget requires claim_id or query", code=ErrorCode.INVALID_INPUT)
        return memory_service.forget(
            owner, claim_id=body.claim_id, query=body.query, confirm=body.confirm
        )

    return router
