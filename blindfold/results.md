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

Measured on the E2E stack after the no-resident-pane fix:

| Harness | Turn 1 | Turns 2 and later |
|---|---|---|
| Claude Code | 3.0–4.4 s | 2.4–3.7 s |
| Pi | 1.3–3.0 s | 1.3–3.0 s |
| Codex | 2.6–5.5 s | 2.6–5.5 s |

CLI startup is only 5–22% of a turn: about 0.5 s for Claude, 0.15 s for Pi
and 0.12–0.17 s for Codex. The model call dominates.

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

- **Warm harness.** Keep a CLI process between turns, but only when it is
  provably safe: the system and memory digests are unchanged, and the history
  has only grown at the end. Any slide of the window means discard and start
  cold. Given the startup share above, it is opt-in and must earn its place
  with measured gains.
- **Compaction and summaries** in the assembler's own table, a real memory
  store, and a budget-aware window (see contract.md).
