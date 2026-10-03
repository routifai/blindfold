# superside-chat: implementation plan

Turns on the Super Chat, Side Chats and Sub-agents for a session with
`omnigent.context.mode=superside-chat`, on the Claude SDK engine. Language:
[CONTEXT.md](CONTEXT.md). Ground rule: [ADR 0001](adr/0001-omnigent-owns-everything.md)
(Omnigent owns everything; the engine only runs the model loop). S7's
contract and adoption checklist: [SUPERSIDE-CHAT.md](SUPERSIDE-CHAT.md);
example bundle: [`examples/super-chat/`](../examples/super-chat/).

## Principles

- **Surgical.** Sessions without `superside-chat` behave exactly as before.
  Every change is gated on `is_superside_chat(labels)`.
- **One seam per concern.** New logic lives in `omnigent/superchat/`, one
  module per concern, each with a small public interface. Engine-specific code
  stays in the Claude SDK adapter only.
- **Reuse first.** Build on existing Omnigent primitives: sessions, items,
  `compaction` items, `sys_session_*`, the sub-agent wake, `session_history`,
  `memory_*`, agent bundles (`config.yaml`, `agents/<type>/`).
- **Portable.** Another backend adopts this by calling Omnigent's routes and
  shipping its own agent bundle; see the adoption checklist below.
- **Light on the laptop.** Unit tests only, one file at a time, no `-n`.

## Slices

Order: S1 first; S2, S3, S4 in parallel after it; then S5, S6, S7.

### S1 · The switch and the engine adapter (foundation)

| Change | Where |
|---|---|
| `SUPERSIDE_CHAT_MODE_VALUE`, `is_superside_chat(labels)`, inheritable label set | `omnigent/context/labels.py` |
| Side Chats and Sub-agents inherit the mode at creation | fork route (`routes_core.py`), sub-agent create body (`runner/tool_dispatch.py`) |
| Tools registered for `superside-chat` sessions on the SDK path: `session_history`, `memory_*` (thread `labels=` into the SDK tool-schema `ToolManager`) | `runner/app.py`, `tools/manager.py` |
| Turn off competing engine features for `superside-chat`: engine auto-compaction, auto-memory, `CLAUDE.md`/setting sources, built-in `Agent` tool | `inner/claude_sdk_executor.py` (adapter only) |
| Framework instructions reach a warm SDK client (today frozen at client start): rebuild the client when the composed instructions change | `inner/claude_sdk_executor.py` |

### S2 · Rollover on the Claude SDK

| Change | Where |
|---|---|
| Track context size per turn from the engine's usage | `superchat/rollover.py` (+ adapter hook) |
| At the threshold (`resolve_rollover_threshold`), or on the first message after the idle period (setting), write a rollover `compaction` item with Omnigent's summary + recent whole turns (`build_rollover_item`), then drop the warm client so the next turn cold-starts from it | `superchat/rollover.py`, `inner/claude_sdk_executor.py` |
| Applies to Super Chat, Side Chats and Sub-agents alike | — |

### S3 · Sub-agents

| Change | Where |
|---|---|
| Launch by Sub-agent Type (bundle under `agents/<type>/`); no raw model ids from the chat | `superchat/subagents.py`, `tools/builtins/spawn.py` |
| Child starts from Brief + Memory Profile and inherits the mode | `runner/tool_dispatch.py` create body |
| Deliver the Result itself in the wake message (not only "check your inbox") | `runner/app.py` wake notice |
| Nesting: one extra level (coordinator), refused below that | `runner/tool_dispatch.py` |
| Default concurrency cap (plus each type's `max_sessions`) | `runner/tool_dispatch.py` |
| Result redirect to the Super Chat when the Originating Side Chat is archived | `runner/app.py` wake target |
| Cancel only by the Originating Chat (`sys_cancel_task`) | existing; verify |

### S4 · Side Chats

| Change | Where |
|---|---|
| Assistant tool to open a Side Chat **with context** (seeded summary) or **blank** (Memory Profile only); only from the Super Chat | `superchat/chats.py`, new built-in tool |
| Auto-archive after inactivity (setting: 1 month production, 1 hour testing) | `superchat/chats.py` + a periodic sweep |
| Super Chat reads Side Chats on demand (`session_history` `list_chats` / `read(chat_id)`) | existing; verify for `superside-chat` |

### S5 · Activity Feed

| Change | Where |
|---|---|
| Activity model: one unit of work (a chat turn's multi-step work, or a Sub-agent), title, one-line outcome, status, Steps with plain-language titles | `superchat/activity.py` (derived from stored items) |
| Routes: list Activities for a user by day; Steps of one Activity | `server/routes/` |
| UI: side panel (feed grouped by day) + Step timeline; Sub-agent chats read-only | `web/src` |

### S6 · Memory for `superside-chat`

| Change | Where |
|---|---|
| Memory Profile sent with every Super Chat / Side Chat turn and to each new Sub-agent | `superchat/` + adapter (finish memory part 2) |
| Upkeep reads Super Chat, Side Chats and Sub-agents; user's own words only | finish memory part 3 |

### S7 · Contract, example bundle, adoption checklist

| Deliverable | Where |
|---|---|
| Contract: routes, labels, tools, events (Result, rollover, Activity) | `rollover/SUPERSIDE-CHAT.md` |
| Example Super Chat bundle with Sub-agent Types | `examples/super-chat/` |
| Adoption checklist for another Omnigent backend | in the contract |

## Adoption checklist (draft)

1. Ship a Super Chat agent bundle (`harness: claude-sdk`) with `agents/<type>/` Sub-agent Types.
2. Create each user's Super Chat once with `omnigent.context.mode=superside-chat`.
3. Show Side Chats and the Activity Feed from Omnigent's routes.
4. Configure: rollover threshold, idle refresh, Side Chat inactivity period, concurrency cap, memory embeddings key.
5. Run the unit tests listed in the contract.
