"""Memory service: the read/write paths behind the ``memory_*`` tools.

Combines the :class:`~omnigent.stores.memory_store.MemoryStore` (source of
truth) with the :class:`~omnigent.memory.index.MemoryIndex` (search). See
``rollover/MEMORY-PLAN.md`` section 4-5 for the design this implements.
"""

from __future__ import annotations

import math
import time
import uuid
from typing import Any

from omnigent.entities import MemoryClaim, MemoryEvidenceLink
from omnigent.memory.index import MemoryIndex
from omnigent.stores.memory_store import MemoryStore

VALID_KINDS = frozenset(
    {
        "preference",
        "instruction",
        "fact",
        "decision",
        "person",
        "project",
        "working_style",
    }
)
DEFAULT_KIND = "fact"

# Starting confidence by explicitness, per MEMORY-PLAN.md section 3. Public
# (not just the private aliases below) because Phase 3's upkeep job
# (``omnigent/memory/upkeep.py``) picks a starting confidence for a verified
# candidate the same way ``remember()`` does for a direct call.
STATED_CONFIDENCE = 0.9
INFERRED_CONFIDENCE = 0.4
_STATED_CONFIDENCE = STATED_CONFIDENCE
_INFERRED_CONFIDENCE = INFERRED_CONFIDENCE

# --- remember()'s near-duplicate dedup rule (Phase 1; Phase 3's upkeep job
# may refine this with a model call per the plan) ---
#
# A near-duplicate (hybrid score and word overlap both high) is reinforced;
# anything else is added. A claim is only superseded when the caller names it
# (replaces_claim_id): the model reconciles conflicts explicitly, never by guess.
_DEDUP_SCORE_THRESHOLD = 0.5
_SAME_TEXT_JACCARD_THRESHOLD = 0.8
_REINFORCE_CONFIDENCE_INCREMENT = 0.05

# search()'s ranking: hybrid_score * confidence, plus a small recency boost
# that decays over ~90 days since the claim was last reinforced.
_RECENCY_BOOST_WEIGHT = 0.1
_RECENCY_BOOST_HALFLIFE_SECONDS = 90 * 24 * 3600

# profile()'s projection (MEMORY-PLAN.md Phase 2, Muse's USER.md): active
# claims of these kinds, grouped in this order. Phase 1 has no separate
# "identity" kind, so a role/identity claim is stored as kind="fact" and
# included here as "About the user".
PROFILE_KINDS: tuple[str, ...] = ("preference", "instruction", "working_style", "fact")
_PROFILE_GROUP_LABELS = {
    "preference": "Preferences",
    "instruction": "Standing instructions",
    "working_style": "Working style",
    "fact": "About the user",
}
_PROFILE_MIN_CONFIDENCE = 0.75
# ~1.5k tokens, estimated chars/4 (no tokenizer call for a per-turn fetch).
_PROFILE_MAX_CHARS = 1_500 * 4

_PROFILE_BLOCK_HEADER = (
    "[Standing memory about the user — provided by the system, not a message from the user]"
)
_PROFILE_BLOCK_FOOTER = "[End of standing memory]"


def render_profile_block(profile: str) -> str:
    """Wrap a rendered profile in the delimiters prepended to every turn.

    Shared by every per-turn delivery seam (``rollover/MEMORY-PLAN.md``
    Phase 2) so the wording lives in one place, next to the projection it
    wraps.
    """
    return f"{_PROFILE_BLOCK_HEADER}\n\n{profile}\n\n{_PROFILE_BLOCK_FOOTER}"


def _tokenize(text: str) -> set[str]:
    return {tok for tok in text.lower().split() if tok}


def _jaccard(a: str, b: str) -> float:
    ta, tb = _tokenize(a), _tokenize(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _claim_to_dict(claim: MemoryClaim, *, score: float | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "claim_id": claim.id,
        "text": claim.claim_text,
        "kind": claim.kind,
        "explicitness": claim.explicitness,
        "confidence": round(claim.confidence, 3),
        "status": claim.status,
        "last_confirmed": claim.reinforced_at or claim.first_seen,
        "source": [{"session_id": e.session_id, "item_id": e.item_id} for e in claim.evidence],
    }
    if score is not None:
        result["score"] = round(score, 4)
    return result


class MemoryService:
    """Ties the claim store and the search index together for the memory tools."""

    def __init__(self, store: MemoryStore, index: MemoryIndex) -> None:
        self._store = store
        self._index = index
        # profile()'s render cache, per user; invalidated on any claim write
        # for that user (see _invalidate_profile). Unbounded by design: one
        # entry per user who has ever been profiled, a short string each.
        self._profile_cache: dict[str, str | None] = {}

    def _invalidate_profile(self, user_id: str) -> None:
        """Drop a cached profile so the next call re-renders it from the store."""
        self._profile_cache.pop(user_id, None)

    # ── Write path ──────────────────────────────────────────────────

    def remember(
        self,
        user_id: str,
        text: str,
        *,
        kind: str | None = None,
        quote: str | None = None,
        speaker: str | None = None,
        evidence: list[MemoryEvidenceLink] | None = None,
        replaces_claim_id: str | None = None,
    ) -> dict[str, Any]:
        """Write a ``stated`` claim, immediately indexed; reinforce a near-duplicate.

        Supersedes only the active claim named by ``replaces_claim_id``.

        :returns: ``{"action": "added"|"reinforced"|"superseded", "claim": {...}}``.
        """
        resolved_kind: str = (
            kind if isinstance(kind, str) and kind in VALID_KINDS else DEFAULT_KIND
        )
        if replaces_claim_id:
            old = self._store.get(replaces_claim_id, user_id)
            if old is None or old.status != "active":
                return {"error": f"no active claim {replaces_claim_id} to replace"}
            existing_id = old.id
            new_claim = self._store.supersede(
                existing_id,
                user_id,
                new_claim_id=uuid.uuid4().hex,
                kind=resolved_kind,
                claim_text=text,
                quote=quote,
                speaker=speaker,
                evidence=evidence,
                explicitness="stated",
                confidence=_STATED_CONFIDENCE,
            )
            self._index.delete(existing_id)
            self._index.upsert(new_claim)
            self._invalidate_profile(user_id)
            return {"action": "superseded", "claim": _claim_to_dict(new_claim)}

        best = self._best_match(user_id, text)
        if (
            best is not None
            and best[1] >= _DEDUP_SCORE_THRESHOLD
            and _jaccard(text, best[2]) >= _SAME_TEXT_JACCARD_THRESHOLD
        ):
            reinforced = self._store.reinforce(
                best[0], user_id, confidence_increment=_REINFORCE_CONFIDENCE_INCREMENT
            )
            if reinforced is not None:
                self._index.upsert(reinforced)
                self._invalidate_profile(user_id)
                return {"action": "reinforced", "claim": _claim_to_dict(reinforced)}

        new_claim = self._store.create(
            uuid.uuid4().hex,
            user_id,
            resolved_kind,
            text,
            quote=quote,
            speaker=speaker,
            evidence=evidence,
            explicitness="stated",
            confidence=_STATED_CONFIDENCE,
        )
        self._index.upsert(new_claim)
        self._invalidate_profile(user_id)
        return {"action": "added", "claim": _claim_to_dict(new_claim)}

    def _best_match(self, user_id: str, text: str) -> tuple[str, float, str] | None:
        """Return ``(claim_id, score, claim_text)`` of the single best hit, or ``None``."""
        hits = self._index.search(user_id, text, limit=1)
        if not hits:
            return None
        top = hits[0]
        return top["id"], float(top["score"]), str(top["text"])

    # ── Write path (Phase 3's upkeep job) ────────────────────────────
    #
    # Thin wrappers around the store/index, mirroring remember()'s shape,
    # for a caller (``omnigent/memory/upkeep.py``) that has already decided
    # kind/confidence/explicitness/run_id itself (via its own classify step)
    # rather than remember()'s text-similarity dedup.

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
    ) -> dict[str, Any]:
        """Insert a new active claim written by an upkeep run (or any caller
        that already knows it is not a duplicate of an active claim)."""
        resolved_kind = kind if kind in VALID_KINDS else DEFAULT_KIND
        new_claim = self._store.create(
            uuid.uuid4().hex,
            user_id,
            resolved_kind,
            text,
            quote=quote,
            speaker=speaker,
            evidence=evidence,
            explicitness=explicitness,
            confidence=confidence,
            valid_until=valid_until,
            run_id=run_id,
        )
        self._index.upsert(new_claim)
        self._invalidate_profile(user_id)
        return {"action": "added", "claim": _claim_to_dict(new_claim)}

    def reinforce_claim(
        self, claim_id: str, user_id: str, *, confidence_increment: float
    ) -> dict[str, Any] | None:
        """Bump an active claim's confidence/reinforcement count, or ``None`` if
        *claim_id* is not an active claim belonging to *user_id*."""
        reinforced = self._store.reinforce(
            claim_id, user_id, confidence_increment=confidence_increment
        )
        if reinforced is None:
            return None
        self._index.upsert(reinforced)
        self._invalidate_profile(user_id)
        return _claim_to_dict(reinforced)

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
    ) -> dict[str, Any]:
        """Supersede *old_claim_id* with a new active claim written by an
        upkeep run. ``{"error": ...}`` if *old_claim_id* is not an active
        claim belonging to *user_id*."""
        old = self._store.get(old_claim_id, user_id)
        if old is None or old.status != "active":
            return {"error": f"no active claim {old_claim_id} to replace"}
        resolved_kind = kind if kind in VALID_KINDS else DEFAULT_KIND
        new_claim = self._store.supersede(
            old_claim_id,
            user_id,
            new_claim_id=uuid.uuid4().hex,
            kind=resolved_kind,
            claim_text=text,
            quote=quote,
            speaker=speaker,
            evidence=evidence,
            explicitness=explicitness,
            confidence=confidence,
            run_id=run_id,
        )
        self._index.delete(old_claim_id)
        self._index.upsert(new_claim)
        self._invalidate_profile(user_id)
        return {"action": "superseded", "claim": _claim_to_dict(new_claim)}

    # ── Read path ───────────────────────────────────────────────────

    def search(
        self,
        user_id: str,
        query: str,
        *,
        kind: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Hybrid search ranked by ``score * confidence`` with a recency boost."""
        hits = self._index.search(user_id, query, kind=kind, limit=max(limit * 2, limit))
        now = time.time()
        ranked: list[tuple[float, dict[str, Any]]] = []
        for hit in hits:
            confidence = float(hit.get("confidence") or 0.0)
            reinforced_at = hit.get("reinforced_at") or 0
            recency_boost = 0.0
            if reinforced_at:
                age = max(now - float(reinforced_at), 0.0)
                recency_boost = _RECENCY_BOOST_WEIGHT * math.exp(
                    -age / _RECENCY_BOOST_HALFLIFE_SECONDS
                )
            rank = float(hit["score"]) * confidence + recency_boost
            ranked.append((rank, hit))
        ranked.sort(key=lambda pair: pair[0], reverse=True)

        results: list[dict[str, Any]] = []
        for rank, hit in ranked[:limit]:
            claim = self._store.get(hit["id"], user_id)
            if claim is None or claim.status != "active":
                continue
            results.append(_claim_to_dict(claim, score=rank))
        return results

    def get(self, user_id: str, claim_id: str) -> dict[str, Any] | None:
        """Return one claim by id, scoped to *user_id*."""
        claim = self._store.get(claim_id, user_id)
        return _claim_to_dict(claim) if claim is not None else None

    def explain(self, user_id: str, claim_id: str) -> dict[str, Any] | None:
        """Evidence quotes, source links, and the supersession chain for a claim."""
        claim = self._store.get(claim_id, user_id)
        if claim is None:
            return None

        supersedes: list[dict[str, Any]] = []
        cursor = claim.supersedes_claim_id
        seen: set[str] = {claim_id}
        while cursor and cursor not in seen:
            prior = self._store.get(cursor, user_id)
            if prior is None:
                break
            supersedes.append(_claim_to_dict(prior))
            seen.add(cursor)
            cursor = prior.supersedes_claim_id

        superseded_by: dict[str, Any] | None = None
        successor = self._store.find_successor(claim_id, user_id)
        if successor is not None:
            superseded_by = _claim_to_dict(successor)

        return {
            "claim": _claim_to_dict(claim),
            "quote": claim.quote,
            "speaker": claim.speaker,
            "evidence": [
                {"session_id": e.session_id, "item_id": e.item_id} for e in claim.evidence
            ],
            "supersedes": supersedes,
            "superseded_by": superseded_by,
        }

    # ── Forget (two-step) ───────────────────────────────────────────

    def forget(
        self,
        user_id: str,
        *,
        claim_id: str | None = None,
        query: str | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Plan (``confirm=False``) or execute (``confirm=True``) forgetting one claim.

        :returns: ``{"status": "plan"|"forgotten"|"not_found", "claim": {...}?}``.
        """
        target_id = claim_id
        if target_id is None:
            if not query:
                return {"status": "error", "error": "forget requires claim_id or query"}
            best = self._best_match(user_id, query)
            if best is None:
                return {"status": "not_found"}
            target_id = best[0]

        claim = self._store.get(target_id, user_id)
        if claim is None or claim.status == "forgotten":
            return {"status": "not_found"}

        if not confirm:
            return {"status": "plan", "claim": _claim_to_dict(claim)}

        updated = self._store.set_status(target_id, user_id, "forgotten")
        if updated is None:
            return {"status": "not_found"}
        self._index.delete(target_id)
        self._invalidate_profile(user_id)
        return {"status": "forgotten", "claim": _claim_to_dict(updated)}

    # ── Work profile (Phase 2) ──────────────────────────────────────

    def profile(self, user_id: str) -> str | None:
        """Render the standing work profile injected every turn.

        A projection of *user_id*'s active claims — Muse's ``USER.md``
        (``rollover/MEMORY-PLAN.md`` Phase 2): claims of :data:`PROFILE_KINDS`
        with confidence at least :data:`_PROFILE_MIN_CONFIDENCE`, grouped by
        kind, newest-reinforced first within a group, capped at roughly
        :data:`_PROFILE_MAX_CHARS` characters. Cached per user until the next
        claim write for that user (:meth:`_invalidate_profile`).

        :returns: The rendered profile text (without the delimiter block —
            see :func:`render_profile_block`), or ``None`` when there is
            nothing to show.
        """
        if user_id in self._profile_cache:
            return self._profile_cache[user_id]
        rendered = self._render_profile(user_id)
        self._profile_cache[user_id] = rendered
        return rendered

    def _render_profile(self, user_id: str) -> str | None:
        lines: list[str] = []
        char_count = 0
        for kind in PROFILE_KINDS:
            claims = self._store.list_active(
                user_id, kind=kind, min_confidence=_PROFILE_MIN_CONFIDENCE
            )
            if not claims:
                continue
            claims.sort(key=lambda c: c.reinforced_at or c.first_seen, reverse=True)
            header = f"{_PROFILE_GROUP_LABELS[kind]}:"
            if char_count + len(header) + 1 > _PROFILE_MAX_CHARS:
                break
            section = [header]
            section_chars = len(header) + 1
            for claim in claims:
                line = f"- {claim.claim_text}"
                if char_count + section_chars + len(line) + 1 > _PROFILE_MAX_CHARS:
                    break
                section.append(line)
                section_chars += len(line) + 1
            if len(section) > 1:
                lines.extend(section)
                char_count += section_chars
        return "\n".join(lines) if lines else None

    # ── Maintenance ─────────────────────────────────────────────────

    def rebuild_index(self) -> int:
        """Rebuild the search index from every active claim in the table."""
        return self._index.rebuild(self._store.list_all_active())
