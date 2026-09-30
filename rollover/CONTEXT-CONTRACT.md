# Context management contract

What the context layer guarantees, how other components plug into it, and what
it does not do yet. Code references are to this repository.

## Terms

| Term | Meaning |
|---|---|
| **Session** | One Omnigent conversation (main chat, side chat or sub-agent task). |
| **Record** | Every item of a session, stored by the server. It is the visible thread and is never altered by context management. |
| **Model context** | What the harness's model actually sees on a turn. Always a bounded view of the record. |
| **Rollover** | The harness compacting its own context once it passes Omnigent's token threshold. |
| **Checkpoint** | A `compaction` item in the record: the summary plus what the harness kept verbatim. |
| **Kept tail** | The recent whole turns kept verbatim in a checkpoint. |
| **Recall** | Reading exact earlier items from the record with the `session_history` tool. |

## Turning it on

Context management is per session and off by default. It is set with session
labels **when the session is created**:

| Label | Value | Default |
|---|---|---|
| `omnigent.context.mode` | `rollover` enables it | unset = upstream behaviour, unchanged |
| `omnigent.context.rollover_at_tokens` | Token threshold that triggers a rollover. Never below 100,000 (or 80% of a smaller window): Claude Code starts at ~60k and Codex keeps ~20k of user messages, so a lower value makes the CLI compact in a loop. | 60% of the model's context window (looked up from the model resolved at launch), capped at 200,000; 100,000 when the window is unknown |
| `omnigent.context.rollover_keep_tokens` | Budget for the kept tail where Omnigent chooses it (pi-native, side-chat seeds) | 16,000 |

Constants and helpers: `omnigent/context/labels.py`, `omnigent/context/rollover.py`.

## Guarantees

1. **The record is never altered.** Users always see every message. A checkpoint
   is added to the record, and nothing is removed.
2. **The model context is bounded.** Each native CLI compacts its own context
   when it reaches the session's threshold: Claude Code through
   `CLAUDE_CODE_AUTO_COMPACT_WINDOW` and `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`,
   Codex through `model_auto_compact_token_limit`, and Pi through its
   extension, which compacts at the threshold after a settled turn.
3. **Compaction happens inside the harness, with no restart.** The CLI process
   and its session continue. On Pi, a message sent while a compaction runs is
   held (up to 120 s) and delivered when it finishes.
4. **Every compaction is recorded** as a `compaction` item in the record, with
   the harness's real summary text.
5. **The rollover instruction is in the harness's system prompt**
   (`ROLLOVER_CONTEXT_INSTRUCTION` in `omnigent/runtime/prompt.py`). It says
   the summary is a pointer, not the truth, and that earlier details must be
   recovered with recall before answering.
6. **Exact earlier content stays reachable** through recall, whatever the
   summary dropped.

Summary format and kept tail per harness:

| Harness | Summary | Kept verbatim |
|---|---|---|
| claude-native | Claude Code's own compaction summary | Claude Code's own rule |
| codex-native | Codex's own summary | Codex keeps the user's messages plus its summary |
| pi-native | Omnigent's: the fixed header plus the state-file summary | Recent turns within `rollover_keep_tokens` |
| Side-chat seed (any harness) | Omnigent's: the fixed header plus the state-file summary | The parent's recent whole turns within `rollover_keep_tokens` |

Omnigent's state-file summary groups sections by topic: who the user is and
their constraints first, then active work with exact identifiers, open
requests and decisions, and "current position / next step" last. Its fixed
header (`CHECKPOINT_HEADER` in `omnigent/context/rollover.py`) states that the
checkpoint is not a message from the user.

## What the model sees after a rollover

The harness's own post-compaction context: its system prompt and tools
(including the rollover instruction), the compaction summary, what it kept
verbatim, then new turns.

## Checkpoint record

A normal `compaction` item (`CompactionData`), posted to
`POST /v1/sessions/{id}/events` by the harness forwarder (claude-native,
codex-native), the Pi extension, or the side-chat fork:

| Field | Content |
|---|---|
| `summary` | The compaction summary (see the table above) |
| `compacted_messages` | What the harness carries forward (Omnigent item dicts) |
| `last_item_id` | The last record item the checkpoint covers |
| `token_count` | Estimated tokens of `compacted_messages` |
| `model` | Model used for the summary |

The web UI shows it as a compaction marker.

## Recall tool: `session_history`

It is present only in rollover sessions and always loaded (never hidden
behind tool search). It is read-only. The session is taken from the calling
context and never from arguments, so it cannot read another session.

| Action | Arguments | Returns |
|---|---|---|
| `read` | `cursor?`, `limit?` (turns; default 5, max 20) | Full turns, newest first, each with role, content, timestamps and item ids; `next_cursor` for the next older page |
| `search` | `query`, `limit?` (default 10, max 20) | Matching items from this session, full-text |
| `status` | — | `rollover_trigger_tokens`; plus `context_window_tokens`, `current_context_tokens` and its `source`, `tokens_remaining_to_rollover`, `context_used_percent` when they are known |

It returns messages, tool calls and tool results only; reasoning and
lifecycle items are never returned. Code:
`omnigent/tools/builtins/session_history.py`.

## Session types

| Session | Context management | Starts with |
|---|---|---|
| **Main chat** (rollover label set) | Rollover plus recall, as above | Its own record |
| **Side chat** forked from a rollover session (`side_chat: true`) | Rollover too: the labels are kept | One checkpoint seeded from the parent: its latest summary plus kept tail, never the full transcript. Implemented for claude-native and codex-native; not for pi-native. |
| **Sub-agent / task session** | **Not managed by default.** Sub-agent sessions are created with their own labels (only a dispatch id), so the rollover label is not inherited. The harness manages their context with its own compaction. | The task message from the parent, plus whatever the parent passes |
| **Any session without the label** | Upstream behaviour, unchanged | — |

To manage a sub-agent's context too, create it with
`omnigent.context.mode=rollover` in its labels.

## Harness support

| Harness | Rollover mechanism | Recall |
|---|---|---|
| claude-native | Claude Code's own auto-compaction, set to the session's threshold | Tool always loaded; the model doesn't reliably call it on its own |
| codex-native | Codex's own auto-compaction, set to the session's threshold | Tool listed; the model doesn't reliably call it on its own |
| pi-native | Pi's own compaction, triggered at the threshold by the extension, with Omnigent's summary | Called on its own in live tests |
| SDK harnesses | Not supported yet | — |

## Integration: long-term memory

The long-term memory component exposes its lookup to agents as a tool, for
example `recall_memory`. To work with this layer:

1. **Implement it as an Omnigent built-in tool.** Subclass `Tool`
   (`omnigent/tools/base.py`) and register it in
   `omnigent/tools/builtins/__init__.py`. Native CLIs then receive it through
   the existing MCP relay with no per-harness code; SDK harnesses receive it
   through `ToolManager`.
2. **Scope from the context, never from arguments.** Read the user and session
   from `ToolContext`, as `session_history` does.
3. **Keep it read-only** and return compact JSON: each result with its text, a
   stable id, where it came from, and when it was learned or last confirmed.
4. **Ask for it to be always loaded** if agents must reach it without searching
   for tools: add its name to `_ALWAYS_LOADED_RELAY_TOOLS` in
   `omnigent/harnesses/claude_native/bridge.py`.
5. **Tell the model when to use it** with one short framework instruction in
   `omnigent/runtime/prompt.py`, next to `ROLLOVER_CONTEXT_INSTRUCTION`. Don't
   put per-harness copies of the rule in harness code.
6. **Memory never goes into a checkpoint.** A checkpoint holds conversation
   state only. Standing memory must be given to the model live on every turn
   (see the extension point below); otherwise it would be frozen into
   summaries.

Division of work: `session_history` returns what was **said in this session**,
and the memory tool returns what is **known about the user across sessions**.

## Integration: orchestration

- Create the main chat with `omnigent.context.mode=rollover` at creation time.
- Side chats forked with `side_chat: true` inherit rollover and are seeded
  automatically. No extra call is needed.
- For a sub-agent that should be context-managed, set the label when creating
  it. Otherwise it relies on its harness.
- A new `compaction` item in a session's event stream means a rollover
  happened. `session_history` with `status` reports how close the session is
  to the next one.

## Not provided yet

| Item | Note |
|---|---|
| Refresh after inactivity | Only the token threshold triggers a rollover |
| Source links inside summaries | A checkpoint records the last item it covers, not a link per fact |
| Pruning verbose material between rollovers | Only kept-tail tool outputs are capped |
| Automatic per-turn retrieval | Recall is on demand; nothing is injected automatically each turn. Needed, since models don't reliably call the recall tool by themselves |
| Standing memory injected every turn | The extension point for long-term memory; not built |
| A fixed "last N messages" per turn | Rollover bounds context by threshold instead |
| SDK harnesses; pi-native side chats | Not supported yet |
