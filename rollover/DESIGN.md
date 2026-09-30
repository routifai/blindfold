# Rollover: the super chat, Muse-style

Status: design, no code yet. Branch `rollover-muse`.

## Goal

One long-running **super chat** that feels like a normal session: streaming,
steering mid-turn, the prompt cache and tool calls all work. Omnigent, not the
CLI, decides what the model carries forward, and the agent can still reach
every message ever sent.

It has three parts, the same shape as Muse and Rakazo:

1. **Live session.** One normal, resident native CLI session (claude-native,
   codex-native, pi-native).
2. **Rollover.** When the context gets long, Omnigent writes a summary
   checkpoint and restarts the CLI session from *summary + recent messages +
   memory*. This happens between turns, never during one.
3. **Recall.** A read-only tool pages and searches the full session record.
   The rule: *the summary is a pointer, not the truth.*

Memory (curated facts that are injected and searchable) is phase 4.

## Why not blindfold every turn

Blindfold (see `blindfold/`) runs a new CLI session on every turn. That gives
strict control, but costs a lot of UX:

- no streaming;
- no steering;
- no warm prompt cache;
- a web-UI hold of about 60 s on turn 2 and later (open issue).

Rollover pays the cost of a fresh session once per rollover instead of on
every turn. Blindfold stays as an optional strict mode.

## What Omnigent already has (verified in code)

| Piece | Where | Use in rollover |
|---|---|---|
| `compaction` item type (`CompactionData`: summary, `last_item_id`, `compacted_messages`) | `omnigent/entities/conversation.py:491`; accepted by `POST /v1/sessions/{id}/events` (`routes_events.py:1465`) | Rollover **is** a compaction item that Omnigent writes |
| Native resume rebuilds the CLI history file from items and **restarts at the latest `compaction` item** | Claude: `claude_native/main.py:5580` (`records.clear()`, replays `compacted_messages`). Codex: `codex_native/main.py:2249` (`replacement_history`) | The rebuilt session is exactly the rollover selection. No new rebuilder is needed for Claude or Codex. |
| Kill the pane, then relaunch it with the rebuilt history file | `_reap_native_pane` (`runner/app.py:13852`) → `_ensure_native_terminal_for_turn` (`app.py:11461`) → `_auto_create_*_terminal` | Rollover recycles the pane on purpose, instead of only on idle timeout |
| LLM summarizer | `summarize_history` (`runtime/compaction.py:350`), can run on the runner's credentials via `POST /v1/summarize` | Writes the summary |
| SDK harnesses already start from the latest compaction item | `_load_history_as_input` / `_convert_raw_items_to_input` | Rollover works for SDK harnesses at no extra cost |
| Real token usage for native sessions | Claude and Codex forwarders post `external_session_usage` (`claude_native/forwarder.py:5260`, `codex_native/forwarder.py:1707`) | The trigger signal. Pi has none, so we estimate. |
| Full-text search, scoped to one session | `ConversationStore.search(query, conversation_id=…)` (`stores/conversation_store/__init__.py:852`) | The recall tool's search |
| History paging tool | `SysSessionGetHistoryTool` (`tools/builtins/spawn.py:1493`) | The recall tool's paging pattern |
| One MCP relay for native CLIs | `serve-mcp` (`claude_native/bridge.py:6993`), used by Claude and Codex. Pi goes through `POST /v1/sessions/{id}/mcp` (`pi_native/bridge.py:359`) | A new built-in tool shows up in native sessions without per-harness code |
| Compaction marker in the web UI | `web/src/components/blocks/StatusBlocks.tsx` | The user sees where a rollover happened. The full scrollback stays. |

## What is missing (verified in code)

- **The CLIs' own auto-compaction is untouched.** `DISABLE_AUTO_COMPACT`,
  `autoCompact` and `model_auto_compact_token_limit` appear nowhere in
  `omnigent/`. Muse runs with `auto_compact_limit_tokens: -1`, meaning the
  runtime owns compaction. We need the same, or each CLI compacts silently on
  its own schedule.
  - Claude Code's self-compaction is already persisted upstream as a real
    compaction item (`_persist_native_compaction_item`,
    `claude_native/forwarder.py`).
  - Codex's self-compaction is mirrored as a compaction item
    (`codex_native/forwarder.py:6994`).
- **Pi resume has no compaction handling** (0 hits in `pi_native/resume.py`),
  and it keeps an existing local session file untouched (`resume.py:553`).
- **Pi-native reports no token usage.**
- **No recall tool** scoped to the agent's own session: `search_conversations`
  searches every conversation.

## The design

### 1. Mode switch

A session label `omnigent.context.mode`:

| Value | Behaviour |
|---|---|
| unset | Upstream Omnigent: the CLI owns its context (side chats, default) |
| `rollover` | The super chat, as described here |
| `blindfold` | Strict mode: a new CLI session every turn (existing `omnigent.blindfold=true` keeps working) |

Side chats forked from the super chat are rollover sessions too, seeded Muse-style from the parent's latest summary plus memory (not the transcript). Memory: side chats read main memory and write their own (memory phase).

### 2. Trigger

After each turn, the server estimates the tokens in the model's current
context: every item since the latest compaction item, plus system text and
memory. It uses the forwarder's reported usage where there is one (Claude,
Codex) and `count_tokens` otherwise (Pi).

- **Threshold:** `omnigent.context.rollover_at_tokens`. The default is 45% of
  the model's context window, because Muse triggers at 150k of 350k. This
  leaves room for a heavy tool turn without the CLI hitting its own limit.
- **It runs between turns.** It never interrupts a turn in flight.

### 3. Building the rollover

This is the assembler's job, reusing contract v0.2:

- `summary`: `summarize_history` over everything since the previous
  compaction, merged with the previous summary. This is a *rolling* summary,
  the way Rakazo batches compaction.
- `recent`: the last `omnigent.context.rollover_keep_messages` messages
  (default 20). Tool calls ride along with their message, counted as
  `max_messages` is today.
- `memory`: rendered into the system text, as now (phase 4 fills it in).

It is written as **one `compaction` item** with `summary`, `last_item_id` and
`compacted_messages` set to the summary exchange followed by the recent items.
The full record is untouched, and the UI shows a compaction marker.

### 4. Restarting the CLI

- **Lazy restart.** Rollover recycles the resident pane: close it the way the
  reaper does. The next turn's existing ensure path relaunches it, the resume
  rebuilder starts from the new compaction item, and the CLI opens with
  exactly *summary + recent*.
- **Cost.** Paid once per rollover, on the next turn: about 0.5 s of CLI start
  plus the history-file rebuild. Every other turn is a normal resident turn.
- **Later optimisation.** Pre-compute the summary in the background at about
  80% of the threshold, so the rollover itself only writes the item.

### 5. Owning compaction

For `rollover` sessions only, each CLI's own auto-compaction is turned off at
launch. The exact flag for each CLI must be **verified against the real CLI**
before it is relied on: Claude Code (env or setting), Codex
(`model_auto_compact_token_limit`), Pi (setting). As a safety net, a
CLI-side compaction that happens anyway is detected, the same way the Codex
forwarder already does.

### 6. Recall tool (MCP, read-only)

A new built-in tool, registered in `omnigent/tools/builtins/__init__.py`. It
appears in native sessions through the existing relay.

```
session_history(action="read", before=<item id>|None, limit=5)   # page backward; default 5, max 20
session_history(action="search", query="...", limit=10)           # scoped FTS: conversation_id = this session
session_history(action="status")                                   # tokens used, window, tokens left before rollover
```

- It is scoped to the calling session and read-only.
- Reading other chats (for example the super chat from a side chat) is
  permission-gated, off by default, as Muse's `chat.read_messages` is.
- The tool description and the system text carry the rule: *"The summary may
  omit details. When earlier context matters, recover it with session_history
  before answering. Do not present an inference as what happened."*

### 7. Memory (phase 4)

What Muse and Rakazo do, in order:

- a small curated `MEMORY` block injected at every turn and every rollover;
- `memory_search` and `memory_write` tools, where the agent writes *before*
  replying when it learns something durable;
- a background writer in the `observe` hook;
- provenance kept on every item (`MemoryItem.source`), and top-5 recall per
  turn.

The time tag is added to each user message, as Muse does.

## Phases

| Phase | Scope | Proof |
|---|---|---|
| 1 | Label; trigger (token estimate); assembler builds the compaction item; pane recycle; CLI auto-compaction off. **Claude + Codex.** | E2E: send a codeword, fill past the threshold, and check that a rollover happened (a compaction item is written and the pane is relaunched). The codeword is still known if it's in the summary or recent messages. Streaming works on every turn. |
| 2 | `session_history` recall tool (read, search, status) | E2E: after a rollover, "what was my first message?" is answered **word for word** through the tool |
| 3 | Pi: compaction handling in `resume.py`, rebuild on rollover, token estimate | The phase 1 and 2 E2E tests on pi-native |
| 4 | Memory store, memory tools and injection; time tag | E2E: a fact from session A is known in a new session through memory |
| 5 | Background pre-summary; per-turn recall permission; the web UI switch | Rollover latency is about the same as a normal turn |

## Open questions

1. **Threshold default.** 45% of the window (Muse-like) or a fixed number of
   messages (Rakazo uses 50 recent, compacting 50 at a time)? Proposal: tokens
   first, with a message cap as a backstop.
2. **Summarizer model.** The session's own model, or a cheap fixed one?
   Proposal: a cheap one, run on the runner's credentials.
3. **Does blindfold stay in the tree?** Proposal: keep it as the strict mode,
   with no new work on it.
