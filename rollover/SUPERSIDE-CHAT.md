# The superside-chat contract

For a team or backend adopting the Super Chat, its Side Chats and its
Sub-agents on top of Omnigent. Language is defined once, in
[CONTEXT.md](CONTEXT.md) — this document never redefines a term, only says
what backs it. The ground rule is [ADR 0001](adr/0001-omnigent-owns-everything.md):
Omnigent owns every capability; the engine only runs the model loop.

Implementation status lives in [SUPERSIDE-CHAT-PLAN.md](SUPERSIDE-CHAT-PLAN.md)
(slices S1-S7). **As of this document, none of S1-S6 has landed in code** —
`omnigent.context.mode=superside-chat` does not exist yet as a label value,
and nothing in `omnigent/` is gated on it. What exists today is the
`rollover` mode (native CLIs: Claude Code, Codex, Pi — see
[README.md](README.md)) and the generic sub-agent/session primitives it
reused. Every row below says plainly which bucket it's in:

- **Built today** — ships now, under the `rollover` mode or as a
  mode-independent primitive.
- **Planned (slice Sn)** — designed in the plan, not yet gated on
  `superside-chat`, not yet callable for it.

Don't build against a "planned" row as if it works. If your backend needs
it sooner, that's a signal to pull the slice forward, not to route around
the contract.

## What the mode is

`omnigent.context.mode=superside-chat` is a session label, set **once, when
a user's Super Chat session is created**, same mechanism as the existing
`omnigent.context.mode=rollover` label (`omnigent/context/labels.py`). It
selects the Claude SDK engine and turns on, for that session and everything
forked or spawned from it: Rollover, `session_history` recall, the
`memory_*` tools, Side Chats, Sub-agents and (later) the Activity Feed. A
session without the label behaves exactly as it does today — this is
additive and gated, never a default.

| Mode value | What it means |
|---|---|
| unset | Plain Omnigent, unchanged |
| `rollover` | Native-CLI context management, built (README.md) |
| `superside-chat` | This contract — **planned (S1)** |

## What Omnigent provides vs. what a backend does

| Layer | Owner |
|---|---|
| Session storage, the record, items, labels, forking, sub-agent sessions, `memory_claims` store | Omnigent — built, mode-independent |
| Rollover (threshold, summary, recall), Side Chat seeding, `session_history`, `memory_*` tools | Omnigent — built for `rollover`; **gating them on `superside-chat` instead of/alongside `rollover` is S1-S2, planned** |
| Sub-agent launch by Sub-agent Type, Brief + Memory Profile at start, Result delivered in the wake message, nesting cap, concurrency cap | Omnigent — the underlying sub-agent session machinery (`sys_session_create/send/close/list/get_info/get_history`, `sys_cancel_task`) is built and mode-independent; the Sub-agent Type / Brief / concurrency-cap layer on top is **planned (S3)** |
| Side Chat "with context" / "blank" assistant tool, inactivity auto-archive | Fork-with-seed is built for `rollover` side chats; the assistant-facing open tool and the archive sweep are **planned (S4)** |
| Activity model and Activity Feed routes | **Planned (S5)** — nothing exists yet; derive, don't invent a parallel log |
| Memory Profile injected every turn, Upkeep job | `memory_*` tools and store are built (Phase 1, `MEMORY-PLAN.md`); always-on injection and Upkeep are **planned (S6)** |
| Agent bundle (`config.yaml`, `AGENTS.md`, `agents/<type>/`) | A backend ships its own, same format as any Omnigent agent image (`omnigent/spec/AGENTSPEC.md`) — see `examples/super-chat/` |
| NOVA UI, Chat List, Activity Panel | A separate frontend project (see CONTEXT.md's "Current scope" note) — not part of this contract |

## Routes, tools, labels, events

### Labels (set at session creation; see `omnigent/context/labels.py`)

| Label | Status | Meaning |
|---|---|---|
| `omnigent.context.mode=rollover` | Built | Native-CLI context management |
| `omnigent.context.mode=superside-chat` | **Planned (S1)** | This contract, on the Claude SDK engine |
| `omnigent.context.rollover_at_tokens` | Built (for `rollover`); applies to `superside-chat` once S1-S2 land | Per-session Rollover threshold override |
| `omnigent.context.rollover_keep_tokens` | Built (for `rollover`); applies to `superside-chat` once S1-S2 land | Verbatim recent-turns budget |

### Routes

| Route | Status | Use |
|---|---|---|
| `POST /v1/sessions/{id}/fork` (`side_chat: true`) | Built | Fork a Side Chat; keeps rollover-family labels, seeds one checkpoint |
| `POST /v1/hosts/{host_id}/runners` | Built | Bind a forked session to a host (required after fork) |
| `GET /v1/sessions/{id}/related_chats` | Built | Discover related Side Chats (native-CLI dispatch path for `session_history list_chats`) |
| `GET /v1/sessions/{id}/items/search` | Built | Full-text search within one session |
| `POST /v1/sessions/{id}/events` (`compaction` item) | Built | Record a Rollover checkpoint |
| `POST /v1/sessions/{id}/memory/remember` | Built | `memory_remember` (REST form) |
| `GET /v1/sessions/{id}/memory/search` | Built | `memory_search` (REST form) |
| `GET /v1/sessions/{id}/memory/claims/{claim_id}` | Built | `memory_get` |
| `GET /v1/sessions/{id}/memory/claims/{claim_id}/explain` | Built | `memory_explain` |
| `POST /v1/sessions/{id}/memory/forget` | Built | `memory_forget` (two-step: plan, then `confirm=true`) |
| Activity Feed routes (list by day, Steps of one Activity) | **Planned (S5)** | Not implemented; no path reserved yet |
| Side Chat "open with context / blank" route or tool | **Planned (S4)** | Fork exists; the assistant-facing decision point does not |

### Tools (in-process for Claude SDK; same contract as the native-CLI relay)

| Tool | Status | Notes |
|---|---|---|
| `session_history` (`read`, `search`, `status`, `list_chats`) | Built, gated on `is_rollover` today | Gating it on `is_superside_chat` too is S1 |
| `memory_remember`, `memory_search`, `memory_get`, `memory_explain`, `memory_forget` | Built, gated on rollover-session check today; requires the optional `omnigent[memory]` extra | Same S1 gating change |
| `sys_session_create` / `sys_session_send` / `sys_session_close` / `sys_session_list` / `sys_session_get_info` / `sys_session_get_history` | Built, mode-independent | The general sub-agent primitives Sub-agents are built from |
| `sys_cancel_task` | Built | Cancel a sub-agent; only its Originating Chat may call it |
| A Sub-agent-Type-aware spawn tool (launch by type, not raw model) | **Planned (S3)** (`superchat/subagents.py`, `tools/builtins/spawn.py`) | Today `sys_session_create` can target any agent id; nothing yet enforces "by Sub-agent Type only" |
| An assistant tool to open a Side Chat with/without context | **Planned (S4)** | — |

### Events / records

| Event | Status | Notes |
|---|---|---|
| `compaction` item (Rollover checkpoint) | Built | `summary`, `compacted_messages`, `last_item_id`, `token_count`, `model` |
| Sub-agent Result delivered in the wake message | **Planned (S3)** | Today the parent is woken and told to check its inbox; the Result text itself is not yet inlined |
| Result redirect to the Super Chat when the Side Chat is archived | **Planned (S3)** | — |
| Side Chat auto-archive on inactivity | **Planned (S4)** | — |
| Activity created/updated | **Planned (S5)** | — |

## Settings

| Setting | Label / config | Default | Status |
|---|---|---|---|
| Rollover threshold | `omnigent.context.rollover_at_tokens` | 60% of the model's context window, clamped 100k-200k (100k when the window is unknown) | Built for `rollover`; same mechanism for `superside-chat` once S1-S2 land |
| Rollover kept-tail budget | `omnigent.context.rollover_keep_tokens` | 16,000 tokens | Built |
| Idle refresh (rollover on first message after idle, not only at the token threshold) | — | — | **Planned (S2)** — not provided yet, even for `rollover` (see README's "Not provided yet") |
| Side Chat inactivity archive | — | **1 month in production, 1 hour for testing** (CONTEXT.md) | **Planned (S4)** — no sweep exists |
| Sub-agent concurrency cap (default, plus each Sub-agent Type's `max_sessions`) | `max_sessions` in a Sub-agent Type's own `config.yaml` | No default enforced today | **Planned (S3)**. The `AgentSpec.max_sessions` field exists (`omnigent/spec/types.py`), but the directory-bundle parser (`omnigent/spec/parser.py::parse`) does not read a top-level `max_sessions:` key from `config.yaml` into it yet — only the legacy single-file inline-agent loader (`omnigent/inner/loader.py`) wires a same-named key today, in a different format. `examples/super-chat/agents/*/config.yaml` declares `max_sessions` as a forward-looking, currently inert field; wiring it into the bundle parser is part of S3. |
| Memory embeddings model | env `OMNIGENT_MEMORY_EMBEDDINGS_MODEL` | `openai/text-embedding-3-small` (via litellm; needs `OPENAI_API_KEY` for that default) | Built (Phase 1, `MEMORY-PLAN.md`); requires the optional `omnigent[memory]` extra or the server mounts no memory routes |

## Adoption checklist

For another backend built on Omnigent that wants this capability:

1. **Ship a Super Chat agent bundle.** `executor.type: omnigent`,
   `executor.config.harness: claude-sdk`, `interaction.conversational: true`,
   and `tools.agents` listing your Sub-agent Types, each under its own
   `agents/<type>/config.yaml`. Never pin a literal model id in the bundle
   (the repo's `no-hardcoded-models` pre-commit hook enforces this for this
   repo's own examples; outside this repo, treat it as the same discipline
   — pin models through provider config, not committed YAML). See
   `examples/super-chat/` for a complete, parseable reference.
2. **Create each user's Super Chat once**, with
   `omnigent.context.mode=superside-chat` set at creation time — **once S1
   ships this label**. Until then, there is nothing to set; don't invent a
   substitute label.
3. **Show Side Chats and the Activity Feed from Omnigent's routes** —
   Side Chats today via `POST /v1/sessions/{id}/fork` (`side_chat: true`)
   plus `session_history`'s `list_chats`/`read`/`search`; the Activity Feed
   only once S5 ships its routes.
4. **Configure the settings above**: Rollover threshold and kept-tail
   budget, Side Chat inactivity period (1 month prod / 1 hour testing, once
   S4 ships the sweep), the default Sub-agent concurrency cap plus each
   Sub-agent Type's `max_sessions` (once S3 wires it), and the memory
   embeddings key if you install the `omnigent[memory]` extra.
5. **Run the unit tests this contract depends on** (light; one file at a
   time, no `-n`, per this repo's own laptop-safety rule):
   - `tests/context` — label gating and Rollover
   - `tests/tools/builtins/test_session_history.py`,
     `tests/runner/test_session_history_tool_dispatch.py` — recall, including
     `list_chats`/`chat_id` cross-chat reading
   - `tests/tools/builtins/test_memory.py`, `tests/memory`,
     `tests/stores/test_memory_store.py`,
     `tests/server/routes/test_session_memory_routes.py` — Memory
   - `tests/server/routes/test_sessions_fork.py` — Side Chat forking and seeding
   - `tests/spec` — agent bundle parsing/validation (what `examples/super-chat/`
     is checked against)
   - Once S1-S6 land: the same suites, re-run against `superside-chat`
     fixtures, plus whatever new test modules each slice adds (see
     SUPERSIDE-CHAT-PLAN.md for the file each slice touches).
6. **Don't claim more than what's gated.** If your backend's UI or docs
   describe a capability from the "Planned" rows above, label it
   not-yet-available in your own product surface, the way `CONTEXT.md`'s
   "Current scope" note requires NOVA's Wires to do.
