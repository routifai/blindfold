# Integration prompt: context management

Give this file to a coding agent that will build on, or integrate with,
Omnigent's context-management layer. The full reference is
[README.md](README.md) in this folder; this prompt is the briefing.

---

You are working in the Omnigent repository on a long-running "super chat": a
work assistant for bank employees. One conversation can run for weeks. A
context-management layer already exists; your job is to build on it without
breaking its guarantees. Read `rollover/README.md` before changing anything in
the areas below.

## What already exists

**Rollover.** A session created with the label
`omnigent.context.mode=rollover` keeps its model context bounded:

- The CLI behind the session (Claude Code, Codex or Pi) compacts its own
  context when it reaches Omnigent's threshold. Omnigent sets the threshold;
  the CLI is never restarted for it.
  - Claude Code: env `CLAUDE_CODE_AUTO_COMPACT_WINDOW` +
    `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` (`claude_auto_compact_env`,
    `omnigent/harnesses/claude_native/main.py`).
  - Codex: `model_auto_compact_token_limit`
    (`omnigent/harnesses/codex_native/launch_args.py`).
  - Pi: Omnigent's extension compacts after a settled turn
    (`omnigent/resources/pi_native/omnigent_pi_native_extension.js`).
- Threshold: 60% of the model's context window, clamped to 100k–200k tokens,
  looked up from the model at launch (`resolve_rollover_threshold`,
  `omnigent/context/rollover.py`). The label
  `omnigent.context.rollover_at_tokens` overrides it but never goes below
  100k: Claude Code alone starts at ~60k tokens, and lower thresholds made the
  CLIs compact in a loop.
- After a compaction, the next user message reaches the model with the most
  recent whole turns from Omnigent's record, verbatim, within
  `omnigent.context.rollover_keep_tokens` (16k default)
  (`build_post_compaction_tail`). Pi keeps its own recent turns.
- Every compaction is recorded in the session record as a `compaction` item
  with the real summary text.

**The record is the source of truth.** The visible thread (every message,
tool call and result) is never altered by context management. What the model
sees is a bounded view of it.

**Recall.** The built-in tool `session_history`
(`omnigent/tools/builtins/session_history.py`) is registered in every rollover
session. It is read-only and scoped to the calling session:

| Action | Use |
|---|---|
| `read` | Page full turns, newest first (`cursor`, `limit` ≤ 20, `chat_id?`) |
| `search` | Full-text search of this session's items (`query`, `limit` ≤ 20, `chat_id?`) |
| `status` | Threshold, current tokens, tokens left before the next compaction |
| `list_chats` | The side chats related to this session (its own side-chat forks, plus its parent when it is itself a side chat): id, title, timestamps, a short preview |

`chat_id` (on `read`/`search`) looks into one of `list_chats`' results instead
of this session — validated against that set (or the caller's own id) and the
same owner, never trusted from arguments alone. This is Muse's main chat
reading a side chat (`chat.list` + `chat.read_messages`), built at the
Omnigent level only: `omnigent/context/rollover.py` (`list_related_chats`,
`related_chat_ids`), the in-process tool, the runner's native-relay REST
dispatch (`omnigent/runner/tool_dispatch.py`), and
`GET /v1/sessions/{id}/related_chats`.

On Claude Code it is always loaded (not behind tool search) and pre-approved.
The rule given to every rollover session's model
(`ROLLOVER_CONTEXT_INSTRUCTION`, `omnigent/runtime/prompt.py`): *the summary
is a pointer, not the truth; recover exact details with recall before
answering.*

## Contracts you must keep

1. Never remove or rewrite items in the session record to save context.
2. Unset label means upstream behaviour, byte for byte. Gate new behaviour on
   `is_rollover(labels)` (`omnigent/context/labels.py`).
3. Framework instructions live in `omnigent/runtime/prompt.py`. Harness code
   only transports them; don't copy rules into per-harness code.
4. Tools are scoped from `ToolContext` (user, session), never from arguments.
5. Standing memory never goes into a compaction summary. It must reach the
   model live on every turn, or it gets frozen into summaries.
6. Keep tool results compact and capped. A large result can push the CLI over
   its threshold in the middle of an answer.

## If you are building the long-term memory tool

A reference implementation (Phase 1 of `rollover/MEMORY-PLAN.md`) already
exists — adopt it, extend it, or replace it behind the same tool contract:

1. Implemented as Omnigent built-in tools: `memory_remember`, `memory_search`,
   `memory_get`, `memory_explain`, `memory_forget`
   (`omnigent/tools/builtins/memory.py`), each a `Tool` subclass registered in
   `omnigent/tools/builtins/__init__.py` (framework-owned, rollover-session
   gated — see `ToolManager._register_memory_tools`). Native CLIs receive
   them through the MCP relay as `mcp__omnigent__memory_*`; SDK harnesses
   through `ToolManager`.
2. Scoped from the context, never from arguments: the user is the calling
   session's owner (`omnigent.tools.builtins.memory.resolve_memory_user`),
   resolved via `ConversationStore.get_session_owner`, with the reserved
   single-user identity as the fallback when no permission store is
   configured.
3. Read-only, compact JSON: each result with its text, a stable id, its source,
   and when it was learned or last confirmed.
4. On Claude Code, the tool names are in `_ALWAYS_LOADED_RELAY_TOOLS`
   (`omnigent/harnesses/claude_native/bridge.py`) and the pre-approved tools
   (`_ROLLOVER_ALLOWED_TOOLS`, `omnigent/runner/native/orchestration.py`).
   Without pre-approval, Claude Code's permission mode denies the call and
   the model appears to "refuse".
5. One short instruction for when to use it, `MEMORY_INSTRUCTION` in
   `omnigent/runtime/prompt.py`, next to `ROLLOVER_CONTEXT_INSTRUCTION`.
6. Split of duties: `session_history` is what was said in this session; the
   memory tool is what is known about the user across sessions.
7. Store: a `memory_claims` table (`omnigent/stores/memory_store/`) is the
   source of truth; a txtai hybrid search index (`omnigent/memory/index.py`)
   is rebuildable from it. Requires the optional `omnigent[memory]` extra
   (txtai + litellm); `omnigent.memory.build_memory_service()` returns `None`
   when it isn't installed, so the server mounts no memory routes and the
   tools report a clear "not configured" error rather than failing opaquely.
   Embeddings model: env `OMNIGENT_MEMORY_EMBEDDINGS_MODEL` (default
   `openai/text-embedding-3-small`; the server needs `OPENAI_API_KEY` for
   that default).

## If you are building the orchestrator, side chats or sub-agents

- Create the main chat with `omnigent.context.mode=rollover` in its labels at
  creation time.
- Side chats: `POST /v1/sessions/{id}/fork` with `side_chat: true`. The fork
  keeps the rollover labels and is seeded with one checkpoint (summary plus
  recent turns), not the full transcript. A fork has no host yet: bind it with
  `POST /v1/hosts/{host_id}/runners` (`session_id`, `workspace`), as the web
  UI's "Start session" does.
- Sub-agents don't inherit rollover. Add the label when creating a
  long-running sub-agent; short tasks can rely on the CLI's own compaction.
- A new `compaction` item in a session's events means a rollover happened.
  `session_history` `status` tells you how close the next one is.
- The super chat can read a side chat: `session_history`'s `list_chats` finds
  it, then `read`/`search` take its id as `chat_id`.
- A draft coordinator system prompt is in `rollover/SUPER-CHAT-PROMPT.md`.

## Pitfalls already hit (don't repeat them)

| Pitfall | What happened |
|---|---|
| Tool not pre-approved on Claude Code | Calls stalled on a permission prompt; it looked like the model refusing |
| Tool behind Claude Code's tool search | The model didn't know the tool existed |
| Threshold below what the CLI keeps | Compaction loop: compacting every turn, never answering |
| Relying on the startup snapshot for labels | A relaunched pane started without rollover setup; read labels live when there's no snapshot |
| Unescaped full-text queries | IDs with `-` or `:` (account and reference numbers) crashed search |
| Assuming a "done" message means a finished turn | Codex sends a preamble before tool calls; wait for the turn to end |
| Proving with unit tests only | Each of the bugs above passed unit tests and only showed up live |

## How to verify your change

- Unit tests: `tests/context`, `tests/tools/builtins/test_session_history.py`,
  `tests/tools/builtins/test_memory.py`, `tests/memory`,
  `tests/stores/test_memory_store.py`,
  `tests/server/routes/test_session_memory_routes.py`,
  `tests/test_claude_native*.py`, `tests/test_codex_native*.py`,
  `tests/test_pi_native*.py`, `tests/runner`. `list_chats`/`chat_id`:
  `tests/stores/test_conversation_store.py` and
  `tests/server/integration/test_sessions_items_search.py`
  (`GET .../related_chats`) in addition to the above.
- Live, on real CLIs: `dev/rollover/rollover_e2e.py` (see
  `dev/rollover/README.md`; ~100k tokens per CLI). It checks that each CLI
  compacts without looping, continues, sees and calls `session_history`,
  quotes the first message exactly after compaction, records the real
  summary, and does nothing when the label is unset.
