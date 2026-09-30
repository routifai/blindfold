# Context management for the super chat

One document for the teams building on this layer: what it does, how to plug
into it, the decisions behind it, and what went wrong along the way. Code
references are to this repository.

- **Super chat / orchestration team:** read [How it works](#how-it-works),
  [Turning it on](#turning-it-on) and [For the super chat and its sub-agents](#for-the-super-chat-and-its-sub-agents).
- **Long-term memory team:** read [How it works](#how-it-works) and
  [For the long-term memory team](#for-the-long-term-memory-team).
- **Everyone:** [Decisions](#decisions), [Problems we hit with the CLIs](#problems-we-hit-with-the-clis),
  [Not provided yet](#not-provided-yet).

## Status

| Requirement | Status |
|---|---|
| Visible thread survives agent-session restarts | ✅ The record keeps every message; context management never alters it |
| Visible history separate from model context | ✅ |
| Compaction with a tunable threshold and token budgets | ✅ Claude Code, Codex and Pi, at Omnigent's threshold |
| Recall of exact earlier content | ✅ `session_history` tool; verified live on all three CLIs after compaction |
| Contract for memory and orchestration | ✅ This document |
| Automatic per-turn retrieval, source links, inactivity refresh, pruning | ❌ See [Not provided yet](#not-provided-yet) |

Live proof (codeword first, ~100k tokens of documents, then "what was my first
message, word for word?"): Claude Code on Haiku 4.5 and Sonnet 5, and Codex on
gpt-5-mini, each compacted without looping, saw `session_history`, and quoted
the first message exactly.

## Terms

| Term | Meaning |
|---|---|
| **Session** | One Omnigent conversation: main chat, side chat or sub-agent task. |
| **Record** | Every item of a session, stored by the server. It is the visible thread and is never altered by context management. |
| **Model context** | What the model actually sees on a turn: a bounded view of the record. |
| **Rollover** | The CLI compacting its own context when it reaches Omnigent's threshold. |
| **Checkpoint** | The `compaction` item that records a rollover: the summary plus what the CLI kept verbatim. |
| **Recall** | Reading exact earlier items from the record with the `session_history` tool. |

## How it works

Three parts:

1. **Live session.** One normal, resident CLI session (Claude Code, Codex or
   Pi). Streaming, steering mid-turn, the prompt cache and tool calls all work
   as usual.
2. **Rollover.** When the context reaches the session's threshold, the CLI
   compacts itself in place. Omnigent sets the threshold and records the
   result. The CLI is never restarted for it.
3. **Recall.** A read-only tool pages and searches the full record. The rule
   given to the model: *the summary is a pointer, not the truth; recover
   exact details with recall before answering.*

| CLI | How Omnigent sets the threshold | Summary and kept content |
|---|---|---|
| Claude Code | `CLAUDE_CODE_AUTO_COMPACT_WINDOW` + `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` (`claude_auto_compact_env`, `harnesses/claude_native/main.py`) | Claude Code's own |
| Codex | `model_auto_compact_token_limit` (`codex_rollover_config_overrides`, `harnesses/codex_native/launch_args.py`) | Codex's own; it keeps the user's messages (up to ~20k tokens) plus its summary |
| Pi | Omnigent's extension compacts after a settled turn at the threshold (`resources/pi_native/omnigent_pi_native_extension.js`) | Omnigent's state-file summary; recent turns within `rollover_keep_tokens` |

Each compaction is recorded as a `compaction` item by the harness forwarder
(Claude Code, Codex) or the Pi extension. The rollover instruction
(`ROLLOVER_CONTEXT_INSTRUCTION`, `runtime/prompt.py`) is added to the system
prompt of every rollover session.

## Turning it on

Per session, off by default, set with labels **when the session is created**:

| Label | Value | Default |
|---|---|---|
| `omnigent.context.mode` | `rollover` enables it | Unset: upstream behaviour, unchanged |
| `omnigent.context.rollover_at_tokens` | Token threshold for a rollover | 60% of the model's context window, between 100,000 and 200,000; 100,000 when the window is unknown. A label value is never taken below 100,000 (or 80% of a smaller window). |
| `omnigent.context.rollover_keep_tokens` | Budget for the verbatim tail where Omnigent writes the summary (Pi, side-chat seeds) | 16,000 |

The window is looked up from the session's model at launch
(`find_model_context_window`: catalog, then litellm), so the rule is the same
for every model: Haiku 4.5 → 120k, gpt-5-mini → 163k, Sonnet 5 (1M) → 200k.
Code: `omnigent/context/labels.py`, `omnigent/context/rollover.py`.

## Guarantees

1. **The record is never altered.** Users always see every message. A
   rollover adds a `compaction` item and removes nothing.
2. **The model context is bounded** by the threshold on every CLI.
3. **No restart.** The CLI process and its session continue through a
   rollover. On Pi, a message sent during a compaction is held (up to 120 s)
   and delivered after it.
4. **Every rollover is recorded** as a `compaction` item.
5. **The rollover instruction is in the system prompt** of every rollover
   session.
6. **Recall is always reachable.** `session_history` is registered in every
   rollover session, kept out of Claude Code's tool search, and pre-approved
   so it never waits on a permission prompt.

## For the super chat and its sub-agents

| Session | Context management | Starts with |
|---|---|---|
| **Main chat** (label set) | Rollover plus recall | Its own record |
| **Side chat** forked with `side_chat: true` from a rollover session | Rollover too: labels are kept | One checkpoint seeded from the parent (latest summary plus recent turns), never the full transcript. Claude Code and Codex; not Pi yet. |
| **Sub-agent / task session** | **Not managed by default.** Sub-agents are created with their own labels (only a dispatch id), so they don't inherit rollover. Their CLI's own compaction applies. | The task message from the parent |
| **Any session without the label** | Upstream behaviour | — |

What to do:

- Create the main chat with `omnigent.context.mode=rollover`.
- Side chats: fork with `POST /v1/sessions/{id}/fork` and `side_chat: true`. The fork has no host yet; bind it with `POST /v1/hosts/{host_id}/runners` (`session_id`, `workspace`), which is what the web UI's "Start session" does. The rollover labels and the seeded checkpoint come with the fork.
- For a long-running sub-agent that should be managed too, add
  `omnigent.context.mode=rollover` to its labels when creating it.
- A new `compaction` item in a session's event stream means a rollover
  happened. `session_history` with `action: status` reports how close the
  session is to the next one.
- Coordinator system prompt: a draft for a bank-employee work assistant is in
  [SUPER-CHAT-PROMPT.md](SUPER-CHAT-PROMPT.md). Its context-management section
  matches `ROLLOVER_CONTEXT_INSTRUCTION`; don't repeat that rule in agent
  instructions.

## For the long-term memory team

The memory lookup is exposed to agents as a tool, for example `recall_memory`.

1. **Implement it as an Omnigent built-in tool.** Subclass `Tool`
   (`omnigent/tools/base.py`) and register it in
   `omnigent/tools/builtins/__init__.py`. Native CLIs then get it through the
   existing MCP relay (tool name `mcp__omnigent__recall_memory`); SDK
   harnesses through `ToolManager`. No per-harness code.
2. **Scope from the context, never from arguments.** Read the user and session
   from `ToolContext`, as `session_history` does.
3. **Keep it read-only** and return compact JSON: each result with its text, a
   stable id, where it came from, and when it was learned or last confirmed.
   Cap the response size: a large tool result can push the CLI over its
   threshold mid-answer.
4. **Make it reachable on Claude Code:** add its name to
   `_ALWAYS_LOADED_RELAY_TOOLS` (`harnesses/claude_native/bridge.py`) so it
   isn't hidden behind tool search, and to the pre-approved tools
   (`_ROLLOVER_ALLOWED_TOOLS`, `runner/native/orchestration.py`, or an
   equivalent list for memory sessions). Without the pre-approval, Claude
   Code's permission mode denies the call.
5. **Tell the model when to use it** with one short framework instruction in
   `omnigent/runtime/prompt.py`, next to `ROLLOVER_CONTEXT_INSTRUCTION`. Don't
   put per-harness copies of the rule in harness code.
6. **Memory never goes into a checkpoint.** A checkpoint holds conversation
   state only. Standing memory must reach the model live on every turn,
   otherwise it gets frozen into summaries.

Division of work: `session_history` is what was **said in this session**;
the memory tool is what is **known about the user across sessions**.

## Recall tool: `session_history`

Present only in rollover sessions. Read-only. The session comes from the
calling context, never from arguments, so it can't read another session.

| Action | Arguments | Returns |
|---|---|---|
| `read` | `cursor?`, `limit?` (turns; default 5, max 20) | Full turns, newest first, with role, content, timestamps and item ids; `next_cursor` for older pages |
| `search` | `query`, `limit?` (default 10, max 20) | Matching items from this session, full-text |
| `status` | — | `rollover_trigger_tokens`, plus window, current tokens, tokens remaining and percent used when known |

It returns messages, tool calls and tool results only (each capped at 2,000
characters); reasoning and lifecycle items are never returned. Code:
`omnigent/tools/builtins/session_history.py`.

## Checkpoint record

A normal `compaction` item (`CompactionData`), posted to
`POST /v1/sessions/{id}/events`:

| Field | Content |
|---|---|
| `summary` | The compaction summary |
| `compacted_messages` | What the CLI carries forward (Omnigent item dicts) |
| `last_item_id` | The last record item the checkpoint covers |
| `token_count` | Estimated tokens of `compacted_messages` |
| `model` | Model used for the summary |

If a Claude Code pane is restarted later, resume rebuilds the CLI session from
the latest `compaction` item's `compacted_messages`, which include Claude
Code's summary. The web UI shows each item as a "Conversation compacted"
marker.

## Decisions

| Decision | Alternatives considered | Why |
|---|---|---|
| **The CLI compacts itself at Omnigent's threshold** | (a) A fresh CLI session every turn with only our context. (b) Omnigent writes the checkpoint and restarts the CLI. | (a) loses streaming, steering and the warm prompt cache. (b) needs a restart per rollover, must never cut a turn in flight, and is more code to own. We built (b) first, then removed it (~930 lines): each CLI's compaction is already tested by its vendor, and we only need to control *when*. |
| **Threshold = 60% of the model's window, 100k–200k** | Fixed number; 45% of the window | One rule for every model. 100k floor: below it the CLI is still over the threshold right after compacting and loops. 200k cap: keeps per-turn cost and summary size sane on 1M-window models. |
| **Recall as a tool, not injected history** | Inject old messages every turn | Keeps context bounded; the model fetches exact text only when needed. Automatic per-turn retrieval is the planned complement (see below). |
| **Recall is label-gated, always loaded, pre-approved** | Per-agent opt-in | Every rollover session needs it, whichever agent spec is bound; a tool behind search or a permission prompt is effectively absent. |
| **Rollover rule in the framework prompt** | Per-harness or per-agent copies | One source of truth (`runtime/prompt.py`), transported by each harness. |
| **Sub-agents not managed by default** | Inherit the parent's labels | Most tasks are short; the orchestrator opts in per sub-agent when it's long-running. |
| **Side chats seeded from a summary** | Full parent transcript | Bounded from the first turn. |

## Problems we hit with the CLIs

| CLI | Problem | Effect | Fix |
|---|---|---|---|
| Claude Code | Its "don't ask" permission mode denies MCP tools that aren't pre-approved | Recall calls stalled ("pending user approval"); looked like the model refusing | `session_history` pre-approved in rollover sessions |
| Claude Code | Tool search hides MCP tools until searched | Model didn't know recall existed | `_meta anthropic/alwaysLoad` on the relay schema |
| Claude Code | Starts at ~60k tokens (system prompt + tools) | A 40k threshold compacted every turn; 100k left ~40k of room and a tool result compacted it mid-answer | Floor at 100k; threshold from the window (Haiku 120k) |
| Claude Code | Auto-compaction window only accepts 100k–1M | Can't express a small threshold directly | Window clamped to that range, percentage absorbs the rest |
| Claude Code | Its compaction hook and its summary line race | Many records got a placeholder summary instead of the real one | The forwarder waits for the summary line (up to 30 s) and saves one record with the real text; resume was never affected |
| Claude Code | Compaction in the middle of an answer | Haiku printed its compaction analysis instead of answering once | More room above the starting size (threshold from the window) |
| Codex | Keeps up to ~20k tokens of user messages after compacting | Low thresholds looped (12 compactions, never reached the tool) | Floor at 100k |
| Codex | Remote compaction with the built-in `openai` provider fails ("expected exactly one compaction output item, got 2") | No compaction | Omnigent launches Codex with its own provider, where it works |
| Codex | Our forwarder skipped MCP tool call items | Recall calls were invisible in the record | Forwarder mirrors `mcpToolCall` as `function_call` items |
| Codex | Compacted payload's summary wasn't stored | Placeholder in the record | Forwarder stores `payload.message` |
| Pi | No built-in trigger at a token count | — | Extension compacts at the threshold after a settled turn |
| Pi | Messages sent during a compaction were lost | Dropped user input | Inbox hold until `session_compact` (120 s cap) |
| Pi | Extension config written before the model's window is known | Extension compacted at 100k regardless of window | Threshold patched into the config once the window resolves |
| All | Models rarely call recall on their own when the summary looks sufficient | Answers from a lossy summary | Instruction in the system prompt; automatic per-turn retrieval planned |

## Not provided yet

| Item | Note |
|---|---|
| Automatic per-turn retrieval | Recall is on demand. Planned: after a compaction, Omnigent searches the record for the new message and attaches a small capped block (a few relevant earlier messages with item ids and open requests). |
| Source links inside summaries | A checkpoint records the last item it covers, not a link per fact |
| Refresh after inactivity | Only the token threshold triggers a rollover |
| Pruning verbose material between rollovers | Not built |
| Standing memory injected every turn | The extension point for long-term memory |
| SDK harnesses; Pi side chats | Not supported yet |

## Testing

- Unit tests: `tests/context`, `tests/tools/builtins/test_session_history.py`,
  `tests/test_claude_native.py`, `tests/test_codex_native*.py`,
  `tests/test_pi_native*.py`, `tests/runner/test_session_history_tool_dispatch.py`.
- Live end-to-end: [`dev/rollover/`](../dev/rollover/README.md) (real CLIs,
  costs ~100k tokens per harness).
