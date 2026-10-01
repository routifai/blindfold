"""Memory-claim entity — persisted in the ``memory_claims`` table.

A :class:`MemoryClaim` is one durable fact, preference, instruction, decision,
person, project, or working-style note learned about a user. See
``rollover/MEMORY-PLAN.md`` section 3 for the design and
``omnigent/memory/`` for the service built on top of this store.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MemoryEvidenceLink:
    """One source pointer for a claim: a session item the claim was drawn from."""

    session_id: str
    item_id: str


@dataclass
class MemoryClaim:
    """
    A claim persisted in the ``memory_claims`` table.

    :param id: Unique claim identifier (bare 32-char hex string).
    :param user_id: The user this claim is about.
    :param kind: One of ``preference``, ``instruction``, ``fact``,
        ``decision``, ``person``, ``project``, ``working_style``.
    :param claim_text: One self-contained sentence describing the claim.
    :param quote: The exact words the evidence was drawn from, or ``None``.
    :param speaker: Who said it, or ``None``.
    :param evidence: Source links this claim was drawn from.
    :param explicitness: ``"stated"`` or ``"inferred"``.
    :param confidence: 0-1.
    :param first_seen: Unix epoch seconds the claim was first written.
    :param reinforced_at: Unix epoch seconds of the most recent
        reinforcement, or ``None``.
    :param reinforcement_count: Number of reinforcements folded into this
        claim.
    :param supersedes_claim_id: The claim this one replaced, or ``None``.
    :param status: ``active``, ``superseded``, ``expired``, or ``forgotten``.
    :param valid_until: Unix epoch seconds after which the claim no longer
        applies, or ``None``.
    :param run_id: The upkeep run that wrote this claim, or ``None`` for a
        claim written directly by ``memory_remember``.
    :param created_at: Unix epoch seconds at row creation.
    :param updated_at: Unix epoch seconds of the last write, or ``None``.
    """

    id: str
    user_id: str
    kind: str
    claim_text: str
    quote: str | None
    speaker: str | None
    evidence: list[MemoryEvidenceLink] = field(default_factory=list)
    explicitness: str = "stated"
    confidence: float = 0.9
    first_seen: int = 0
    reinforced_at: int | None = None
    reinforcement_count: int = 0
    supersedes_claim_id: str | None = None
    status: str = "active"
    valid_until: int | None = None
    run_id: str | None = None
    created_at: int = 0
    updated_at: int | None = None
