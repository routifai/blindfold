"""Long-term memory: reference implementation of ``rollover/MEMORY-PLAN.md``.

Phase 1 (store and tools): a ``memory_claims`` table
(``omnigent.stores.memory_store``) is the source of truth; a txtai hybrid
search index (:mod:`omnigent.memory.index`) is built and rebuilt from it.
:class:`~omnigent.memory.service.MemoryService` implements the write/read
paths (``remember`` / ``search`` / ``get`` / ``explain`` / ``forget``) that
back the ``memory_*`` built-in tools
(``omnigent/tools/builtins/memory.py``).

Requires the optional ``omnigent[memory]`` extra (txtai + litellm). Every
module here imports those lazily so the rest of Omnigent is unaffected when
the extra isn't installed.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from omnigent.memory.config import MemoryConfig
from omnigent.memory.service import MemoryService

__all__ = ["MemoryConfig", "MemoryService", "build_memory_service", "memory_extra_available"]


def memory_extra_available() -> bool:
    """Return ``True`` if the optional ``omnigent[memory]`` extra (txtai) is installed.

    Probes via :func:`importlib.util.find_spec` (not ``import``) so the check
    never loads txtai or its transitive deps (torch, faiss) — they stay lazy
    until a search/write actually runs. Mirrors
    ``omnigent.tools.builtins._hindsight_available``.
    """
    return importlib.util.find_spec("txtai") is not None


def build_memory_service(db_uri: str, index_dir: Path) -> MemoryService | None:
    """Build the server's :class:`MemoryService`, or ``None`` if unavailable.

    :param db_uri: SQLAlchemy database URI backing the ``memory_claims`` table.
    :param index_dir: Directory the txtai search index is persisted under.
    :returns: A configured :class:`MemoryService`, or ``None`` when the
        ``memory`` extra isn't installed — callers then skip wiring the
        memory routes/runtime getter, and the ``memory_*`` tools surface a
        clear "not configured" error instead of a half-working feature.
    """
    if not memory_extra_available():
        return None
    from omnigent.memory.index import MemoryIndex
    from omnigent.stores.memory_store.sqlalchemy_store import SqlAlchemyMemoryStore

    store = SqlAlchemyMemoryStore(db_uri)
    index = MemoryIndex(index_dir)
    return MemoryService(store, index)
