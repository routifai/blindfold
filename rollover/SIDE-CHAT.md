# Side chats

How a side chat gets its context in a rollover session, how the main chat
and a side chat read each other, and where it lives in the code. Modeled on
Meta's Muse. The general contract is in [README.md](README.md).

## What a side chat is

A side chat is a separate conversation forked from a chat for a focused
topic. It has its own record and its own CLI session. It starts from a
**summary of the parent**, not the parent's full transcript, so its context
is bounded from the first turn.

| | Muse | Omnigent |
|---|---|---|
| Starts with | "born with the main agent's system prompt, memory, and context summarized" | System prompt with the rollover and memory instructions, plus the parent's summary and recent turns |
| Full parent transcript in context | No | No |
| Long-term memory | Shared (`~/MEMORY.md`) | Shared, per user (`memory_*` tools) |
| Reports back to the main chat | No | No |
| Main chat reads a side chat | `chat.list` + `chat.read_messages` | `session_history` `list_chats` + `read` / `search` with `chat_id` |
| Side chat reads its parent | Same tools | Same, and the parent's items are also copied into the fork |

## Lifecycle

```
main chat (rollover)
   │  POST /v1/sessions/{id}/fork  {side_chat: true}
   ▼
1. Fork copies the visible record and keeps the rollover labels
2. Fork does NOT clone the CLI's own session file (that would be the full transcript)
3. Server appends one seed checkpoint: parent summary + recent whole turns
   │  POST /v1/hosts/{host_id}/runners  {session_id, workspace}   ← bind to a host
   ▼
4. CLI starts from the seed (latest compaction item)
5. Side chat runs as a normal rollover session: compaction, recall, memory
```

| Step | Code |
|---|---|
| Fork, keep labels | `omnigent/server/routes/sessions/routes_core.py`: fork handler, `body.side_chat` branch |
| Don't clone the CLI session | `routes_core.py`: `resume_source_native_session` is false for a rollover side chat |
| Seed the fork | `routes_core.py`: `_seed_rollover_side_chat` |
| Build the seed | `omnigent/context/rollover.py`: `build_side_chat_seed` (summary), `select_recent` (whole turns within `rollover_keep_tokens`), `_cap_tool_outputs`, `state_file_summarizer_instruction`, `CHECKPOINT_HEADER` |
| CLI starts from the seed | Claude Code: resume rebuild from the latest compaction item (`omnigent/harnesses/claude_native/main.py`). Codex: same, with `_checkpoint_marker_as_developer` (`omnigent/harnesses/codex_native/main.py`) so the seed isn't read as the user's first message |

The seed is the parent's latest compaction summary; if the parent never
compacted, a new summary is written over its whole record at fork time.

## Reading across chats

`session_history` (the recall tool, present in every rollover session):

| Action | Arguments | Returns |
|---|---|---|
| `list_chats` | — | The calling chat's side chats (forked from it, newest first) and, if the calling chat is a side chat, its parent. Each with id, title, dates, last message preview. Max 20. |
| `read` | `chat_id?`, `cursor?`, `limit?` | Full turns of that chat, newest first |
| `search` | `query`, `chat_id?`, `limit?` | Matching items in that chat |

Without `chat_id`, `read` and `search` act on the calling chat, as before.

**Access rules** (`list_related_chats` in `omnigent/context/rollover.py`,
`_resolve_chat_target` in `omnigent/tools/builtins/session_history.py`):

1. The owner is resolved from the store for the calling chat, never taken
   from arguments.
2. A chat is related only if it is a side chat forked from the caller
   (label `omnigent.fork.source_id` = caller, plus the side-chat label) or the
   caller's own parent, **and** has the same owner.
3. `chat_id` must be the caller itself or in the caller's `list_chats`,
   recomputed on every call. Anything else returns an error.
4. Read-only: nothing writes into another chat.

**Instruction to the model** (`ROLLOVER_CONTEXT_INSTRUCTION`,
`omnigent/runtime/prompt.py`): when the user refers to another chat, call
`list_chats`, then `read` or `search` with `chat_id`; name the source chat in
the answer; never write into another chat.

**Two execution paths, same rules:**

| Path | Code |
|---|---|
| In-process (SDK harnesses) | `omnigent/tools/builtins/session_history.py`: `_list_chats`, `_resolve_chat_target` |
| Native CLIs (through the runner) | `omnigent/runner/tool_dispatch.py`: `_session_history_list_chats_via_rest`, `_resolve_chat_target_via_rest`; server route `GET /v1/sessions/{id}/related_chats` (`omnigent/server/routes/sessions/routes_items.py`) |
| Discovery query | `ConversationStore.list_conversations(fork_source_id=…)` (`omnigent/stores/conversation_store/`) |

## For the orchestrator

1. Fork: `POST /v1/sessions/{id}/fork` with `side_chat: true`. Rollover labels
   and the seed come with it; nothing else to pass.
2. Bind it to a host: `POST /v1/hosts/{host_id}/runners` with `session_id` and
   `workspace`. The fork has no host until then (the web UI's "Start session"
   button does this).
3. Send messages to the side chat as to any session. Results don't flow back
   to the main chat automatically; the main chat reads them on demand with
   `list_chats` / `read`.

## Verified

| Check | How |
|---|---|
| Fork keeps labels, seeds from the parent's checkpoint, or summarizes when there is none; non-rollover forks unchanged | `tests/server/routes/test_sessions_fork.py` (side-chat tests) |
| Side chat recalls the parent's exact words | Live, Claude Code: a fork answered the parent's codeword through `session_history` |
| Related-chat discovery: children, parent, plain fork excluded, unrelated chat excluded, other user excluded | `tests/tools/builtins/test_session_history.py` |
| `chat_id` allowed and denied, runner path, route | `tests/runner/test_session_history_tool_dispatch.py`, `tests/server/integration/test_sessions_items_search.py` |
| Store filter by fork source | `tests/stores/test_conversation_store.py` |

## Limits

| Limit | Note |
|---|---|
| Discovery depends on the fork label | The server stamps `omnigent.fork.source_id` when the parent has a workspace or a native runner, which is the case for the super chat; a chat-only parent's side chats aren't found |
| Pi side chats | Not supported: Pi's resume keeps its own session file and ignores the seed |
| Main-chat reading not live-tested | Unit-tested on both paths; no live run yet |
| Binding to a host is a separate call | The fork API takes no host or workspace |
