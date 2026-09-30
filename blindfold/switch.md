# Turning blindfold on and off

The switch is **per session** and **off by default**. There is no per-turn
switching: a session is either blindfolded for its whole life or not at all.

| Session | Blindfold | Who owns context and memory |
|---|---|---|
| **Super chat**: the one long-running main chat | **on** | The context assembler: a message window, long-term memory, and later summaries |
| **Side chat**: a normal session, including one opened from the super chat | **off** (default) | The harness itself: the CLI's own transcript and its own memory files, exactly as upstream Omnigent |

## Turning it on

Set the labels **when creating the session**:

```json
POST /v1/sessions
{
  "agent_id": "...",
  "labels": {
    "omnigent.blindfold": "true",
    "omnigent.context.max_messages": "20"
  }
}
```

| Label | Meaning | Default |
|---|---|---|
| `omnigent.blindfold` | `"true"` turns blindfold on for the session | unset = off |
| `omnigent.context.max_messages` | History window, in messages (see [contract.md](contract.md)) | 20 |
| `omnigent.context.memory_fixture` | Test hook: one synthetic long-term memory fact | unset |

Set the labels at creation, not later with a PATCH. The runner reads them when
it prepares the native session. If they arrive after that, the first turn can
run unblindfolded.

There is no toggle in the web UI yet. The labels are set through the API.

## Turning it off

Create the session without `omnigent.blindfold`. Nothing else changes: there is
no connection file, no assembler call and no one-shot CLI, and the resident CLI
pane runs as upstream.

## Side chats opened from the super chat

A side chat is a fork (`side_chat: true`). The fork route drops every
blindfold label (`BLINDFOLD_SESSION_LABELS`), so the side chat runs with the
harness's own memory. A plain fork (not a side chat) keeps the labels, and so
stays blindfolded.

This is covered by `tests/server/routes/test_sessions_fork.py::test_side_chat_from_blindfolded_session_uses_harness_memory`.
