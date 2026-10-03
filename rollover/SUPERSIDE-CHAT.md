# The superside-chat contract

For a team or backend adopting the Super Chat, its Side Chats and its
Sub-agents on top of Omnigent. Terms are defined once, in
[CONTEXT.md](CONTEXT.md). The ground rule is
[ADR 0001](adr/0001-omnigent-owns-everything.md): Omnigent owns every
capability; the engine (Claude SDK) only runs the model loop. Build history:
[SUPERSIDE-CHAT-PLAN.md](SUPERSIDE-CHAT-PLAN.md) (slices S1-S7, all merged).

Status: **built, unit-tested, and run live end to end on the Claude SDK engine
(Haiku, 2026-10-03)**: Rollover (refresh on return), Sub-agents, Side
Chats (with context, blank, archive), Memory Profile, Upkeep and the
Activity Feed. See [Live test](#live-test).

## What the mode is

`omnigent.context.mode=superside-chat` is a session label, set once when a
user's Super Chat is created. Side Chats and Sub-agents created from it
inherit it. A session without it behaves exactly as before.

| Mode value | Meaning |
|---|---|
| unset | Plain Omnigent, unchanged |
| `rollover` | Native-CLI context management ([README.md](README.md)) |
| `superside-chat` | This contract, on the Claude SDK engine |

What the mode turns on, for the session and everything created from it:

| Capability | Behaviour |
|---|---|
| Engine adapter | The engine's own compaction, auto-memory, `CLAUDE.md` loading and built-in `Agent`/`Task` tool are off. The engine restarts when Omnigent's composed instructions change. |
| Rollover | Omnigent writes a summary + recent whole turns when the context reaches the threshold (after the turn, in the background) or on the first message after the idle period (before that turn). The next turn starts from it; a turn waits (max 60 s) for an in-flight rollover. |
| Recall | `session_history` (`read`, `search`, `status`, `list_chats`, `chat_id`) |
| Memory | `memory_*` tools; the Memory Profile is prepended to every Super Chat and Side Chat turn (never stored in the thread); Upkeep learns from the user's own words |
| Side Chats | `side_chat_open` (Super Chat only), with context or blank; archived after inactivity; unarchived when the user writes to it |
| Sub-agents | Omnigent sessions launched by Sub-agent Type; start from Brief + Memory Profile; Result delivered in the wake message; one extra level of nesting; concurrency caps; cancel only by the Originating Chat |
| Activity Feed | Activities derived from stored work (chat turns with tool calls, Sub-agents), with plain-language Steps |

## What Omnigent provides vs. what a backend does

| Layer | Owner |
|---|---|
| Model loop | The Claude SDK engine, configured through an Omnigent agent bundle |
| Everything in the table above | Omnigent |
| The Super Chat bundle (prompt, Sub-agent Types) | The backend (start from `examples/super-chat/`) |
| Creating each user's Super Chat with the mode label | The backend |
| UI: Super Chat, Side Chat list, Activity Feed panel, read-only Sub-agent chats | The backend's UI, reading Omnigent's routes (the Omnigent web app has the components) |

## Labels

| Label | Meaning |
|---|---|
| `omnigent.context.mode=superside-chat` | Turns the capability on (set at creation) |
| `omnigent.context.rollover_at_tokens` | Rollover threshold override |
| `omnigent.context.rollover_keep_tokens` | Recent-turns budget in a rollover summary |
| `omnigent.side_chat`, `omnigent.fork.source_id` | Set by Omnigent on a Side Chat; link it to its Super Chat |

## Routes

| Route | Use |
|---|---|
| `POST /v1/sessions` (`labels`: the mode) | Create a user's Super Chat |
| `POST /v1/sessions/{id}/fork` (`side_chat: true`) | Side Chat with context (also what `side_chat_open` uses) |
| `POST /v1/hosts/{host_id}/runners` | Bind a new Side Chat to a host (done by `side_chat_open`) |
| `GET /v1/sessions/{id}/related_chats` | Side Chats of a Super Chat (and a Side Chat's parent) |
| `GET /v1/sessions/{id}/activities?before=&limit=` | Activity Feed, newest first, each with a `date` for grouping |
| `GET /v1/sessions/{id}/activities/{activity_id}` | One Activity with its Steps |
| `GET /v1/sessions/{id}/memory/profile` | The user's Memory Profile block |
| `POST /v1/sessions/{id}/memory/remember`, `GET .../memory/search`, `GET .../memory/claims/{id}`, `GET .../memory/claims/{id}/explain`, `POST .../memory/forget` | Memory |
| `GET /v1/sessions/{id}/items/search` | Full-text search inside one session |

## Tools the model sees

| Tool | Notes |
|---|---|
| `session_history` | Recall, including other chats of the same user |
| `memory_remember`, `memory_search`, `memory_get`, `memory_explain`, `memory_forget` | Needs the `omnigent[memory]` extra on the server |
| `side_chat_open(title, start, first_message?)` | `start`: `with_context` or `blank`; refused outside the Super Chat |
| `sys_session_send` / `sys_session_create` | Launch a Sub-agent by Type; `model`/`reasoning_effort` overrides are refused |
| `sys_cancel_task` | Cancel a Sub-agent; only its Originating Chat |
| `sys_read_inbox`, `sys_session_get_history` | Read results and a Sub-agent's full thread |

## Events and records

| Record | Notes |
|---|---|
| `compaction` item | A rollover: `summary`, `compacted_messages`, `last_item_id`, `token_count`, `model` |
| Wake message with the Result | Starts a turn in the Originating Chat; if that Side Chat is archived, goes to the Super Chat with a note |
| Upkeep run | Per user: window, counts (inserted, reinforced, superseded, rejected) |

## Settings

| Setting | Where | Default |
|---|---|---|
| Rollover threshold | label `omnigent.context.rollover_at_tokens` | 60% of the model's window, 100k-200k |
| Recent-turns budget | label `omnigent.context.rollover_keep_tokens` | 16,000 tokens |
| Idle refresh | env `OMNIGENT_ROLLOVER_IDLE_REFRESH_SECONDS` | 43200 (12 h); lower for testing |
| Side Chat inactivity archive | env `OMNIGENT_SIDE_CHAT_ARCHIVE_AFTER_SECONDS` | 2592000 (1 month); 3600 for testing |
| Sub-agent concurrency | env `OMNIGENT_SUBAGENT_MAX_CONCURRENT` + each Type's `max_sessions` in its `config.yaml` | 10 per caller; per-Type as configured |
| Memory embeddings | env `OMNIGENT_MEMORY_EMBEDDINGS_MODEL` + the provider key on the server | `openai/text-embedding-3-small` |
| Upkeep model | the server config's `llm:` block (`omnigent server --config`) | none: Upkeep runs are recorded as skipped (`no_llm_configured`) |

## Known limits

| Limit | Note |
|---|---|
| Activity Feed panel placement | The UI component exists but is not mounted in the app yet |
| Side Chat archive sweep | Runs per workspace (no cross-workspace query exists) |
| `?stream=true` turn path | Skips the turn-start hooks (idle refresh, deferred engine refresh); the default path is covered |
| Concurrency caps | Count the caller's direct children, not the whole tree |
| A runner crash mid-task | Stops in-flight Sub-agents; finished Results are recovered |
| Archived Side Chat | Only the wake is redirected to the Super Chat; the inbox entry stays with the Side Chat |
| "Cancelled" Activity status | Inferred from stored items; there is no separate cancelled marker |
| Upkeep window | Each run reads the newest ~30k tokens of the user's messages; a first run over a long history learns from the recent part only |
| Rollover summary wording | The checkpoint text says the conversation "grew past its context limit" even when the cause was idle time |

## Adoption checklist

For another backend built on Omnigent:

1. **Ship a Super Chat bundle**: `executor.type: omnigent`,
   `executor.config.harness: claude-sdk`, conversational, with Sub-agent
   Types under `agents/<type>/config.yaml` (model, effort, `max_sessions`).
   Start from `examples/super-chat/`. Pin models through provider config, not
   committed YAML.
2. **Create each user's Super Chat once** with
   `omnigent.context.mode=superside-chat`.
3. **Install the memory extra** (`omnigent[memory]`) on the server and set the
   embeddings model and its provider key. Give the server an `llm:` block
   (`omnigent server --config`) so Upkeep can run, for example:

   ```yaml
   llm:
     model: anthropic/claude-haiku-4-5-20251001
     connection:
       api_key: ${ANTHROPIC_API_KEY}
       base_url: https://api.anthropic.com/v1
   ```
4. **Set the settings above** (or keep the defaults; use the testing values
   while trying it out). The runner-side ones (idle refresh, Sub-agent
   concurrency) are set on the host process; the host forwards them to each
   runner.
5. **Build the UI** on the routes: the Super Chat, its Side Chats
   (`related_chats`), the Activity Feed (`activities`) and read-only
   Sub-agent chats.
6. **Run the unit tests**, one file at a time:
   `tests/context`, `tests/superchat`, `tests/memory`,
   `tests/tools/test_manager.py`, `tests/tools/builtins/test_session_history.py`,
   `tests/runner/test_runner_dispatch.py`,
   `tests/runner/test_side_chat_tool_dispatch.py`,
   `tests/runner/test_superside_chat_rollover_scheduling.py`,
   `tests/runner/test_superside_chat_subagent_wake.py`,
   `tests/runner/test_memory_profile_block.py`,
   `tests/inner/test_claude_sdk_executor.py`,
   `tests/server/routes/test_sessions_fork.py`,
   `tests/server/integration/test_sessions_activities.py`,
   `tests/spec/test_parser.py`, `tests/host/test_connect.py`,
   `tests/server/test_memory_upkeep_scheduler.py`,
   `tests/server/integration/test_sessions_items_search.py`.

## Live test

Run against a local server and a Docker runner, model `claude-haiku-4-5`, with
idle refresh at 60 s and Side Chat archive at 60 s.

| Check | Result |
|---|---|
| Engine adapter | Engine started with auto-compaction, auto-memory, setting sources and `Agent`/`Task` off; Omnigent tools present |
| Refresh on return | After 75 s idle, the next message rolled the chat over; the answer still used a fact from before |
| Sub-agent | Launched by Type; started from Brief + Memory Profile with the mode inherited; Result delivered in the wake; the assistant spoke first; a `model` override was refused |
| Side Chats | With context answered from the Super Chat's seed; blank did not; a Side Chat could not open another; archived after inactivity, still readable by the Super Chat, unarchived when written to |
| Memory | Memory Profile answered a question with no tool call; Upkeep after a rollover added a fact the user mentioned in passing and rejected two candidates whose quotes were not verbatim |
| Activity Feed | Chat turns (titled by the request) and the Sub-agent listed, each with Steps |

Bugs the live run found, all fixed with tests: runner-side settings stripped
by the host's env filter; the rollover summary called with the agent name
and routed to the wrong provider; the with-context Side Chat not seeded for
this mode, and its seed carrying the request that opened it; Side Chats able
to open Side Chats (label cache dropped `omnigent.side_chat`); archived Side
Chats unreadable; Upkeep overflowing on a long history and missing JSON after
prose; Activity titles repeating the chat title.
