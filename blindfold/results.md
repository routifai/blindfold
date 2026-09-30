# Results and limits

## End-to-end test

The web UI plus a runner in a Docker container stand in for hosted compute. The
container has the claude, pi and codex CLIs and the API keys in an env file.
Setup: `dev/blindfold/README.md`.

Models: Claude Code on `claude-haiku-4-5-20251001`, Pi on OpenRouter
`anthropic/claude-haiku-4.5`, Codex on `gpt-5-nano`.

Probe: *"What's my codeword? Answer only from what you already know in this
conversation: do not run any tools or read any files. Reply with the codeword
or UNKNOWN."*

Pass rule: **the model must not remember anything older than `max_messages`.**

Run 5 (2026-09-30):

| Case | claude-native | pi-native | codex-native |
|---|---|---|---|
| Blind (`max_messages=1`) | UNKNOWN ✅ | UNKNOWN ✅ | UNKNOWN ✅ |
| Injected (`max_messages=3`) | PAPAYA-42 ✅ | PAPAYA-42 ✅ | PAPAYA-42 ✅ |
| Window edge (codeword 3 messages back, `max_messages=3`) | UNKNOWN ✅ | UNKNOWN ✅ | UNKNOWN ✅ |
| Memory (MANGO-7 only in the assembler's memory) | MANGO-7 ✅ | MANGO-7 ✅ | MANGO-7 ✅ |
| No leaks (BANANA-99 planted in `CLAUDE.md`, `AGENTS.md`, `~/.claude`, `~/.codex`, `~/.pi`) | UNKNOWN ✅ | UNKNOWN ✅ | UNKNOWN ✅ |
| Tool calls recorded (`echo TOOL-CANARY-7`) | ✅ | ✅ | ❌ see below |
| Baseline (blindfold off: must remember) | remembers ✅ | remembers ✅ | remembers ✅ |

Notes:
- In the Pi memory case, Pi's *reasoning* quoted the memory fact. That is the
  model's own output, like its answer. The memory item and the
  `<long_term_memory>` block themselves never appear in the record. The E2E
  check was corrected to treat reasoning as model output.
- In the Codex tool-calls case, the call **was recorded** (`function_call`
  plus its output), but the command failed. The one-shot runs `codex exec`
  with Codex's default command sandbox, which needs bubblewrap, and bubblewrap
  cannot create namespaces inside the Docker container. The session's own
  sandbox opt-in (`omnigent.codex_native.bypass_sandbox`) is not yet passed to
  the one-shot. That decision is open.

## Latency

The one-shot CLI turn itself (runner log `process_s`): Claude Code 3–4.4 s,
Pi 1.3–3 s, Codex 2.6–5.5 s. CLI startup is only 5–22% of that: about 0.5 s
for Claude, 0.15 s for Pi and 0.12–0.17 s for Codex. The model call dominates.

**Open issue: a ~60 s hold in the web UI on every turn after the first.**

In run 5, turn 2 and later took 46–60 s end to end on all three CLIs. The
unblindfolded baseline took 2.6–7.7 s. A probe reproduced it: turn 1 took
3.2 s, turn 2 took 60.5 s.

The server reports the session as `idle` the whole time, and the CLI answers
in 3–4 s once the message arrives. The web UI holds the second message for
about 55 s before posting it. The cause is in how the UI decides a native turn
has finished, and it is not yet located. The earlier "turn-2 delay fixed"
result was measured without the browser.

## Direction

The super chat is moving to a rollover design instead of blindfolding every
turn:

- one normal resident session (streaming, steering, prompt cache);
- a rollover to a new session built by the assembler (summary, recent
  messages, memory) when the context gets long;
- a read-only recall tool over the full session record;
- a memory store.

Blindfold stays as an optional strict mode. The warm harness was built on
branches `bf-warm-claude` and `bf-warm-pi` and not merged. It was correct
(the window-slide discard was proven live), but saves about 0.3 s per turn.

## Known limits

- **No live streaming.** The answer and the tool-call items arrive when the
  one-shot finishes.
- **Codex item types not yet mapped:** `mcp_tool_call`, `image_view` and
  `image_generation`.
- **Runner-launched sessions only.** A `omnigent claude` started by hand from
  a terminal is not blindfolded.
- **Tool access is not context.** Without "do not run any tools", an agentic
  model can search the disk (`env`, `find`, `grep`) and read files in its
  workspace. Blindfold controls what is *given* to the model, not what its
  tools can reach.
- **No web UI toggle.** Blindfold is set with session labels through the API.

## Next

- **Warm harness** (built, not merged; see Direction). Keep a CLI process between turns, but only when it is
  provably safe: the system and memory digests are unchanged, and the history
  has only grown at the end. Any slide of the window means discard and start
  cold. Given the startup share above, it is opt-in and must earn its place
  with measured gains.
- **Compaction and summaries** in the assembler's own table, a real memory
  store, and a budget-aware window (see contract.md).
