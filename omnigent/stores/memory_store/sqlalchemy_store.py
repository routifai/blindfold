"""SQLAlchemy-backed memory-claim store."""

from __future__ import annotations

import json

from sqlalchemy import asc, desc, select
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlMemoryClaim, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.entities import MemoryClaim, MemoryEvidenceLink
from omnigent.stores.memory_store import MemoryStore

_ACTIVE = "active"


def _encode_evidence(evidence: list[MemoryEvidenceLink] | None) -> str | None:
    if not evidence:
        return None
    return json.dumps([{"session_id": e.session_id, "item_id": e.item_id} for e in evidence])


def _decode_evidence(raw: str | None) -> list[MemoryEvidenceLink]:
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [
        MemoryEvidenceLink(session_id=e.get("session_id", ""), item_id=e.get("item_id", ""))
        for e in decoded
        if isinstance(e, dict)
    ]


def _to_entity(row: SqlMemoryClaim) -> MemoryClaim:
    """Convert a :class:`SqlMemoryClaim` ORM row to a :class:`MemoryClaim`."""
    return MemoryClaim(
        id=row.id,
        user_id=row.user_id,
        kind=row.kind,
        claim_text=row.claim_text,
        quote=row.quote,
        speaker=row.speaker,
        evidence=_decode_evidence(row.evidence),
        explicitness=row.explicitness,
        confidence=row.confidence,
        first_seen=row.first_seen,
        reinforced_at=row.reinforced_at,
        reinforcement_count=row.reinforcement_count,
        supersedes_claim_id=row.supersedes_claim_id,
        status=row.status,
        valid_until=row.valid_until,
        run_id=row.run_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlAlchemyMemoryStore(MemoryStore):
    """SQLAlchemy-backed implementation of :class:`MemoryStore`."""

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the SQLAlchemy memory store.

        :param storage_location: SQLAlchemy database URI, e.g.
            ``"sqlite:///chat.db"``.
        """
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.memory_store",
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.memory_store",
            immediate=True,
        )

    def create(
        self,
        claim_id: str,
        user_id: str,
        kind: str,
        claim_text: str,
        *,
        quote: str | None = None,
        speaker: str | None = None,
        evidence: list[MemoryEvidenceLink] | None = None,
        explicitness: str = "stated",
        confidence: float = 0.9,
        supersedes_claim_id: str | None = None,
        valid_until: int | None = None,
        run_id: str | None = None,
    ) -> MemoryClaim:
        """Insert a new active claim. See :meth:`MemoryStore.create`."""
        created_at = now_epoch()

        def write(session: Session) -> MemoryClaim:
            row = SqlMemoryClaim(
                id=claim_id,
                user_id=user_id,
                kind=kind,
                claim_text=claim_text,
                quote=quote,
                speaker=speaker,
                evidence=_encode_evidence(evidence),
                explicitness=explicitness,
                confidence=confidence,
                first_seen=created_at,
                reinforced_at=None,
                reinforcement_count=0,
                supersedes_claim_id=supersedes_claim_id,
                status=_ACTIVE,
                valid_until=valid_until,
                run_id=run_id,
                created_at=created_at,
                updated_at=None,
            )
            session.add(row)
            session.flush()
            return _to_entity(row)

        return run_write_transaction(self._session_immediate, "insert_memory_claim", write)

    def get(self, claim_id: str, user_id: str) -> MemoryClaim | None:
        """Return a claim if it belongs to *user_id*, else ``None``."""
        with self._session("select_memory_claim_by_id") as session:
            row = session.get(SqlMemoryClaim, (current_workspace_id(), claim_id))
            if row is None or row.user_id != user_id:
                return None
            return _to_entity(row)

    def list_active(
        self,
        user_id: str,
        *,
        kind: str | None = None,
        min_confidence: float | None = None,
        limit: int = 1000,
    ) -> list[MemoryClaim]:
        """List a user's active claims, newest first."""
        with self._session("list_active_memory_claims") as session:
            stmt = (
                select(SqlMemoryClaim)
                .where(SqlMemoryClaim.workspace_id == current_workspace_id())
                .where(SqlMemoryClaim.user_id == user_id)
                .where(SqlMemoryClaim.status == _ACTIVE)
            )
            if kind is not None:
                stmt = stmt.where(SqlMemoryClaim.kind == kind)
            if min_confidence is not None:
                stmt = stmt.where(SqlMemoryClaim.confidence >= min_confidence)
            stmt = stmt.order_by(desc(SqlMemoryClaim.created_at), desc(SqlMemoryClaim.id)).limit(
                limit
            )
            rows = session.execute(stmt).scalars().all()
            return [_to_entity(r) for r in rows]

    def list_all_active(self, limit: int = 100_000) -> list[MemoryClaim]:
        """List every active claim across every user, for index rebuilds."""
        with self._session("list_all_active_memory_claims") as session:
            stmt = (
                select(SqlMemoryClaim)
                .where(SqlMemoryClaim.workspace_id == current_workspace_id())
                .where(SqlMemoryClaim.status == _ACTIVE)
                .order_by(asc(SqlMemoryClaim.created_at), asc(SqlMemoryClaim.id))
                .limit(limit)
            )
            rows = session.execute(stmt).scalars().all()
            return [_to_entity(r) for r in rows]

    def reinforce(
        self, claim_id: str, user_id: str, *, confidence_increment: float
    ) -> MemoryClaim | None:
        """Bump reinforcement count/confidence, stamp ``reinforced_at``."""
        now = now_epoch()

        def write(session: Session) -> MemoryClaim | None:
            row = session.get(SqlMemoryClaim, (current_workspace_id(), claim_id))
            if row is None or row.user_id != user_id or row.status != _ACTIVE:
                return None
            row.reinforcement_count += 1
            row.reinforced_at = now
            row.confidence = min(1.0, row.confidence + confidence_increment)
            row.updated_at = now
            session.flush()
            return _to_entity(row)

        return run_write_transaction(self._session_immediate, "reinforce_memory_claim", write)

    def supersede(
        self,
        old_claim_id: str,
        user_id: str,
        *,
        new_claim_id: str,
        kind: str,
        claim_text: str,
        quote: str | None = None,
        speaker: str | None = None,
        evidence: list[MemoryEvidenceLink] | None = None,
        explicitness: str = "stated",
        confidence: float = 0.9,
        run_id: str | None = None,
    ) -> MemoryClaim:
        """Mark the old claim superseded and insert the replacement, atomically."""
        now = now_epoch()

        def write(session: Session) -> MemoryClaim:
            old_row = session.get(SqlMemoryClaim, (current_workspace_id(), old_claim_id))
            if old_row is not None and old_row.user_id == user_id:
                old_row.status = "superseded"
                old_row.updated_at = now
            new_row = SqlMemoryClaim(
                id=new_claim_id,
                user_id=user_id,
                kind=kind,
                claim_text=claim_text,
                quote=quote,
                speaker=speaker,
                evidence=_encode_evidence(evidence),
                explicitness=explicitness,
                confidence=confidence,
                first_seen=now,
                reinforced_at=None,
                reinforcement_count=0,
                supersedes_claim_id=old_claim_id,
                status=_ACTIVE,
                valid_until=None,
                run_id=run_id,
                created_at=now,
                updated_at=None,
            )
            session.add(new_row)
            session.flush()
            return _to_entity(new_row)

        return run_write_transaction(self._session_immediate, "supersede_memory_claim", write)

    def find_successor(self, claim_id: str, user_id: str) -> MemoryClaim | None:
        """Return the claim that supersedes *claim_id*, if any."""
        with self._session("select_memory_claim_successor") as session:
            stmt = (
                select(SqlMemoryClaim)
                .where(SqlMemoryClaim.workspace_id == current_workspace_id())
                .where(SqlMemoryClaim.user_id == user_id)
                .where(SqlMemoryClaim.supersedes_claim_id == claim_id)
            )
            row = session.execute(stmt).scalars().first()
            return _to_entity(row) if row is not None else None

    def set_status(self, claim_id: str, user_id: str, status: str) -> MemoryClaim | None:
        """Transition a claim's status."""
        now = now_epoch()

        def write(session: Session) -> MemoryClaim | None:
            row = session.get(SqlMemoryClaim, (current_workspace_id(), claim_id))
            if row is None or row.user_id != user_id:
                return None
            if row.status != status:
                row.status = status
                row.updated_at = now
            session.flush()
            return _to_entity(row)

        return run_write_transaction(self._session_immediate, "set_memory_claim_status", write)
