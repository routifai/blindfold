"""Session-label keys the context assembler reacts to.

``omnigent.blindfold`` selects the lifecycle (fresh CLI session every turn,
vendor memory off — see ``omnigent/context_assembly/README.md`` and the
per-harness wiring in ``omnigent/runner/native/orchestration.py``).

The ``omnigent.context.*`` keys are **test hooks only**: v0.2 ships one
default policy (system text from the agent's instructions, empty memory,
most-recent-items-that-fit history). These labels let a test pin a
deterministic policy instead of depending on real conversation length or a
real memory store, so the proving tests in the contract's §9 are
reproducible. Production callers should never need to set them.
"""

from __future__ import annotations

# Set to "true" on a session to run it under the blindfold context-assembly
# contract: fresh CLI session every turn, vendor memory off, fail closed.
BLINDFOLD_LABEL = "omnigent.blindfold"

# Test hook. "none" -> history carries only the new message. "recent"
# (default when absent) -> the most recent items that fit the budget.
HISTORY_POLICY_LABEL = "omnigent.context.history"
HISTORY_POLICY_NONE = "none"
HISTORY_POLICY_RECENT = "recent"

# Test hook. When set, memory.items carries exactly one synthesized
# {"kind": "fact", "text": <value>} item instead of the real (currently
# always-empty) memory store. Lets a test prove the memory channel end to
# end without standing up a memory store.
MEMORY_FIXTURE_LABEL = "omnigent.context.memory_fixture"
