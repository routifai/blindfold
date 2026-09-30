# Blindfold mode

Omnigent is a **meta harness**: it drives other agent harnesses (Claude Code,
Pi, Codex) on your behalf. Blindfold mode makes it a meta harness in the full
sense: **meta context and meta memory**. Omnigent decides what the harness
sees on every turn: the system text, the long-term memory and the chat history.
The harness itself remembers nothing between turns.

| Doc | What it covers |
|---|---|
| [how-it-works.md](how-it-works.md) | One turn end to end, and where the context gets injected in each CLI |
| [switch.md](switch.md) | How blindfold is turned on and off (per session; super chat vs side chats) |
| [contract.md](contract.md) | The context-assembly contract v0.2 and how messages are counted |
| [files.md](files.md) | Every file touched, and why |
| [results.md](results.md) | End-to-end results, known limits, what is next |

## The problem

Without blindfold, a native harness session (claude-native, pi-native,
codex-native) is a long-running CLI process that owns its own memory:

- **The CLI's transcript.** Omnigent sends the CLI only the new message. The
  CLI remembers every earlier turn itself, in its process and its own session
  file. Omnigent has no way to make a running CLI forget something.
- **Vendor memory.** The CLI loads `CLAUDE.md`, `AGENTS.md`, `~/.claude`
  auto-memory, `~/.codex`, `~/.pi` on its own. We planted a codeword in those
  files and the model repeated it.
- **Resume.** When Omnigent resumes a native session, it rebuilds the CLI's
  history file from *all* of the session's items.

So "what the model knows" was decided by the CLI, not by us. A context
assembler (sliding window, summaries, long-term memory) is pointless if the
harness also remembers everything on its own.

## What blindfold does

For a blindfolded session, every turn:

1. Omnigent's **context assembler** chooses the context: system text, memory
   and the last N messages.
2. A **new, disposable CLI session** is created with only that context. Its
   history file is written in the CLI's own format, and vendor memory is off.
3. The CLI runs once, and its answer and tool calls are recorded in
   Omnigent's session.
4. The CLI session is deleted.

The Omnigent record still keeps every message, so the UI shows the whole chat.
Only the model's view is narrowed.

## Why not just "a new session with a new message" every turn?

A fresh session that gets only the new message knows nothing: that is the
`max_messages=1` case. Useful context needs the *history we chose* to go in
together with the new message. Nothing in Omnigent could do that for a native
CLI from the outside:

- Omnigent's API has no way to seed a new session with a chosen history.
  Resume replays everything.
- A new Omnigent session per turn would show dozens of sessions in the UI
  instead of one chat.
- Vendor memory would still leak into every new session.

So the change sits where Omnigent runs a turn: at each native executor's
`run_turn`, guarded by the session's switch.

**Rakazo does conceptually the same thing** (checked against
`elie222/rakazo`, `packages/adapters/src/pi-runtime.ts` and `executor.ts`). It
assembles history from its own database (a compacted summary, recalled memory
and recent messages), then creates a brand-new Pi agent every run with
`new Agent({ initialState: { systemPrompt, messages: history } })`. It uses
Pi as an **in-process library** (`pi-agent-core`), so it hands over the message
list in memory: no CLI, no files, no vendor memory.

We use the **native CLIs**, which cannot take a message list in memory. That
is the extra work here:

- writing the history into each CLI's own session-file format (reusing
  Omnigent's existing rebuilders);
- switching off each CLI's vendor memory;
- parsing each CLI's output events to get the tool calls back into the record.

## The rule the code follows

`omnigent.blindfold=true` → the context assembler owns the context.
Unset (the default) → Omnigent behaves exactly as upstream. The new logic lives
in `omnigent/context_assembly/` and `omnigent/harnesses/*_native/blindfold.py`.
The existing code gets only small branches, each guarded by the switch.
