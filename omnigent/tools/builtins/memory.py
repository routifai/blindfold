"""Built-in tools: long-term memory (Phase 1 of ``rollover/MEMORY-PLAN.md``).

Five Muse-style tools backed by :class:`~omnigent.memory.service.MemoryService`:
``memory_remember`` (write), ``memory_search``, ``memory_get``,
``memory_explain`` (read), and ``memory_forget`` (two-step write). Like
``session_history``, they are auto-registered only for rollover sessions
(see ``ToolManager._register_memory_tools``) and scoped from
:class:`ToolContext` — the user is resolved from the calling session's
owner, never taken from the model's arguments.
"""

from __future__ import annotations

import json
from typing import Any

from omnigent.tools.base import Tool, ToolContext
from omnigent.tools.builtins._arguments import parse_json_object_arguments

_SEARCH_DEFAULT_LIMIT = 10
_SEARCH_MAX_LIMIT = 20

_KIND_ENUM = (
    "preference",
    "instruction",
    "fact",
    "decision",
    "person",
    "project",
    "working_style",
)


def resolve_memory_user(ctx: ToolContext) -> tuple[str | None, str | None]:
    """Resolve the user memory is scoped to from ``ctx``, never from arguments.

    Mirrors ``session_history``'s session-from-context scoping: the owner of
    the calling session is memory's "the user" — the same identity across
    every session they use, regardless of who else can see a shared chat.
    A session with no owner grant (no permission store configured — a
    single-user server) falls back to the reserved single-user identity, the
    same sentinel the scheduled-task fire path resolves ``None`` to.

    :returns: ``(user_id, error)`` — exactly one is ``None``.
    """
    if not ctx.conversation_id:
        return None, "memory tools have no calling session in context"
    from omnigent.runtime import get_conversation_store
    from omnigent.server.auth import RESERVED_USER_LOCAL

    user_id = get_conversation_store().get_session_owner(ctx.conversation_id)
    return user_id or RESERVED_USER_LOCAL, None


def _get_service(ctx: ToolContext) -> tuple[Any, str | None]:
    from omnigent.runtime import get_memory_service

    service = get_memory_service()
    if service is None:
        return None, "long-term memory is not configured on this server"
    return service, None


class MemoryRememberTool(Tool):
    """Write a durable claim about the calling session's user, indexed immediately."""

    @classmethod
    def name(cls) -> str:
        return "memory_remember"

    @classmethod
    def description(cls) -> str:
        return (
            "Save a durable fact, preference, instruction, or decision about "
            "the user so it is recalled in future sessions. Call this when "
            "the user states something that should persist (a preference, a "
            "standing instruction, a decision) or asks you to remember it. "
            'Say "I\'ll remember that" only after this call succeeds. Never '
            "store secrets or credentials. A near-duplicate of an existing "
            "claim is reinforced (not duplicated); a claim that contradicts "
            "an existing one about the same thing supersedes it."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "One self-contained sentence describing the claim.",
                        },
                        "kind": {
                            "type": "string",
                            "enum": list(_KIND_ENUM),
                            "description": "What kind of claim this is. Defaults to 'fact'.",
                        },
                        "quote": {
                            "type": "string",
                            "description": "The user's exact words this claim is drawn from.",
                        },
                        "replaces_claim_id": {
                            "type": "string",
                            "description": (
                                "When this corrects or updates an existing memory, its claim_id "
                                "(find it with memory_search first). The old claim is kept as "
                                "superseded."
                            ),
                        },
                    },
                    "required": ["text"],
                    "additionalProperties": False,
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        args, error = parse_json_object_arguments(arguments)
        if error is not None:
            return json.dumps({"error": error})
        assert args is not None
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            return json.dumps({"error": "text must be a non-empty string"})

        user_id, user_error = resolve_memory_user(ctx)
        if user_error is not None:
            return json.dumps({"error": user_error})
        service, service_error = _get_service(ctx)
        if service_error is not None:
            return json.dumps({"error": service_error})

        from omnigent.entities import MemoryEvidenceLink

        evidence = [MemoryEvidenceLink(session_id=ctx.conversation_id or "", item_id="")]
        result = service.remember(
            user_id,
            text.strip(),
            kind=args.get("kind"),
            quote=args.get("quote"),
            evidence=evidence,
            replaces_claim_id=args.get("replaces_claim_id") or None,
        )
        return json.dumps(result)


class MemorySearchTool(Tool):
    """Hybrid search over the calling session's user's active claims."""

    @classmethod
    def name(cls) -> str:
        return "memory_search"

    @classmethod
    def description(cls) -> str:
        return (
            "Search what is known about the user from prior sessions — "
            "preferences, instructions, facts, decisions, people, projects, "
            "working style. Call this before answering anything about prior "
            "work, decisions, dates, people, or preferences, and before "
            "recommending anything. Returns claims ranked by relevance and "
            "confidence, each with when it was last confirmed and where it "
            "came from."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "The search query."},
                        "kind": {
                            "type": "string",
                            "enum": list(_KIND_ENUM),
                            "description": "Restrict results to one kind of claim.",
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "description": (
                                f"Max results, default {_SEARCH_DEFAULT_LIMIT}, "
                                f"maximum {_SEARCH_MAX_LIMIT}."
                            ),
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        args, error = parse_json_object_arguments(arguments)
        if error is not None:
            return json.dumps({"error": error})
        assert args is not None
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return json.dumps({"error": "query must be a non-empty string"})
        from omnigent.tools.builtins.session_history import clamp_limit

        limit = clamp_limit(
            args.get("limit"), default=_SEARCH_DEFAULT_LIMIT, maximum=_SEARCH_MAX_LIMIT
        )
        if isinstance(limit, str):
            return limit
        kind = args.get("kind")
        if kind is not None and kind not in _KIND_ENUM:
            return json.dumps({"error": f"kind must be one of {list(_KIND_ENUM)}"})

        user_id, user_error = resolve_memory_user(ctx)
        if user_error is not None:
            return json.dumps({"error": user_error})
        service, service_error = _get_service(ctx)
        if service_error is not None:
            return json.dumps({"error": service_error})

        results = service.search(user_id, query.strip(), kind=kind, limit=limit)
        return json.dumps({"results": results})


class MemoryGetTool(Tool):
    """Fetch one claim by id, scoped to the calling session's user."""

    @classmethod
    def name(cls) -> str:
        return "memory_get"

    @classmethod
    def description(cls) -> str:
        return "Fetch one remembered claim by its claim_id (from memory_search results)."

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "claim_id": {"type": "string", "description": "The claim id to fetch."},
                    },
                    "required": ["claim_id"],
                    "additionalProperties": False,
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        args, error = parse_json_object_arguments(arguments)
        if error is not None:
            return json.dumps({"error": error})
        assert args is not None
        claim_id = args.get("claim_id")
        if not isinstance(claim_id, str) or not claim_id:
            return json.dumps({"error": "claim_id must be a non-empty string"})

        user_id, user_error = resolve_memory_user(ctx)
        if user_error is not None:
            return json.dumps({"error": user_error})
        service, service_error = _get_service(ctx)
        if service_error is not None:
            return json.dumps({"error": service_error})

        claim = service.get(user_id, claim_id)
        if claim is None:
            return json.dumps({"error": "claim not found"})
        return json.dumps(claim)


class MemoryExplainTool(Tool):
    """Evidence and supersession chain for one claim."""

    @classmethod
    def name(cls) -> str:
        return "memory_explain"

    @classmethod
    def description(cls) -> str:
        return (
            "Explain where a remembered claim came from and what it "
            "replaced or was replaced by — the evidence quote, source "
            "session, and supersession chain."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "claim_id": {"type": "string", "description": "The claim id to explain."},
                    },
                    "required": ["claim_id"],
                    "additionalProperties": False,
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        args, error = parse_json_object_arguments(arguments)
        if error is not None:
            return json.dumps({"error": error})
        assert args is not None
        claim_id = args.get("claim_id")
        if not isinstance(claim_id, str) or not claim_id:
            return json.dumps({"error": "claim_id must be a non-empty string"})

        user_id, user_error = resolve_memory_user(ctx)
        if user_error is not None:
            return json.dumps({"error": user_error})
        service, service_error = _get_service(ctx)
        if service_error is not None:
            return json.dumps({"error": service_error})

        explanation = service.explain(user_id, claim_id)
        if explanation is None:
            return json.dumps({"error": "claim not found"})
        return json.dumps(explanation)


class MemoryForgetTool(Tool):
    """Two-step forget: plan, then confirm."""

    @classmethod
    def name(cls) -> str:
        return "memory_forget"

    @classmethod
    def description(cls) -> str:
        return (
            "Forget a remembered claim, by claim_id or by a search query "
            "matching it. Two steps: call with confirm=false (or omitted) "
            "first and show the user what would be forgotten; only call "
            "again with confirm=true after the user agrees."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "claim_id": {"type": "string", "description": "Claim id to forget."},
                        "query": {
                            "type": "string",
                            "description": (
                                "Search query identifying the claim, if claim_id is unknown."
                            ),
                        },
                        "confirm": {
                            "type": "boolean",
                            "description": "Set true only after the user confirmed the plan.",
                        },
                    },
                    "additionalProperties": False,
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        args, error = parse_json_object_arguments(arguments)
        if error is not None:
            return json.dumps({"error": error})
        assert args is not None
        claim_id = args.get("claim_id")
        query = args.get("query")
        confirm = bool(args.get("confirm", False))
        if claim_id is None and not query:
            return json.dumps({"error": "forget requires claim_id or query"})

        user_id, user_error = resolve_memory_user(ctx)
        if user_error is not None:
            return json.dumps({"error": user_error})
        service, service_error = _get_service(ctx)
        if service_error is not None:
            return json.dumps({"error": service_error})

        result = service.forget(user_id, claim_id=claim_id, query=query, confirm=confirm)
        return json.dumps(result)
