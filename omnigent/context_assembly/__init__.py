"""Context-assembly contract (v0.2): what a blindfolded harness is told.

See ``blindfold-mission/context-assembly-contract.md`` for the full contract
this package implements, and ``omnigent/context_assembly/assembler.py`` for
the default policy.
"""

from __future__ import annotations

from omnigent.context_assembly.assembler import (
    assemble,
    assemble_or_fail_closed,
    observe,
    render_system_text,
    select_history_refs,
)
from omnigent.context_assembly.labels import (
    BLINDFOLD_LABEL,
    HISTORY_POLICY_LABEL,
    HISTORY_POLICY_NONE,
    HISTORY_POLICY_RECENT,
    MEMORY_FIXTURE_LABEL,
)
from omnigent.context_assembly.models import (
    AssembleAudit,
    AssembleRequest,
    AssembleResponse,
    Budget,
    HarnessCapabilities,
    HarnessInfo,
    HistoryBlock,
    HistoryItemRef,
    HistorySummary,
    MemoryBlock,
    MemoryItem,
    ObserveRequest,
    RecordInfo,
    SessionRef,
    SystemBlock,
    UsageInfo,
)

__all__ = [
    "BLINDFOLD_LABEL",
    "HISTORY_POLICY_LABEL",
    "HISTORY_POLICY_NONE",
    "HISTORY_POLICY_RECENT",
    "MEMORY_FIXTURE_LABEL",
    "AssembleAudit",
    "AssembleRequest",
    "AssembleResponse",
    "Budget",
    "HarnessCapabilities",
    "HarnessInfo",
    "HistoryBlock",
    "HistoryItemRef",
    "HistorySummary",
    "MemoryBlock",
    "MemoryItem",
    "ObserveRequest",
    "RecordInfo",
    "SessionRef",
    "SystemBlock",
    "UsageInfo",
    "assemble",
    "assemble_or_fail_closed",
    "observe",
    "render_system_text",
    "select_history_refs",
]
