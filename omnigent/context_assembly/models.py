"""Wire shapes for the context-assembly contract (v0.2).

Mirrors ``blindfold-mission/context-assembly-contract.md`` field-for-field so
the JSON a runner sends/receives over ``POST /v1/sessions/{id}/context`` is
exactly what :func:`omnigent.context_assembly.assembler.assemble` accepts and
returns. Kept as a standalone module (no server/runner imports) so the
contract's "HTTP later with the same shapes" promise holds without pulling in
the whole server.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

CONTRACT_VERSION = "0.2"


class SessionRef(BaseModel):
    """The session an ``assemble``/``observe`` call is about."""

    id: str
    owner: str
    labels: dict[str, str] = Field(default_factory=dict)


class HarnessCapabilities(BaseModel):
    """What the target CLI can render, so the assembler renders within it."""

    history_format: str
    images: bool = True


class HarnessInfo(BaseModel):
    """The harness/model this turn will run under."""

    name: str
    model: str
    context_window_tokens: int
    capabilities: HarnessCapabilities


class RecordInfo(BaseModel):
    """A pointer at the session's record; the assembler reads the record
    itself (``GET /v1/sessions/{id}/items``) — this only locates it."""

    item_count: int
    last_item_id: str


class Budget(BaseModel):
    """Token ceiling the response's rendered content must fit inside."""

    max_input_tokens: int = 24_000


class AssembleRequest(BaseModel):
    """``POST /v1/sessions/{id}/context`` request body (contract §2)."""

    contract_version: str = CONTRACT_VERSION
    turn_id: str
    session: SessionRef
    harness: HarnessInfo
    new_item_id: str
    record: RecordInfo
    budget: Budget = Field(default_factory=Budget)


class SystemBlock(BaseModel):
    """Contract §3 ``system``. ``mode="append"`` keeps the CLI's own base
    prompt; ``digest`` lets a harness skip re-rendering an unchanged prompt."""

    mode: str = "append"
    text: str
    digest: str


class MemoryItem(BaseModel):
    """One long-term-memory fact/preference/instruction (contract §3)."""

    id: str
    kind: str
    text: str
    source: dict[str, str] | None = None
    updated_at: int = 0


class MemoryBlock(BaseModel):
    items: list[MemoryItem] = Field(default_factory=list)
    digest: str = ""


class HistoryItemRef(BaseModel):
    """A record item included by reference, copied faithfully by the
    per-harness rebuilder that resolves it (contract §3 ``history.items``)."""

    ref: str


class HistorySummary(BaseModel):
    """A synthetic summary of older items — never written to the record."""

    text: str
    covers_up_to: str


class HistoryBlock(BaseModel):
    summary: HistorySummary | None = None
    items: list[HistoryItemRef] = Field(default_factory=list)
    digest: str = ""


class AssembleAudit(BaseModel):
    """What was included and its size (contract §3 ``audit``)."""

    memory_items: int
    history_items: int
    summary: bool
    estimated_tokens: int
    # Not in the wire contract's example, but explicitly allowed by "stored
    # with the turn for debugging and evals" — set when assemble() itself
    # failed and Omnigent fell back to the fail-closed shape (contract §7).
    fallback: bool = False


class AssembleResponse(BaseModel):
    """``POST /v1/sessions/{id}/context`` response body (contract §3)."""

    contract_version: str = CONTRACT_VERSION
    turn_id: str
    system: SystemBlock
    memory: MemoryBlock
    history: HistoryBlock
    audit: AssembleAudit


class UsageInfo(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


class ObserveRequest(BaseModel):
    """``observe`` request (contract §4); the response is bare ``204``."""

    contract_version: str = CONTRACT_VERSION
    turn_id: str
    session_id: str
    outcome: str
    new_item_ids: list[str] = Field(default_factory=list)
    usage: UsageInfo = Field(default_factory=UsageInfo)
