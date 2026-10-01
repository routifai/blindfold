# Context management for the super chat

One document for the teams building on this layer: what it does, how to plug
into it, the decisions behind it, and what went wrong along the way. Code
references are to this repository.

To brief a coding agent working on this layer, hand it
[INTEGRATION.md](INTEGRATION.md).
Side chats in detail (context, cross-chat reading, code map):
[SIDE-CHAT.md](SIDE-CHAT.md).
Agent memory, industry standard vs Muse (diagram, open in a browser):
[memory-comparison.html](memory-comparison.html).

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
| Recent turns kept verbatim after a compaction | ✅ All three CLIs, within `rollover_keep_tokens` |
| Automatic per-turn retrieval of older items, source links, inactivity refresh, pruning | ❌ See [Not provided yet](#not-provided-yet) |

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
| Claude Code | `CLAUDE_CODE_AUTO_COMPACT_WINDOW` + `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` (`claude_auto_compact_env`, `harnesses/claude_native/main.py`) | Claude Code's own summary, plus Omnigent's verbatim recent turns from before the compaction, delivered once on the next message (within `rollover_keep_tokens`) |
| Codex | `model_auto_compact_token_limit` (`codex_rollover_config_overrides`, `harnesses/codex_native/launch_args.py`) | Codex's own; it keeps the user's messages (up to ~20k tokens) plus its summary, plus Omnigent's verbatim recent turns from before the compaction, delivered once on the next message (within `rollover_keep_tokens`) |
| Pi | Omnigent's extension compacts after a settled turn at the threshold (`resources/pi_native/omnigent_pi_native_extension.js`) | Omnigent's state-file summary; recent turns within `rollover_keep_tokens` |

Each compaction is recorded as a `compaction` item by the harness forwarder
(Claude Code, Codex) or the Pi extension. The rollover instruction
(`ROLLOVER_CONTEXT_INSTRUCTION`, `runtime/prompt.py`) is added to the system
prompt of every rollover session.

Claude Code and Codex keep less than Pi on their own rollover: Pi always
carries a verbatim recent-turns tail forward (`select_recent` into
`compacted_messages`), but Claude Code's own compaction keeps only its
summary and Codex's keeps its summary plus user text only — no assistant
replies. The runner reproduces Pi's tail for them: once a rollover session's
`compaction` item is recorded, the NEXT message the web UI delivers to a
claude-native or codex-native CLI gets a verbatim block of the whole turns
Omnigent recorded immediately before that compaction (built by
`build_post_compaction_tail`, `omnigent/context/rollover.py`), prepended
only for that delivery — the persisted record and the user's own message are
unchanged. One block per compaction; a later compaction re-arms it (tracked
by `consume_post_compaction_tail`, `omnigent/context/rollover.py`). Two
delivery seams apply it: `omnigent/runner/app.py`'s
`_apply_post_compaction_tail`, called from the shared `proxy_stream`
turn-delivery path for both harnesses; and, for a codex-native session
resumed after a runner restart, `_apply_post_compaction_tail_to_resume_items`
in `omnigent/harnesses/codex_native/main.py` — Codex reads its own last
unanswered turn straight out of the rebuilt resume rollout there, never
through `proxy_stream`.

## Turning it on

Per session, off by default, set with labels **when the session is created**:

| Label | Value | Default |
|---|---|---|
| `omnigent.context.mode` | `rollover` enables it | Unset: upstream behaviour, unchanged |
| `omnigent.context.rollover_at_tokens` | Token threshold for a rollover | 60% of the model's context window, between 100,000 and 200,000; 100,000 when the window is unknown. A label value is never taken below 100,000 (or 80% of a smaller window). |
| `omnigent.context.rollover_keep_tokens` | Budget for the verbatim recent-turns tail, on all three CLIs (Pi's own, Claude Code's and Codex's post-compaction tail, side-chat seeds) | 16,000 |

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
- The super chat reading a side chat (Muse's `chat.list` +
  `chat.read_messages`) is built: `session_history`'s `list_chats` finds the
  related side chats, then `read`/`search` take that id as `chat_id`. See
  [Reading a side chat](#reading-a-side-chat-muse-parity) above.
- Coordinator system prompt: a draft for a bank-employee work assistant is in
  [SUPER-CHAT-PROMPT.md](SUPER-CHAT-PROMPT.md). Its context-management section
  matches `ROLLOVER_CONTEXT_INSTRUCTION`; don't repeat that rule in agent
  instructions.

## For the long-term memory team

Phase 1 of a reference implementation now lives in this repository —
[`MEMORY-PLAN.md`](MEMORY-PLAN.md) is the design; the memory team can adopt it
as-is, extend it (Phases 2-4: work profile every turn, the upkeep job,
colleagues/working-style synthesis), or replace it behind the same tool
contract. The memory lookup is exposed to agents as the `memory_*` tools, not
a single `recall_memory` tool:

| Tool | What |
|---|---|
| `memory_remember(text, kind?, quote?)` | Write a `stated` claim, indexed immediately; reinforces or supersedes a near-duplicate |
| `memory_search(query, kind?, limit?)` | Hybrid (BM25 + dense) search over the user's active claims, ranked by `score × confidence` with a recency boost |
| `memory_get(claim_id)` | Fetch one claim |
| `memory_explain(claim_id)` | Evidence quotes, source links, and the supersession chain |
| `memory_forget(claim_id?, query?, confirm)` | Two-step: a plan, then `confirm=true` to execute |

Reference implementation:

1. **Built-in tools**, Muse-style naming: `omnigent/tools/builtins/memory.py`
   (`MemoryRememberTool`, `MemorySearchTool`, `MemoryGetTool`,
   `MemoryExplainTool`, `MemoryForgetTool`), each a `Tool` subclass registered
   in `omnigent/tools/builtins/__init__.py`. Native CLIs reach them through
   the MCP relay (`mcp__omnigent__memory_*`); SDK harnesses through
   `ToolManager`.
2. **Scoped from the context, never from arguments.** The user is resolved
   from the calling session's owner
   (`omnigent.tools.builtins.memory.resolve_memory_user`, mirroring
   `session_history`'s session-from-context scoping) — never a value the
   model supplies.
3. **Read-only except `remember`/`forget`**, returning compact, capped JSON:
   claim id, text, kind, explicitness, last confirmed, and source links. A
   large tool result can push the CLI over its threshold mid-answer.
4. **Reachable on Claude Code:** `memory_*` is in `_ALWAYS_LOADED_RELAY_TOOLS`
   (`harnesses/claude_native/bridge.py`) so it isn't hidden behind tool
   search, and in the pre-approved tools (`_ROLLOVER_ALLOWED_TOOLS`,
   `runner/native/orchestration.py`). Without the pre-approval, Claude Code's
   permission mode denies the call.
5. **Told to the model** with one short framework instruction,
   `MEMORY_INSTRUCTION` in `omnigent/runtime/prompt.py`, next to
   `ROLLOVER_CONTEXT_INSTRUCTION` — appended only for a rollover session, same
   gate, same file (no per-harness copies).
6. **Never goes into a checkpoint.** A checkpoint holds conversation state
   only. Standing memory reaches the model live, every turn, via the
   instruction above plus on-demand `memory_search` calls (Phase 2's
   always-injected work profile is not built yet).
7. **Store and index**: a `memory_claims` table (source of truth,
   `omnigent/stores/memory_store/`) plus a txtai hybrid search index
   (`omnigent/memory/index.py`), rebuildable from the table via
   `MemoryService.rebuild_index()`. Requires the optional `omnigent[memory]`
   extra (txtai + litellm) — `omnigent/memory/build_memory_service()` returns
   `None` when it isn't installed, so a server without it mounts no memory
   routes and the tools return a clear "not configured" error instead of a
   half-working feature. Embeddings model:
   `OMNIGENT_MEMORY_EMBEDDINGS_MODEL` (default
   `openai/text-embedding-3-small`, via litellm — the server needs
   `OPENAI_API_KEY` for the default model).

Division of work: `session_history` is what was **said in this session**;
the memory tool is what is **known about the user across sessions**. See
`MEMORY-PLAN.md` for the reinforce/supersede rule, the ranking formula, and
what's deferred to later phases (contradiction detection beyond a
same-topic-different-text heuristic; the watermarked upkeep job that extracts
claims the user never explicitly asked to remember).

## Recall tool: `session_history`

Present only in rollover sessions. Read-only. The session comes from the
calling context, never from arguments, so it can't read another session on
its own — `read`/`search` can look into a *related* side chat, but only one
the context itself names (see below), never an arbitrary id.

| Action | Arguments | Returns |
|---|---|---|
| `read` | `cursor?`, `limit?` (turns; default 5, max 20), `chat_id?` | Full turns, newest first, with role, content, timestamps and item ids; `next_cursor` for older pages |
| `search` | `query`, `limit?` (default 10, max 20), `chat_id?` | Matching items, full-text |
| `status` | — | `rollover_trigger_tokens`, plus window, current tokens, tokens remaining and percent used when known |
| `list_chats` | — | The side chats related to this session (forked from it, or its parent if this one IS a side chat): id, title, created/updated timestamps, a short last-message preview |

It returns messages, tool calls and tool results only (each capped at 2,000
characters); reasoning and lifecycle items are never returned. Code:
`omnigent/tools/builtins/session_history.py`.

### Reading a side chat (Muse parity)

Muse's main chat can read a side chat (`chat.list` + `chat.read_messages`).
Omnigent's `session_history` mirrors that: call `list_chats` to find the
related side chats, then pass one of those ids as `chat_id` to `read` or
`search` to look into it — same output shape, limits and caps as reading
this session itself. `chat_id` is validated against the caller's own
`list_chats` set (or the caller's own id) and must be owned by the same
user; an unrelated or cross-owner id is a clear error, never a silent
redirect. This is read-only in both directions — a side chat is never
written to through this tool. Implemented at the Omnigent level only (no
per-CLI code): `omnigent/context/rollover.py` (`list_related_chats`,
`related_chat_ids`), `omnigent/tools/builtins/session_history.py` (the
in-process tool), `omnigent/runner/tool_dispatch.py` (the native-relay REST
dispatch), and `GET /v1/sessions/{id}/related_chats`
(`omnigent/server/routes/sessions/routes_items.py`).

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
| All | Recall looked unused | Mostly our wiring (permission prompt, missing record of Codex calls); once fixed, Claude Code and Codex called it on their own in every live run | Pre-approval, always loaded, rollover instruction; recent turns re-sent after a compaction |

## Not provided yet

| Item | Note |
|---|---|
| Automatic per-turn retrieval | Recall is on demand. Planned: after a compaction, Omnigent searches the record for the new message and attaches a small capped block (a few relevant earlier messages with item ids and open requests). |
| Source links inside summaries | A checkpoint records the last item it covers, not a link per fact |
| Refresh after inactivity | Only the token threshold triggers a rollover |
| Pruning verbose material between rollovers | Not built |
| Standing memory injected every turn | The extension point for long-term memory |
| SDK harnesses; Pi side chats | Not supported yet |

## Files changed

Every file this work changes against upstream, and why.

**Context-management core**

| File | Why |
|---|---|
| `omnigent/context/__init__.py` | New package |
| `omnigent/context/labels.py` | Label names (`omnigent.context.*`) and `is_rollover` |
| `omnigent/context/rollover.py` | Threshold (`resolve_rollover_threshold`), kept-tail budget, recent-turn selection, state-file summary for Pi and side-chat seeds, side-chat seed builder, recent-turns block after a compaction (`build_post_compaction_tail`, once per compaction), related-side-chat discovery (`list_related_chats`, `related_chat_ids`) |
| `omnigent/runtime/prompt.py` | `ROLLOVER_CONTEXT_INSTRUCTION`, added to every rollover session's system prompt; one sentence on using `list_chats`/`chat_id` for another chat |
| `omnigent/llms/summarize.py`, `omnigent/runtime/compaction.py` | `extra_instructions` so Omnigent's summarizer can produce the state-file shape |
| `omnigent/models/model_fallbacks.py` | Fallback model for side-chat seed summaries when the session names none |

**Recall**

| File | Why |
|---|---|
| `omnigent/tools/builtins/session_history.py` | The `session_history` tool (`read`, `search`, `status`, `list_chats`); `chat_id` on `read`/`search` to look into a related side chat, validated against the caller's own `list_chats`/id |
| `omnigent/tools/builtins/__init__.py`, `omnigent/tools/manager.py` | Register it, only for rollover sessions |
| `omnigent/runner/tool_dispatch.py` | Run it from native CLIs through the runner (over the server API) and include it in the relay tool list; `list_chats`/`chat_id` dispatch over the same REST endpoints |
| `omnigent/server/routes/sessions/routes_items.py` | `GET /v1/sessions/{id}/items/search`, scoped to one session; `GET /v1/sessions/{id}/related_chats` for `list_chats` |
| `omnigent/stores/conversation_store/__init__.py`, `sqlalchemy_store.py` | Quote full-text terms so IDs with `-` or `:` don't crash search; `list_conversations(fork_source_id=...)` filter powering related-chat discovery |

**Claude Code**

| File | Why |
|---|---|
| `omnigent/harnesses/claude_native/main.py` | Compaction threshold through `CLAUDE_CODE_AUTO_COMPACT_WINDOW` + `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` |
| `omnigent/harnesses/claude_native/bridge.py` | Keep `session_history` out of tool search (always loaded) |
| `omnigent/harnesses/claude_native/forwarder.py` | Save Claude Code's real summary in the compaction record (waits for the summary line instead of saving a placeholder) |

**Codex**

| File | Why |
|---|---|
| `omnigent/harnesses/codex_native/launch_args.py` | Compaction threshold through `model_auto_compact_token_limit` |
| `omnigent/harnesses/codex_native/forwarder.py` | Save Codex's real summary; record its MCP tool calls (e.g. `session_history`) |
| `omnigent/harnesses/codex_native/main.py` | Recent-turns block on a resumed session; side-chat seed marker sent as a developer message |

**Pi**

| File | Why |
|---|---|
| `omnigent/resources/pi_native/omnigent_pi_native_extension.js` (+ `.test.js`) | Compact at the threshold after a settled turn, with Omnigent's summary; hold incoming messages during a compaction; recall instruction |
| `omnigent/harnesses/pi_native/bridge.py` | Write the extension's rollover config; patch in the window-based threshold once the model is known |
| `omnigent/harnesses/pi_native/credentials.py` | Pi's own compaction settings (reserve and kept tokens) from the same threshold |

**Runner and server**

| File | Why |
|---|---|
| `omnigent/runner/native/orchestration.py` | Per launch: the threshold from the model's window, pre-approving `session_history` on Claude Code, the rollover instruction, live labels when there's no startup snapshot, Codex threshold, Pi threshold patch |
| `omnigent/runner/app.py` | Labels cache for rollover checks, relay tools by label, local provider credentials for side-chat summaries, recent-turns block on the next message after a compaction |
| `omnigent/server/routes/sessions/routes_core.py` | Seed a side chat forked from a rollover session with one checkpoint (summary plus recent turns) |

**Long-term memory (Phase 1 of `MEMORY-PLAN.md`)**

| File | Why |
|---|---|
| `omnigent/db/db_models.py`, `omnigent/db/migrations/versions/mm1a2b3c4d5e_add_memory_claims_table.py` | The `memory_claims` table (`SqlMemoryClaim`) |
| `omnigent/entities/memory_claim.py` | `MemoryClaim` / `MemoryEvidenceLink` dataclasses |
| `omnigent/stores/memory_store/__init__.py`, `.../sqlalchemy_store.py` | CRUD + per-user isolation + reinforce/supersede/forget |
| `omnigent/memory/__init__.py`, `config.py`, `index.py`, `service.py` | txtai hybrid search index (lazy-imported, pure-numpy ANN backend), the `MemoryService` read/write paths (remember/search/get/explain/forget), and `build_memory_service()` (returns `None` when the `memory` extra isn't installed) |
| `omnigent/tools/builtins/memory.py`, `omnigent/tools/builtins/__init__.py`, `omnigent/tools/manager.py` | The `memory_*` built-in tools; registered only for rollover sessions, same gate as `session_history` |
| `omnigent/runner/tool_dispatch.py` | Native-relay dispatch of `memory_*` over the server's REST API |
| `omnigent/server/routes/session_memory.py`, `omnigent/server/schemas.py`, `omnigent/server/app.py`, `omnigent/cli.py` | `/v1/sessions/{id}/memory/*` endpoints, mounted only when a `memory_service` is configured |
| `omnigent/runtime/_globals.py`, `omnigent/runtime/__init__.py` | `get_memory_service()` runtime getter |
| `omnigent/harnesses/claude_native/bridge.py` | `memory_*` in `_ALWAYS_LOADED_RELAY_TOOLS` |
| `omnigent/runner/native/orchestration.py` | `memory_*` in `_ROLLOVER_ALLOWED_TOOLS` |
| `omnigent/runtime/prompt.py` | `MEMORY_INSTRUCTION`, appended next to `ROLLOVER_CONTEXT_INSTRUCTION` |
| `pyproject.toml`, `uv.lock` | The optional `memory` extra (txtai + litellm) |

**Dev, docs and tests**

| File | Why |
|---|---|
| `dev/rollover/` | Live end-to-end test on real CLIs (script, runner image, example config, how-to) |
| `rollover/README.md`, `rollover/INTEGRATION.md`, `rollover/SUPER-CHAT-PROMPT.md` | This reference, the coding-agent briefing, the draft coordinator prompt |
| `tests/` (24 files) | Unit tests for all of the above; `tests/conftest.py` also keeps local provider keys out of the suite |

## Testing

- Unit tests: `tests/context`, `tests/tools/builtins/test_session_history.py`,
  `tests/test_claude_native.py`, `tests/test_codex_native*.py`,
  `tests/test_pi_native*.py`, `tests/runner/test_session_history_tool_dispatch.py`,
  `tests/runner/test_post_compaction_tail_delivery.py`. Related-chat
  discovery and `chat_id`: the same `test_session_history.py` and
  `test_session_history_tool_dispatch.py`, plus
  `tests/stores/test_conversation_store.py` (`fork_source_id` filter) and
  `tests/server/integration/test_sessions_items_search.py`
  (`GET .../related_chats`).
- Long-term memory (Phase 1): `tests/stores/test_memory_store.py`, `tests/memory`
  (service + index, with a deterministic offline embeddings backend — no
  OpenAI calls), `tests/tools/builtins/test_memory.py`,
  `tests/runner/test_memory_tool_dispatch.py`,
  `tests/server/routes/test_session_memory_routes.py`, plus the
  `memory_remember`/`memory_search` assertions folded into
  `tests/tools/test_manager.py`, `tests/runtime/test_prompt.py`, and
  `tests/runner/test_app_claude_native_launch_args.py`.
- Live end-to-end: [`dev/rollover/`](../dev/rollover/README.md) (real CLIs,
  costs ~100k tokens per harness).
