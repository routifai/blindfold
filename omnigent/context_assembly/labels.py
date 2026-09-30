"""Session-label keys the context assembler reacts to.

``omnigent.blindfold`` selects the lifecycle (fresh CLI session every turn,
vendor memory off — see the per-harness wiring in
``omnigent/runner/native/orchestration.py``).

``omnigent.context.*`` configures the v0.2 default policy. It ships one knob,
``max_messages``, plus a memory-fixture test hook; production callers can
leave both unset and get the server default.
"""

from __future__ import annotations

# Set to "true" on a session to run it under the blindfold context-assembly
# contract: fresh CLI session every turn, vendor memory off, fail closed.
BLINDFOLD_LABEL = "omnigent.blindfold"

# History window size: the last N *messages* (user + assistant `message`
# items; a message's function_call/function_call_output/native_tool/reasoning
# items ride along attached to it, uncounted), the last one always being the
# new user message. Unset/invalid -> DEFAULT_MAX_MESSAGES. No summarization
# or compaction in v0.2 — older messages are simply dropped.
MAX_MESSAGES_LABEL = "omnigent.context.max_messages"
DEFAULT_MAX_MESSAGES = 20

# Test hook. When set, memory.items carries exactly one synthesized
# {"kind": "fact", "text": <value>} item instead of the real (currently
# always-empty) memory store. Lets a test prove the memory channel end to
# end without standing up a memory store.
MEMORY_FIXTURE_LABEL = "omnigent.context.memory_fixture"

# CLI process lifecycle for a blindfolded session's per-turn harness process.
# Unset (default) is "fresh": a new, disposable CLI process every turn, as in
# v0.2 of the contract. "warm_if_valid" opts a session into reusing a
# long-lived process across turns whenever the harness's own validity rule
# holds (same system+memory prompt, same model, and the exact same prior
# history the process has already seen, nothing dropped from the front by a
# sliding window) — see each native harness's own `blindfold_warm` module for
# the reuse/discard rule and the fail-closed default. Coordinated by name
# across harnesses; every harness reacts to the same label.
LIFECYCLE_LABEL = "omnigent.context.lifecycle"
LIFECYCLE_WARM_IF_VALID = "warm_if_valid"


# Every label that configures blindfold mode for a session; a side chat forked
# from a blindfolded session drops all of them.
BLINDFOLD_SESSION_LABELS: frozenset[str] = frozenset(
    {BLINDFOLD_LABEL, MAX_MESSAGES_LABEL, MEMORY_FIXTURE_LABEL, LIFECYCLE_LABEL}
)
