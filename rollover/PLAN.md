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
| Label helpers | `omnigent/context/labels.py`: `CONTEXT_MODE_LABEL`, `is_rollover(labels) -> bool`, `ROLLOVER_SESSION_LABELS` (dropped by side-chat forks) |
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
- A side-chat fork drops `ROLLOVER_SESSION_LABELS` (in `routes_core.py`).
- Tests: the selection and counting rules, the trigger threshold, "no
  rollover mid-turn", "OFF is unchanged", and the side-chat drop.

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

Later: Pi (compaction in `resume.py`, a token estimate), memory, and the time tag.

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
