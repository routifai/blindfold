# Context-assembly contract v0.2

Two calls, served by the Omnigent server, over the Postgres/SQLite record it
already keeps. Both are owner/READ-authorized, the same as
`GET /v1/sessions/{id}/items`. The implementation is in
`omnigent/server/routes/sessions/routes_context.py` and
`omnigent/context_assembly/{assembler,models}.py`.

## `POST /v1/sessions/{id}/context`: assemble

Request (sent by the runner before each turn):

```json
{
  "contract_version": "0.2",
  "turn_id": "turn_…",
  "session": { "id": "…", "owner": "…", "labels": { "omnigent.blindfold": "true" } },
  "harness": {
    "name": "claude-native",
    "model": "claude-haiku-4-5-20251001",
    "context_window_tokens": 200000,
    "capabilities": { "history_format": "claude-jsonl", "images": true }
  },
  "new_item_id": "<id of the user message just recorded>",
  "record": { "item_count": 42, "last_item_id": "…" },
  "budget": { "max_input_tokens": 24000 }
}
```

Response:

```json
{
  "contract_version": "0.2",
  "turn_id": "turn_…",
  "system":  { "mode": "append", "text": "<agent instructions>", "digest": "…" },
  "memory":  { "items": [{ "id": "…", "kind": "fact", "text": "…" }], "digest": "…" },
  "history": { "summary": null, "items": [{ "ref": "<item id>" }], "digest": "…" },
  "audit":   { "memory_items": 0, "history_items": 5, "summary": false,
               "estimated_tokens": 812, "fallback": false }
}
```

- `system.text` is the agent's own instructions. `mode: append` means the CLI
  keeps its built-in base prompt.
- `memory.items` is always empty today: no memory store exists yet. The
  `memory_fixture` label fills it with one fact for tests.
- `history.items` are **references** to items already in the session record.
  The runner fetches them and writes them in the CLI's format.
- `history.summary` is reserved for compaction (see below).
- The digests let a caller tell cheaply whether a part changed since the last
  turn.

## `POST /v1/sessions/{id}/context/observe`: after the turn

```json
{ "contract_version": "0.2", "turn_id": "turn_…", "session_id": "…",
  "outcome": "completed", "new_item_ids": [], "usage": { "input_tokens": 0, "output_tokens": 0, "cost_usd": 0 } }
```

It returns 204. Today it only logs. It is the hook a future memory writer or
summarizer runs from.

## How messages are counted

`max_messages` counts **messages**: items of type `message`, from either the
user or the assistant. It does not count input and output tokens, and it does
not count turns.

- The window is the last N messages. The last one is always the new user
  message. So `max_messages=1` means the model sees only the new message, and
  `max_messages=3` means the new message plus the two messages before it
  (typically the previous user message and the reply to it).
- Tool calls, tool results and reasoning (`function_call`,
  `function_call_output`, `native_tool`, `reasoning`) **ride along
  uncounted** with the message they belong to (same `response_id`). A message
  and its tool calls are kept or dropped together, so a tool result is never
  cut off from its call.
- `budget.max_input_tokens` (24,000 by default) is a **ceiling** on top of the
  window. When the window is over it, whole message groups are dropped from the
  oldest end. The new message's own group is never dropped.
- Older messages are dropped. There is no summary yet, and the assembler never
  falls back to sending all history.

Example with `max_messages=3`:

```
user       "my codeword is PAPAYA-42"       ← dropped
assistant  "Noted."                         ← dropped
user       "run ls"                         ← message 1
  function_call  shell ls                   ← rides along, uncounted
  function_call_output  …                   ← rides along, uncounted
assistant  "Here are the files…"            ← message 2
user       "what's my codeword?"            ← message 3: the new message, always kept
```

The model does not know the codeword. `window_edge` in the E2E test checks
exactly this: a codeword sent 3 messages back must be forgotten.

## Fail closed

An assembler error or a timeout of more than 2 s gives an `audit.fallback:
true` response carrying only the new message.

## Reserved for later versions

- **Compaction and summaries.** `history.summary` holds a summary of what was
  dropped, stored in the assembler's own table. Omnigent's own `compaction`
  items are not used by the blindfold path.
- **A real long-term memory store** behind `memory.items`.
- **A budget-aware window** that sizes N from the model's context window.
- **Lifecycle** (`fresh` or `warm_if_valid`): whether a warm CLI may be reused
  (see results.md).
