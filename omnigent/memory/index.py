"""txtai-backed search index over active memory claims.

The ``memory_claims`` table is the source of truth; this index is a
rebuildable projection of its active rows, kept in sync incrementally
(``upsert`` / ``delete`` on every write) and rebuildable in full from the
table via :meth:`MemoryIndex.rebuild`.

``txtai`` is imported lazily — constructing a :class:`MemoryIndex` (not
merely importing this module) is what requires the optional
``omnigent[memory]`` extra.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from omnigent.entities import MemoryClaim
from omnigent.memory.config import MemoryConfig

# Pure-numpy ANN backend: avoids pulling faiss into the query path (faiss's
# IVF/nprobe selection errors out on the small per-user corpora this index
# holds — see the design notes in rollover/MEMORY-PLAN.md) and keeps a
# per-user claim set (expected: low thousands at most) fast enough without
# an approximate index.
_ANN_BACKEND = "numpy"


def _missing_extra_error() -> ImportError:
    return ImportError(
        "Long-term memory requires the 'memory' extra: install with "
        "`uv sync --extra memory` (or `pip install omnigent[memory]`). "
        "It pulls in txtai + litellm."
    )


class MemoryIndex:
    """
    One txtai hybrid (BM25 + dense) index of active claims for the server.

    :param index_path: Directory the index is persisted under (txtai owns
        the files inside it). The directory need not exist yet.
    :param config: Embeddings model configuration. Defaults to
        :meth:`MemoryConfig.from_env`.
    :param vectors_override: Advanced/test-only override of the txtai
        vectors backend config (e.g. ``{"method": "external", "transform":
        "mypackage.fake_transform"}``) in place of the default
        ``litellm`` + :attr:`MemoryConfig.embeddings_model`. Lets unit
        tests exercise real indexing/search without any network calls or
        provider API keys.
    """

    def __init__(
        self,
        index_path: Path,
        config: MemoryConfig | None = None,
        *,
        vectors_override: dict[str, Any] | None = None,
    ) -> None:
        self._index_path = Path(index_path)
        self._config = config or MemoryConfig.from_env()
        self._vectors_override = vectors_override
        self._embeddings: Any | None = None
        # txtai's Embeddings is not documented as thread-safe for concurrent
        # writes; one process-wide lock serializes index mutations the same
        # way the SQL store serializes writes through a single engine.
        self._lock = threading.Lock()

    def _vectors_config(self) -> dict[str, Any]:
        if self._vectors_override is not None:
            return dict(self._vectors_override)
        return {"method": "litellm", "path": self._config.embeddings_model}

    def _embeddings_instance(self) -> Any:
        """Return the loaded or freshly constructed txtai ``Embeddings``."""
        if self._embeddings is not None:
            return self._embeddings
        try:
            from txtai.embeddings import Embeddings
        except ImportError as exc:
            raise _missing_extra_error() from exc

        if (self._index_path / "config").exists() or (self._index_path / "config.json").exists():
            embeddings = Embeddings()
            embeddings.load(str(self._index_path))
        else:
            embeddings = Embeddings(
                content=True,
                hybrid=True,
                backend=_ANN_BACKEND,
                **self._vectors_config(),
            )
        self._embeddings = embeddings
        return embeddings

    @staticmethod
    def _document(claim: MemoryClaim) -> tuple[str, dict[str, Any], None]:
        return (
            claim.id,
            {
                "text": claim.claim_text,
                "user_id": claim.user_id,
                "kind": claim.kind,
                "status": claim.status,
                "confidence": claim.confidence,
                "reinforced_at": claim.reinforced_at or 0,
            },
            None,
        )

    def upsert(self, claim: MemoryClaim) -> None:
        """Index (or re-index) one claim. Call for every create/reinforce/supersede."""
        with self._lock:
            embeddings = self._embeddings_instance()
            embeddings.upsert([self._document(claim)])
            embeddings.save(str(self._index_path))

    def delete(self, claim_id: str) -> None:
        """Remove one claim from the index (forget, expire, supersede)."""
        with self._lock:
            embeddings = self._embeddings_instance()
            embeddings.delete([claim_id])
            embeddings.save(str(self._index_path))

    def search(
        self,
        user_id: str,
        query: str,
        *,
        kind: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Hybrid search over *user_id*'s active claims only.

        :returns: Rows with ``id``, ``text``, ``score``, ``kind``,
            ``confidence``, ``reinforced_at`` — ranking is txtai's raw hybrid
            score; callers apply the confidence/recency re-rank (see
            :mod:`omnigent.memory.service`).
        """
        with self._lock:
            embeddings = self._embeddings_instance()
            sql = (
                "select id, text, score, kind, confidence, reinforced_at from txtai "
                "where similar(:q) and user_id = :uid and status = 'active'"
            )
            params: dict[str, Any] = {"q": query, "uid": user_id}
            if kind is not None:
                sql += " and kind = :kind"
                params["kind"] = kind
            return list(embeddings.search(sql, limit, parameters=params))

    def rebuild(self, claims: Iterable[MemoryClaim]) -> int:
        """Rebuild the index from scratch from the given (active) claims.

        The table is the source of truth; call this after a table-only
        migration, or to recover from a lost/corrupted index directory.

        :returns: Number of claims indexed.
        """
        try:
            from txtai.embeddings import Embeddings
        except ImportError as exc:
            raise _missing_extra_error() from exc

        with self._lock:
            documents = [self._document(c) for c in claims]
            if not documents:
                # txtai can't save() an index that never received a non-empty
                # index() call — nothing to persist, so just drop any stale
                # in-memory instance; the next upsert() builds fresh.
                self._embeddings = None
                return 0
            embeddings = Embeddings(
                content=True,
                hybrid=True,
                backend=_ANN_BACKEND,
                **self._vectors_config(),
            )
            embeddings.index(documents)
            self._index_path.mkdir(parents=True, exist_ok=True)
            embeddings.save(str(self._index_path))
            self._embeddings = embeddings
            return len(documents)
