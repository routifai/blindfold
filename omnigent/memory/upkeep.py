"""Phase 3 of ``rollover/MEMORY-PLAN.md``: the memory upkeep job.

The self-improvement loop: window -> gate -> extract -> verify -> apply ->
project -> record (section 4, section 6). Pure pipeline logic lives here;
``omnigent/server/memory_upkeep.py`` wires it to the server's background
scheduling and the compaction trigger.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Protocol

from omnigent.db.utils import now_epoch
from omnigent.entities import MemoryEvidenceLink, MemoryUpkeepRun
from omnigent.memory.service import INFERRED_CONFIDENCE, STATED_CONFIDENCE, VALID_KINDS
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.memory_upkeep_store import MemoryUpkeepStore

_logger = logging.getLogger(__name__)

# ── configurable thresholds (section 6 / "make the interval/thresholds
# configurable constants") ──────────────────────────────────────────────

#: Gate: fewer substantive new user messages than this skips the run
#: (``disposition: "no_new_signal"``), and the watermark does not advance.
GATE_MIN_USER_MESSAGES = 3

#: Window scan bounds — a reference-implementation safety valve, not a
#: correctness requirement: a user with more unread sessions/messages than
#: this just needs another sweep to catch up (the watermark never skips
#: anything, it only moves as far as this run actually read).
WINDOW_SESSIONS_LIMIT = 200
# One extraction call per run: keep the newest messages within this budget
# (~30k tokens) and clip each one, so a long backlog can't overflow the model.
WINDOW_MAX_CHARS = 120_000
WINDOW_ITEM_MAX_CHARS = 2_000
WINDOW_ITEMS_PAGE_SIZE = 50
WINDOW_MAX_ITEMS_PER_SESSION = 500

#: A claim candidate longer than this is rejected as malformed.
MAX_CLAIM_TEXT_CHARS = 500

#: How many existing claims the apply step compares a candidate against.
APPLY_SEARCH_LIMIT = 5

#: Confidence bump on "same" (reinforce), per MEMORY-PLAN.md section 6.
REINFORCE_INCREMENT_STATED = 0.05
REINFORCE_INCREMENT_INFERRED = 0.2

#: Hourly sweep interval (omnigent/server/memory_upkeep.py).
HOURLY_SWEEP_INTERVAL_S = 3600


# ── extraction / classification prompts (module constants, per plan) ────

MEMORY_UPKEEP_EXTRACTION_PROMPT = """\
You are extracting durable long-term memory claims about one user from a \
transcript of their own messages. Assistant replies are shown only for \
conversational context — never extract a claim from them.

Only extract durable things: stated preferences, standing instructions, the \
user's role, recurring people, teams or clients they mention, ongoing \
projects, decisions they made, and working-style signals visible in a \
correction. Never extract one-time requests (e.g. "send this to Bob \
today"), small talk, or secrets (passwords, tokens, API keys, account \
numbers) — leave those out entirely.

Each candidate's "claim_text" must be one self-contained sentence, written \
so it makes sense with no other context. Each candidate's "quote" must be \
the exact words the user wrote, copied verbatim from the cited item — do \
not paraphrase, fix spelling, or add punctuation the user didn't use.

Mark "explicitness" as "stated" when the user said it directly (e.g. "I \
prefer...", "always...", "call me..."), or "inferred" when you are deducing \
it from behavior or a correction rather than a direct statement.

Respond with a JSON object of this exact shape and nothing else:
{"candidates": [{"kind": "preference"|"instruction"|"fact"|"decision"|
"person"|"project"|"working_style", "claim_text": "...", "quote": "...",
"item_id": "...", "explicitness": "stated"|"inferred",
"valid_until": "<ISO date or null>"}]}
Return {"candidates": []} when nothing durable is present in the transcript.
Output JSON only — no markdown, no commentary."""

MEMORY_UPKEEP_CLASSIFY_PROMPT = """\
Decide how a new memory candidate about a user relates to their existing \
memory claims, listed below.

Respond with a JSON object of this exact shape and nothing else:
{"relation": "same"|"new"|"contradicts", "claim_id": "<id or null>"}
Use "same" when the candidate restates or paraphrases one of the listed \
claims with the same meaning — set "claim_id" to that claim's id. Use \
"contradicts" only when the candidate clearly conflicts with one specific \
listed claim — set "claim_id" to that claim's id; never guess at a \
contradiction the text doesn't name. Otherwise use "new" with "claim_id": \
null. Output JSON only — no markdown, no commentary."""

# ── secret / one-time guards (verify step) ───────────────────────────────

_SECRET_PATTERNS = (
    re.compile(r"\b(?:password|passwd|api[_-]?key|secret|token)\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),  # OpenAI-style secret key
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),  # GitHub token
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b"),  # IBAN-like
)

_ONE_TIME_MARKERS = (
    "this time",
    "just this once",
    "just for now",
    "just now",
    "for this report",
    "for this one",
    "today only",
    "only for today",
)


# ── data shapes ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class WindowItem:
    """One message item inside an upkeep run's window.

    :param role: ``"user"`` or ``"assistant"``. Only ``"user"`` items are
        ever evidence; ``"assistant"`` items are context-only (section 2
        of the window step).
    """

    session_id: str
    item_id: str
    role: str
    text: str
    created_at: int


@dataclass(frozen=True)
class VerifiedCandidate:
    """An extraction candidate that passed every deterministic check."""

    kind: str
    claim_text: str
    quote: str
    item_id: str
    session_id: str
    explicitness: str
    valid_until: int | None


class UpkeepLLMCaller(Protocol):
    """One LLM call: a system prompt plus a user turn, returning raw text."""

    async def __call__(self, *, instructions: str, input_text: str) -> str: ...


class UpkeepMemory(Protocol):
    """The subset of :class:`~omnigent.memory.service.MemoryService` the
    apply step needs — a ``Protocol`` so tests can pass a plain fake
    instead of a real (txtai-backed) service."""

    def search(
        self, user_id: str, query: str, *, kind: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]: ...

    def record_claim(
        self,
        user_id: str,
        kind: str,
        text: str,
        *,
        quote: str | None = None,
        speaker: str | None = None,
        evidence: list[MemoryEvidenceLink] | None = None,
        explicitness: str = "stated",
        confidence: float = STATED_CONFIDENCE,
        run_id: str | None = None,
        valid_until: int | None = None,
    ) -> dict[str, Any]: ...

    def reinforce_claim(
        self, claim_id: str, user_id: str, *, confidence_increment: float
    ) -> dict[str, Any] | None: ...

    def supersede_claim(
        self,
        old_claim_id: str,
        user_id: str,
        *,
        kind: str,
        text: str,
        quote: str | None = None,
        speaker: str | None = None,
        evidence: list[MemoryEvidenceLink] | None = None,
        explicitness: str = "stated",
        confidence: float = STATED_CONFIDENCE,
        run_id: str | None = None,
    ) -> dict[str, Any]: ...


# ── 1. window ─────────────────────────────────────────────────────────────


def _role_of(item: Any) -> str | None:
    role = getattr(item.data, "role", None)
    return role if role in ("user", "assistant") else None


def _message_text(item: Any) -> str:
    content = getattr(item.data, "content", None)
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") in ("input_text", "output_text", "text"):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return " ".join(parts)


def _session_window_items(
    conversation_store: ConversationStore,
    session_id: str,
    since: int,
) -> list[WindowItem]:
    """User + assistant message items in *session_id* newer than *since*.

    Pages newest-first and stops as soon as an item at or before the
    watermark is reached — items only get older from there.
    """
    collected: list[WindowItem] = []
    cursor: str | None = None
    while len(collected) < WINDOW_MAX_ITEMS_PER_SESSION:
        page = conversation_store.list_items(
            session_id,
            limit=WINDOW_ITEMS_PAGE_SIZE,
            before=cursor,
            order="desc",
            type="message",
        )
        if not page.data:
            break
        reached_watermark = False
        for item in page.data:
            if item.created_at <= since:
                reached_watermark = True
                break
            role = _role_of(item)
            if role is None:
                continue
            collected.append(
                WindowItem(
                    session_id=session_id,
                    item_id=item.id,
                    role=role,
                    text=_message_text(item),
                    created_at=item.created_at,
                )
            )
        if reached_watermark or not page.has_more:
            break
        cursor = page.last_id
    collected.reverse()  # chronological (ascending) order
    return collected


def gather_window(
    conversation_store: ConversationStore,
    user_id: str,
    since: int,
) -> list[WindowItem]:
    """User-authored message items (plus the assistant turn they reply to)
    across *user_id*'s sessions since *since*. Never tool outputs,
    documents, web content, or assistant text as evidence — the apply step
    enforces that by only ever citing a ``role == "user"`` item.
    """
    items: list[WindowItem] = []
    cursor: str | None = None
    scanned = 0
    while scanned < WINDOW_SESSIONS_LIMIT:
        page = conversation_store.list_conversations(
            owned_by=user_id,
            kind=None,
            limit=min(50, WINDOW_SESSIONS_LIMIT - scanned),
            after=cursor,
            order="desc",
            sort_by="updated_at",
        )
        if not page.data:
            break
        reached_watermark = False
        for conv in page.data:
            scanned += 1
            if conv.updated_at < since:
                reached_watermark = True
                break
            items.extend(_session_window_items(conversation_store, conv.id, since))
        if reached_watermark or not page.has_more:
            break
        cursor = page.last_id
    items.sort(key=lambda i: i.created_at)
    return items


def bound_window(window_items: list[WindowItem]) -> list[WindowItem]:
    """The newest *window_items* within :data:`WINDOW_MAX_CHARS`, each clipped
    to :data:`WINDOW_ITEM_MAX_CHARS`, in chronological order."""
    kept: list[WindowItem] = []
    total = 0
    for item in reversed(window_items):
        clipped = replace(item, text=item.text[:WINDOW_ITEM_MAX_CHARS])
        total += len(clipped.text)
        if total > WINDOW_MAX_CHARS and kept:
            break
        kept.append(clipped)
    kept.reverse()
    return kept


# ── 2. gate ───────────────────────────────────────────────────────────────


def gate(
    window_items: list[WindowItem], *, min_user_messages: int = GATE_MIN_USER_MESSAGES
) -> bool:
    """``True`` when there is enough new signal to bother extracting."""
    substantive = sum(1 for i in window_items if i.role == "user" and len(i.text.strip()) >= 2)
    return substantive >= min_user_messages


# ── 3. extract ────────────────────────────────────────────────────────────


def _strip_code_fence(text: str) -> str:
    """The JSON object in a model reply, even when wrapped in a fence or prose."""
    stripped = text.strip()
    start, end = stripped.find("{"), stripped.rfind("}")
    if start == -1 or end < start:
        return stripped
    return stripped[start : end + 1]


def _format_window_for_extraction(window_items: list[WindowItem]) -> str:
    lines: list[str] = []
    for item in window_items:
        if item.role == "user":
            lines.append(f'[item_id="{item.item_id}"] USER: {item.text}')
        else:
            lines.append(f"ASSISTANT (context only, not evidence): {item.text}")
    return "\n".join(lines)


async def extract_candidates(
    llm_caller: UpkeepLLMCaller, window_items: list[WindowItem]
) -> list[dict[str, Any]]:
    """Call the extraction model; return raw candidate dicts (unverified)."""
    raw_text = await llm_caller(
        instructions=MEMORY_UPKEEP_EXTRACTION_PROMPT,
        input_text=_format_window_for_extraction(window_items),
    )
    try:
        parsed = json.loads(_strip_code_fence(raw_text))
    except (TypeError, ValueError):
        _logger.warning("memory upkeep extraction returned non-JSON output")
        return []
    if not isinstance(parsed, dict):
        return []
    candidates = parsed.get("candidates")
    return candidates if isinstance(candidates, list) else []


# ── 4. verify (deterministic) ────────────────────────────────────────────


def _looks_like_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


def _looks_like_one_time(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _ONE_TIME_MARKERS)


def _normalize_for_verbatim(text: str) -> str:
    return " ".join(text.lower().split())


def _quote_is_verbatim(quote: str, source_text: str) -> bool:
    return _normalize_for_verbatim(quote) in _normalize_for_verbatim(source_text)


def _parse_valid_until(value: Any) -> int | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return int(datetime.fromisoformat(value).timestamp())
    except ValueError:
        return None


def verify_candidate(
    raw: Any, items_by_id: dict[str, WindowItem]
) -> tuple[VerifiedCandidate | None, str | None]:
    """Deterministic checks (section 4). ``(candidate, None)`` on success,
    else ``(None, reason)``."""
    if not isinstance(raw, dict):
        return None, "malformed"
    kind = raw.get("kind")
    claim_text = raw.get("claim_text")
    quote = raw.get("quote")
    item_id = raw.get("item_id")
    explicitness = raw.get("explicitness")
    if not all(
        isinstance(v, str) and v.strip() for v in (kind, claim_text, quote, item_id, explicitness)
    ):
        return None, "malformed"
    assert isinstance(kind, str)
    assert isinstance(claim_text, str)
    assert isinstance(quote, str)
    assert isinstance(item_id, str)
    assert isinstance(explicitness, str)
    if kind not in VALID_KINDS:
        return None, "invalid_kind"
    if explicitness not in ("stated", "inferred"):
        return None, "invalid_explicitness"
    if len(claim_text) > MAX_CLAIM_TEXT_CHARS or len(quote) > MAX_CLAIM_TEXT_CHARS:
        return None, "overlong"
    item = items_by_id.get(item_id)
    if item is None:
        # Not in the window, not owned by this user, or not a user item —
        # the gather_window/items_by_id boundary is the only evidence
        # source, so an unknown id is rejected rather than trusted.
        return None, "unknown_item"
    if _looks_like_secret(quote) or _looks_like_secret(claim_text):
        return None, "secret"
    if _looks_like_one_time(quote):
        return None, "one_time"
    if not _quote_is_verbatim(quote, item.text):
        return None, "quote_not_verbatim"
    return (
        VerifiedCandidate(
            kind=kind,
            claim_text=claim_text.strip(),
            quote=quote,
            item_id=item_id,
            session_id=item.session_id,
            explicitness=explicitness,
            valid_until=_parse_valid_until(raw.get("valid_until")),
        ),
        None,
    )


# ── 5. apply ──────────────────────────────────────────────────────────────


def _starting_confidence(explicitness: str) -> float:
    return STATED_CONFIDENCE if explicitness == "stated" else INFERRED_CONFIDENCE


async def classify_relation(
    llm_caller: UpkeepLLMCaller, candidate: VerifiedCandidate, matches: list[dict[str, Any]]
) -> tuple[str, str | None]:
    """One classification call: how does *candidate* relate to *matches*?"""
    listing = "\n".join(f'- claim_id="{m["claim_id"]}": {m["text"]}' for m in matches)
    input_text = f'Candidate: "{candidate.claim_text}"\n\nExisting claims:\n{listing}'
    raw_text = await llm_caller(instructions=MEMORY_UPKEEP_CLASSIFY_PROMPT, input_text=input_text)
    try:
        parsed = json.loads(_strip_code_fence(raw_text))
    except (TypeError, ValueError):
        return "new", None
    if not isinstance(parsed, dict):
        return "new", None
    relation = parsed.get("relation")
    claim_id = parsed.get("claim_id")
    if relation not in ("same", "new", "contradicts"):
        return "new", None
    return relation, claim_id if isinstance(claim_id, str) else None


async def apply_candidate(
    memory: UpkeepMemory,
    user_id: str,
    candidate: VerifiedCandidate,
    run_id: str,
    llm_caller: UpkeepLLMCaller,
) -> str:
    """Apply one verified candidate. Returns ``"inserted"``, ``"reinforced"``,
    ``"superseded"``, or a rejection reason string."""
    evidence = [MemoryEvidenceLink(session_id=candidate.session_id, item_id=candidate.item_id)]
    confidence = _starting_confidence(candidate.explicitness)
    matches = memory.search(user_id, candidate.claim_text, limit=APPLY_SEARCH_LIMIT)
    if not matches:
        memory.record_claim(
            user_id,
            candidate.kind,
            candidate.claim_text,
            quote=candidate.quote,
            speaker="user",
            evidence=evidence,
            explicitness=candidate.explicitness,
            confidence=confidence,
            run_id=run_id,
            valid_until=candidate.valid_until,
        )
        return "inserted"

    relation, named_claim_id = await classify_relation(llm_caller, candidate, matches)
    matched_ids = {m["claim_id"] for m in matches}

    if relation == "same":
        target_id = str(
            named_claim_id if named_claim_id in matched_ids else matches[0]["claim_id"]
        )
        increment = (
            REINFORCE_INCREMENT_STATED
            if candidate.explicitness == "stated"
            else REINFORCE_INCREMENT_INFERRED
        )
        reinforced = memory.reinforce_claim(target_id, user_id, confidence_increment=increment)
        return "reinforced" if reinforced is not None else "reinforce_target_missing"

    if relation == "contradicts":
        # Never act on a contradiction the classifier didn't name explicitly.
        if named_claim_id is None or named_claim_id not in matched_ids:
            return "contradiction_not_named"
        result = memory.supersede_claim(
            named_claim_id,
            user_id,
            kind=candidate.kind,
            text=candidate.claim_text,
            quote=candidate.quote,
            speaker="user",
            evidence=evidence,
            explicitness=candidate.explicitness,
            confidence=confidence,
            run_id=run_id,
        )
        return "contradiction_target_missing" if "error" in result else "superseded"

    memory.record_claim(
        user_id,
        candidate.kind,
        candidate.claim_text,
        quote=candidate.quote,
        speaker="user",
        evidence=evidence,
        explicitness=candidate.explicitness,
        confidence=confidence,
        run_id=run_id,
        valid_until=candidate.valid_until,
    )
    return "inserted"


# ── 6. project (hook point for Phase 2's profile cache) ──────────────────

_profile_cache_invalidator: Any = None


def register_profile_cache_invalidator(fn: Any) -> None:
    """Join point for Phase 2's work-profile cache.

    Called once at server startup with a ``Callable[[str], None]`` that
    drops a user's cached profile projection (:meth:`MemoryService.profile`
    re-renders it on the next call). Until set, invalidation after a write
    is a no-op — correct (the cache just expires on its own trigger), only
    the ``apply`` step's immediate drop is skipped.
    """
    global _profile_cache_invalidator
    _profile_cache_invalidator = fn


def _invalidate_profile_cache(user_id: str) -> None:
    if _profile_cache_invalidator is not None:
        _profile_cache_invalidator(user_id)


# ── orchestration: window -> gate -> extract -> verify -> apply -> project
# -> record ────────────────────────────────────────────────────────────


def _new_counts() -> dict[str, Any]:
    return {
        "seen": 0,
        "candidates": 0,
        "inserted": 0,
        "reinforced": 0,
        "superseded": 0,
        "rejected": 0,
        "rejected_reasons": {},
    }


def _reject(counts: dict[str, Any], reason: str) -> None:
    counts["rejected"] += 1
    counts["rejected_reasons"][reason] = counts["rejected_reasons"].get(reason, 0) + 1


async def run_upkeep_once(
    *,
    user_id: str,
    conversation_store: ConversationStore,
    memory: UpkeepMemory,
    upkeep_store: MemoryUpkeepStore,
    llm_caller: UpkeepLLMCaller | None,
    now: int | None = None,
    min_user_messages: int = GATE_MIN_USER_MESSAGES,
) -> MemoryUpkeepRun | None:
    """Run the upkeep job once for *user_id*. ``None`` if a run is already
    in flight for this user (the lease — see
    :meth:`~omnigent.stores.memory_upkeep_store.MemoryUpkeepStore.start_run`)
    — a silent skip, never surfaced to the user, never recorded as a row.
    """
    now_ts = now if now is not None else now_epoch()
    watermark_run = upkeep_store.get_last_succeeded_run(user_id)
    since = watermark_run.window_until if watermark_run is not None else 0
    run_id = uuid.uuid4().hex
    started = upkeep_store.start_run(run_id, user_id, since, now_ts)
    if started is None:
        return None

    counts = _new_counts()
    try:
        window_items = bound_window(
            await asyncio.to_thread(gather_window, conversation_store, user_id, since)
        )
        counts["seen"] = sum(1 for i in window_items if i.role == "user")

        if not gate(window_items, min_user_messages=min_user_messages):
            return upkeep_store.finish_run(
                run_id, state="skipped", counts=counts, disposition="no_new_signal"
            )
        if llm_caller is None:
            return upkeep_store.finish_run(
                run_id, state="skipped", counts=counts, disposition="no_llm_configured"
            )

        raw_candidates = await extract_candidates(llm_caller, window_items)
        counts["candidates"] = len(raw_candidates)
        items_by_id = {i.item_id: i for i in window_items if i.role == "user"}

        changed = False
        for raw in raw_candidates:
            candidate, reason = verify_candidate(raw, items_by_id)
            if candidate is None:
                _reject(counts, reason or "malformed")
                continue
            outcome = await apply_candidate(memory, user_id, candidate, run_id, llm_caller)
            if outcome in ("inserted", "reinforced", "superseded"):
                counts[outcome] += 1
                changed = True
            else:
                _reject(counts, outcome)

        if changed:
            _invalidate_profile_cache(user_id)
        return upkeep_store.finish_run(run_id, state="succeeded", counts=counts, disposition="ok")
    except Exception:
        upkeep_store.finish_run(run_id, state="failed", counts=counts, disposition="error")
        raise


def build_upkeep_llm_caller() -> UpkeepLLMCaller | None:
    """Build the production :class:`UpkeepLLMCaller` from the server-level
    ``llm:`` config (``RuntimeCaps.llm``).

    Reuses the policy engine's server-LLM resolution
    (``omnigent.runtime.policies.builder``) — the same "how does the
    server itself call an LLM, with which credentials" answer function
    policies already use, so upkeep extraction needs no separate
    credential story. ``None`` when no server ``llm:`` config is present;
    callers then skip extraction (the run is recorded as ``skipped`` /
    ``no_llm_configured``).
    """
    from omnigent.runtime import get_caps
    from omnigent.runtime.policies.builder import (
        _build_policy_llm_client,
        _resolve_server_llm_connection,
    )

    server_llm = get_caps().llm
    if server_llm is None:
        return None
    connection = _resolve_server_llm_connection(server_llm)
    client = _build_policy_llm_client(server_llm, connection)
    if client is None:
        return None

    async def _call(*, instructions: str, input_text: str) -> str:
        from omnigent.llms.summarize import extract_summary_text

        response = await client.create(
            input=[{"role": "user", "content": input_text}],
            instructions=instructions,
        )
        return extract_summary_text(response)

    return _call
