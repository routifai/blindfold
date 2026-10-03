---
status: accepted
---

# Omnigent owns everything; the engine only runs the model loop

The Super Chat (one per user), its Side Chats and its Sub-agents run on the
Claude SDK engine, switched on per session with
`omnigent.context.mode=superside-chat`. Every capability goes through
Omnigent's own layer: Sub-agents are Omnigent sub-agent sessions, long chats
are shortened by Omnigent's Rollover, Memory is Omnigent's `memory_*` store
and Memory Profile, and Side Chats, Activities and Results are Omnigent
sessions, items and routes. The engine's own versions of these are not used
and are turned off where they would compete (auto-compaction, auto-memory,
`CLAUDE.md` loading). We chose this so the design is portable to any backend
built on Omnigent, so the engine can be swapped, and so the records stay in
Omnigent's database under our rules rather than in the engine's local files.

## Considered Options

- **Native CLIs (Claude Code, Codex, Pi) as the engine.** Built first under
  the `rollover` mode. Rejected for the Super Chat: each CLI builds its own
  model requests, so Omnigent can only steer it from outside, and every bug
  found in that work was in that adapter layer (permission prompts, tool
  loading, compaction loops, the CLI's own memory).
- **The Claude SDK's built-in sub-agents** (`Agent` tool). They cover
  background runs, stop, resume and step logs, but their transcripts live as
  files on the engine's machine and are deleted after 30 days, they compact
  with the engine's rules, and resuming them is tied to an engine session that
  our Rollover replaces. A code check (2026-10-03) found Omnigent's sub-agent
  sessions already provide launch, background runs, result wake-up, real
  cancel, resume with durable history and nesting from a Claude SDK session,
  plus a sub-agent panel in the UI. The remaining gaps are small and
  isolated: deliver the Result itself in the wake message, pass the
  `superside-chat` mode and Memory Profile to children, a one-level nesting
  cap, a default concurrency cap, read-only sub-agent chats in the UI, and
  redirecting a Result when its Side Chat is archived.
- **The engine's own compaction.** No control over when it runs, what the
  summary keeps, or how it is recorded.

## Consequences

- Neither option survives a runner crash in the middle of a sub-agent's task;
  finished Results are recovered either way.
- Turning off engine features is part of every engine adapter, not optional.
