"""SQLAlchemy-backed memory-upkeep-run store."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlMemoryUpkeepRun, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.entities import MemoryUpkeepRun
from omnigent.stores.memory_upkeep_store import MemoryUpkeepStore

_RUNNING = "running"


def _decode_counts(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _to_entity(row: SqlMemoryUpkeepRun) -> MemoryUpkeepRun:
    """Convert a :class:`SqlMemoryUpkeepRun` ORM row to a :class:`MemoryUpkeepRun`."""
    return MemoryUpkeepRun(
        run_id=row.id,
        user_id=row.user_id,
        window_since=row.window_since,
        window_until=row.window_until,
        state=row.state,
        disposition=row.disposition,
        counts=_decode_counts(row.counts),
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


class SqlAlchemyMemoryUpkeepStore(MemoryUpkeepStore):
    """SQLAlchemy-backed implementation of :class:`MemoryUpkeepStore`."""

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the SQLAlchemy memory-upkeep-run store.

        :param storage_location: SQLAlchemy database URI, e.g.
            ``"sqlite:///chat.db"``.
        """
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.memory_upkeep_store",
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.memory_upkeep_store",
            immediate=True,
        )

    def start_run(
        self,
        run_id: str,
        user_id: str,
        window_since: int,
        window_until: int,
    ) -> MemoryUpkeepRun | None:
        """Atomically start a run. See :meth:`MemoryUpkeepStore.start_run`.

        The existing-running-row check and the insert happen in the same
        write transaction (an "immediate" SQLite transaction / a single
        round trip elsewhere), so the ``running`` row is itself *user_id*'s
        lease — two concurrent callers can never both get a non-``None``
        result for the same user.
        """
        started_at = now_epoch()

        def write(session: Session) -> MemoryUpkeepRun | None:
            existing = session.execute(
                select(SqlMemoryUpkeepRun)
                .where(SqlMemoryUpkeepRun.workspace_id == current_workspace_id())
                .where(SqlMemoryUpkeepRun.user_id == user_id)
                .where(SqlMemoryUpkeepRun.state == _RUNNING)
                .limit(1)
            ).scalar_one_or_none()
            if existing is not None:
                return None
            row = SqlMemoryUpkeepRun(
                id=run_id,
                user_id=user_id,
                window_since=window_since,
                window_until=window_until,
                state=_RUNNING,
                disposition=None,
                counts=None,
                started_at=started_at,
                finished_at=None,
            )
            session.add(row)
            session.flush()
            return _to_entity(row)

        return run_write_transaction(self._session_immediate, "start_memory_upkeep_run", write)

    def finish_run(
        self,
        run_id: str,
        *,
        state: str,
        counts: dict[str, Any],
        disposition: str | None = None,
    ) -> MemoryUpkeepRun | None:
        """Transition a run to a terminal state. See :meth:`MemoryUpkeepStore.finish_run`."""
        finished_at = now_epoch()

        def write(session: Session) -> MemoryUpkeepRun | None:
            row = session.get(SqlMemoryUpkeepRun, (current_workspace_id(), run_id))
            if row is None:
                return None
            row.state = state
            row.disposition = disposition
            row.counts = json.dumps(counts)
            row.finished_at = finished_at
            session.flush()
            return _to_entity(row)

        return run_write_transaction(self._session_immediate, "finish_memory_upkeep_run", write)

    def get_last_succeeded_run(self, user_id: str) -> MemoryUpkeepRun | None:
        """Return the watermark-setting run (see ``MemoryUpkeepStore.get_last_succeeded_run``)."""
        with self._session("select_last_succeeded_memory_upkeep_run") as session:
            stmt = (
                select(SqlMemoryUpkeepRun)
                .where(SqlMemoryUpkeepRun.workspace_id == current_workspace_id())
                .where(SqlMemoryUpkeepRun.user_id == user_id)
                .where(SqlMemoryUpkeepRun.state == "succeeded")
                .order_by(desc(SqlMemoryUpkeepRun.window_until), desc(SqlMemoryUpkeepRun.id))
                .limit(1)
            )
            row = session.execute(stmt).scalars().first()
            return _to_entity(row) if row is not None else None

    def has_running_run(self, user_id: str) -> bool:
        """Return whether a run is in flight. See :meth:`MemoryUpkeepStore.has_running_run`."""
        with self._session("select_running_memory_upkeep_run") as session:
            stmt = (
                select(SqlMemoryUpkeepRun.id)
                .where(SqlMemoryUpkeepRun.workspace_id == current_workspace_id())
                .where(SqlMemoryUpkeepRun.user_id == user_id)
                .where(SqlMemoryUpkeepRun.state == _RUNNING)
                .limit(1)
            )
            return session.execute(stmt).scalars().first() is not None

    def list_runs(self, user_id: str, *, limit: int = 20) -> list[MemoryUpkeepRun]:
        """List a user's runs, newest first. See :meth:`MemoryUpkeepStore.list_runs`."""
        with self._session("list_memory_upkeep_runs") as session:
            stmt = (
                select(SqlMemoryUpkeepRun)
                .where(SqlMemoryUpkeepRun.workspace_id == current_workspace_id())
                .where(SqlMemoryUpkeepRun.user_id == user_id)
                .order_by(desc(SqlMemoryUpkeepRun.started_at), desc(SqlMemoryUpkeepRun.id))
                .limit(limit)
            )
            rows = session.execute(stmt).scalars().all()
            return [_to_entity(r) for r in rows]
