# Rollover: implementation plan

Base: upstream Omnigent `main` (74f94c3d5). Integration branch: `rollover`.
Design: [DESIGN.md](DESIGN.md). Blindfold (on `rollover-muse`) is **not** carried
over. At most, small helpers are ported by hand where noted.

## Shared contract (fixed before any agent starts)

Every agent codes against these names. Nobody renames them.

| Name | Value |
|---|---|
| Mode label | `omnigent.context.mode` = `rollover` (unset = upstream behaviour) |
| Threshold label | `omnigent.context.rollover_at_tokens` (int). Default: 45% of the session's context window, or 90,000 when the window is unknown |
| Keep label | `omnigent.context.rollover_keep_messages` (int, default 20) |
| Module | `omnigent/context/` (new package) |
| Label helpers | `omnigent/context/labels.py`: `CONTEXT_MODE_LABEL`, `is_rollover(labels) -> bool`, `ROLLOVER_SESSION_LABELS` (kept by side-chat forks) |
| Rollover item | A normal `compaction` item (`CompactionData`): `summary`, `last_item_id`, `compacted_messages` = [summary exchange] + the last N messages with their tool items |
| Tool name | `session_history`, actions `read` / `search` / `status` |
| Prompt text | `ROLLOVER_CONTEXT_INSTRUCTION` in `omnigent/runtime/prompt.py`, added only for rollover sessions |

## Agents (Sonnet, one worktree each, run in parallel)

### A: Rollover core (server side)

Worktree `omnigent-ro-core`, branch `ro-core`.

- `omnigent/context/labels.py`, `omnigent/context/rollover.py`:
  - `estimate_context_tokens(items_since_last_compaction, ...)`: prefer the
    last `external_session_usage` when there is one, else `count_tokens`.
  - `select_recent(items, keep_messages)`: the last N messages, tool items
    riding along with their message. Port `select_history_refs` from
    `rollover-muse:omnigent/context_assembly/assembler.py`.
  - `build_rollover_item(...)`: calls `summarize_history` (a rolling summary
    merged with the previous compaction summary) and builds `CompactionData`.
- The trigger hook: after a turn completes in a rollover session, estimate;
  if over the threshold, write the compaction item, then recycle the native
  pane through the reaper's close path. The next turn's ensure path relaunches
  the CLI from the rebuilt history. Never trigger mid-turn.
- A side chat forked from a rollover session **stays in rollover mode** and is
  seeded Muse-style: its CLI opens from one compaction item holding the
  parent's latest summary plus the kept tail, never the full transcript.
  Non-rollover forks behave exactly as upstream.
- Tests: the selection and counting rules, the trigger threshold, "no
  rollover mid-turn", "OFF is unchanged", and side-chat seeding.

### A, revision 2: rules measured on Meta Muse (binding, supersede A above)

Evidence: five real Muse compactions read from its own database (see the Muse
evidence page). These replace the "last N messages" rule.

1. **Kept tail = whole turns.** A turn is a user message plus everything after
   it up to the next user message. Walk turns backward from the end and add
   whole turns while the total stays within `omnigent.context.rollover_keep_tokens`
   (new label, default 16_000, via `count_tokens`) **and** within
   `rollover_keep_messages` (user+assistant messages, default 20). The tail
   always starts at a user message. Never split a turn; never keep a
   `function_call` without its output.
2. **Empty tail is valid.** If the most recent turn alone exceeds the budget,
   keep no turns; the summary covers everything (Muse did this 2 of 5 times).
3. **Fixed header, added by code, not the LLM**, at the start of the summary text:
   "This conversation grew past its context limit and earlier turns were
   compacted into the summary below. The work in it is your own; build on it
   instead of redoing it. Files, processes and jobs your tools created still
   exist. Standing instructions and memory are live every turn and are not
   part of this summary. When facts conflict, the latest evidence and user
   corrections win. The summary may omit details: recover exact earlier
   messages with the session_history tool before relying on them."
4. **Summarizer instruction = a state file, not a narrative.** Title
   `## Context checkpoint — YYYY-MM-DD`; sections by topic, always starting
   with the user's identity, preferences and constraints (including corrections
   and "do not" rules) and always ending with "Current position / next step";
   each active task with exact identifiers (ids, paths, URLs, numbers),
   timestamps with time zone, status and next step; look-alike items in
   separate sections; negative facts ("no message was sent"); what the user was
   told about failures; absolute dates only. Rewrite the previous checkpoint
   into the new one (update in place, drop resolved noise) rather than
   appending. Pass this via a small optional prompt parameter on
   `summarize_history` / `build_summarization_prompt`; do not fork them.
5. **No stale-usage double rollover.** Ignore the reported
   `last_context_tokens` label when it predates the latest compaction item;
   fall back to `count_tokens` over the items since that compaction.
6. **Memory and standing instructions never go into `compacted_messages`**
   (they're injected live every turn). `token_count` on the item = the
   estimated tokens of summary + kept tail (Claude's `compact_boundary` needs it).

### B: Recall tool and system-prompt instructions

Worktree `omnigent-ro-recall`, branch `ro-recall`.

- `omnigent/tools/builtins/session_history.py`, a built-in `Tool`:
  - `read(before=<item id>|None, limit=5, max 20)`: pages backward through
    **the calling session** (the `SysSessionGetHistoryTool` pattern);
  - `search(query, limit=10)`: `ConversationStore.search(query,
    conversation_id=<self>)`;
  - `status()`: tokens used, the window, and tokens left before rollover (uses
    A's `estimate_context_tokens` through the agreed name; stub it until A
    merges).
  - Read-only. It cannot reach another session.
- Registered in `omnigent/tools/builtins/__init__.py`. It must be
  **advertised automatically for rollover sessions** (driven by the label, not
  per agent spec), and must reach claude-native and codex-native through the
  existing `serve-mcp` relay. **Verify this end to end**; don't assume it.
- `ROLLOVER_CONTEXT_INSTRUCTION` in `omnigent/runtime/prompt.py`, following
  the framework-instructions rule in `CLAUDE.md`. It carries Muse's rules:
  - "Compaction summarizes older messages; the summary may omit details."
  - "When earlier context matters, recover it with `session_history` before
    answering; page backward as needed."
  - "Do not present an inference as what happened."

  **Verify how framework instructions reach native CLIs** (`runner/app.py`
  calls `build_instructions`). If they don't, report that rather than hacking
  around it.
- Tests: scoping (no cross-session read), paging limits, search scoping, and
  the instruction present only for rollover sessions.

### B, revision 2: recall shaped like Muse's `chat.read_messages` (binding)

1. `session_history(action="read")` returns **full turns** (a user message plus
   everything that answered it), **newest first**, each with role, content,
   timestamps and item ids. Paging uses an opaque `cursor`; the response
   returns `next_cursor` for the next *older* page (no cursor = newest turns).
   `limit` counts turns: default 5, minimum 1, maximum 20. Reading never
   changes the session. Own session only for now.
2. The tool is **always available** in rollover sessions (never deferred),
   because recall is needed right after a compaction.
3. `status` returns what can be computed: `context_window_tokens`,
   `current_context_tokens` plus `source` (`"reported"` or `"estimate"`),
   `rollover_trigger_tokens`, `tokens_remaining_to_rollover`,
   `context_used_percent`. Omit what can't be computed.
4. `ROLLOVER_CONTEXT_INSTRUCTION` also says that standing instructions and
   memory are live every turn and are not part of the compacted summary.

### C: Omnigent owns compaction (harness side)

Worktree `omnigent-ro-harness`, branch `ro-harness`.

- For rollover sessions only, turn off each CLI's own auto-compaction at
  launch. **Find the real flag by testing the installed CLI**, not from
  memory:
  - Claude Code: an env var or setting;
  - Codex: `model_auto_compact_token_limit` or equivalent.
- Safety net: the Claude forwarder surfaces a CLI self-compaction
  (`isCompactSummary`, `claude_native/bridge.py:8324`) as a real `compaction`
  event, the way `codex_native/forwarder.py:6994` already does. Rollover
  sessions only.
- Prove the existing resume path does the rollover: write a synthetic
  compaction item into a Claude session and a Codex session, recycle the pane,
  and show that the relaunched CLI's history file holds only summary + kept
  messages.
- Tests for each flag, with OFF unchanged.

### D: End-to-end (after A, B and C merge)

- Reuse the Docker runner setup (`rollover-muse:dev/blindfold/`), with its own
  ports and container.
- The scenario, for claude-native and codex-native:
  1. Send a codeword.
  2. Chat past a **small** threshold (label `rollover_at_tokens` set low).
  3. Check that a compaction item was written and the pane relaunched.
  4. Check that streaming worked on every turn.
  5. Ask "what was my first message?": the answer must be exact, and the
     `session_history` tool call must be in the record.
- A baseline session (mode unset) behaves as upstream.

Later: Pi (compaction in `resume.py`, a token estimate); a permission-gated
`session_history` read of the same user's other chats (the main chat pulls from
side chats, nothing is pushed, as in Muse); memory with two paths, as in Muse: an immediate in-turn write to the injected
memory block (from any chat), plus an hourly consolidation job that runs only
when there are new turns and indexes memory for search; and the time tag.

## Rules for every agent

- Surgical: the label unset means byte-for-byte upstream behaviour.
- No per-harness copies of policy. Keep policy in `omnigent/context/` and
  `runtime/prompt.py`.
- Comments: two or three lines at most, describing the scenario, not the
  change history (repo `CLAUDE.md`).
- `ruff check` and `ruff format` on changed files, and the related tests
  green. Commit in small steps and never push.
- Never touch the shared test stack (:8780 / `omnigent-runner-test`). Use your
  own ports and containers.
- The lead (me) reviews every diff before merging into `rollover`.
