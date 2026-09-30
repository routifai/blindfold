# Blindfold mode

One switch per session, `omnigent.blindfold=true`:

- **Off** (default): Omnigent behaves exactly as upstream.
- **On**: the context assembler owns what the harness knows. Every turn runs a **new, disposable CLI session** (claude-native, pi-native, codex-native) that sees only the assembler's system text, long-term memory block and chat history — no reattach to an earlier CLI session, no vendor memory (`CLAUDE.md`, `AGENTS.md`, `~/.claude`, `~/.codex`, `~/.pi`), and only the one API key that CLI needs.

The Omnigent record still keeps every message, so the UI shows the full chat.

## Labels

| Label | Meaning |
|---|---|
| `omnigent.blindfold=true` | Turn blindfold mode on for the session. Set it when creating the session. |
| `omnigent.context.max_messages=<N>` | History window: the last N messages (user + assistant), the last one always being the new message. Tool calls stay with their message. Server default: 20. |
| `omnigent.context.memory_fixture=<text>` | Test hook: one synthetic long-term memory item. |
| `omnigent.context.lifecycle=warm_if_valid` | pi-native only so far. Reuse a long-lived `pi --mode rpc` process across turns instead of a fresh one-shot every turn, whenever it's still valid (module docstring: `omnigent/harnesses/pi_native/blindfold_warm.py`). Unset (default) is `fresh` — today's behavior, unchanged. |

## How it works

1. Before a turn, the runner asks the server `POST /v1/sessions/{id}/context`; the in-process assembler (`omnigent/context_assembly/`) returns system text, memory and the selected history (contract v0.2).
2. The harness's existing rebuilder writes that history in the CLI's own format (Claude project JSONL, Pi session file, Codex rollout) into a fresh config directory.
3. The CLI runs once (`claude -p … --resume`, `pi --print --session …`, `codex exec resume …`) and is thrown away. Vendor memory is off: `--setting-sources ""` + `CLAUDE_CODE_DISABLE_CLAUDE_MDS`/`CLAUDE_CODE_DISABLE_AUTO_MEMORY`, `--no-context-files`, `-c project_doc_max_bytes=0`.
4. After the turn, the runner calls `POST /v1/sessions/{id}/context/observe`.

If the assembler fails, the turn fails closed: only the new message is sent, never the whole history.

## End-to-end test (web UI + a runner container as hosted compute)

1. Server with the web UI on the host, reachable from Docker:
   `OMNIGENT_LOCAL_SINGLE_USER=1 omnigent server --host 0.0.0.0 --port 8780 --no-open`
2. Runner image and container (claude, pi, codex CLIs; keys by env file):
   ```
   docker build -t omnigent-runner-test -f dev/blindfold/runner.Dockerfile .
   docker run -d --name omnigent-runner-test --env-file runner.env \
     -v "$(pwd)/runner-config.yaml:/root/.omnigent/config.yaml" \
     omnigent-runner-test omnigent host --server http://host.docker.internal:8780 --non-interactive --no-open
   ```
   `runner.env` holds `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, `OPENAI_API_KEY`; `runner-config.yaml` starts from `runner-config.example.yaml` (mounted read-write: the host writes its identity into it).
3. Plant canaries and a workspace inside the container:
   ```
   docker exec omnigent-runner-test sh -c 'mkdir -p /root/ws /root/.claude /root/.codex /root/.pi/agent; for f in /root/CLAUDE.md /root/AGENTS.md /root/.claude/CLAUDE.md /root/.codex/AGENTS.md /root/.pi/agent/AGENTS.md; do printf "The secret codeword is BANANA-99.\n" > $f; done'
   ```
4. Run: `python dev/blindfold/blindfold_e2e.py` (env: `BLINDFOLD_BASE_URL`, `BLINDFOLD_OUT_DIR`, `BLINDFOLD_HOST_ID`, `BLINDFOLD_RUNNER_CONTAINER`).

Every blindfolded case is also checked for runner log evidence that the blindfold path ran.

## Results (2026-09-30)

Claude Code on `claude-haiku-4-5-20251001`, Pi on OpenRouter `anthropic/claude-haiku-4.5`, Codex on `gpt-5-nano`. Probe: "What's my codeword? Answer only from what you already know in this conversation: do not run any tools or read any files. Reply with the codeword or UNKNOWN."

| Case | claude-native | pi-native | codex-native |
|---|---|---|---|
| Blind (`max_messages=1`) | UNKNOWN ✅ | UNKNOWN ✅ | UNKNOWN ✅ |
| Injected (`max_messages=3`) | PAPAYA-42 ✅ | PAPAYA-42 ✅ | PAPAYA-42 ✅ |
| Window edge (codeword 3 messages back, `max_messages=3`) | UNKNOWN ✅ | UNKNOWN ✅ | UNKNOWN ✅ |
| Memory (MANGO-7 only from the assembler's memory field) | MANGO-7 ✅ | MANGO-7 ✅ | MANGO-7 ✅ |
| No leaks (BANANA-99 planted in vendor memory and parent folder) | UNKNOWN ✅ | UNKNOWN ✅ | UNKNOWN ✅ |
| Baseline (blindfold off) | remembers ✅ | remembers ✅ | remembers ✅ |

The memory item and `<long_term_memory>` block never appear in the session record (the model's own answer does, as expected).

## Tool calls in the record

Each one-shot CLI invocation now uses its structured event-stream output
(Claude Code `--output-format stream-json --verbose`, Pi `--mode json`,
Codex `exec --json`) instead of plain text, so a turn's tool calls, their
results, and any reasoning reach the session record the same way a resident
native session's do (`function_call` / `function_call_output` / `message` /
`reasoning` conversation items) — see
`omnigent/context_assembly/oneshot_events.py` for the per-CLI parsers and
`omnigent/context_assembly/blindfold.py`'s `post_oneshot_items` for how
they're posted. The turn's own final answer is posted once, by the normal
`TurnComplete.response` path — a matching item is never also posted here.

Items are posted after the one-shot process finishes (in event order), not
incrementally while it runs, so the UI won't show a blindfolded turn's tool
calls until the whole turn completes.

Not yet mapped: Codex's `mcp_tool_call`, `image_view`, and
`image_generation` item types (mirroring the app-server forwarder's own gap
— `mcpToolCall`'s shape hasn't been verified against the real CLI either).

## Warm lifecycle (pi-native)

`omnigent.context.lifecycle=warm_if_valid` opts a pi-native session into
reusing one long-lived `pi --mode rpc` process across turns (started with the
same settings as the one-shot path: fresh `PI_CODING_AGENT_DIR`,
`--no-context-files`, `--append-system-prompt`, `--model`, `--session` of the
assembler-written history) instead of a fresh `pi --print` process every
turn. Every turn re-checks validity before reusing: process alive, not idle
past 10 minutes, system+memory prompt unchanged, model unchanged, and the
assembler's selected prior-history ids for this turn exactly match what the
process has already seen — a sliding `max_messages` window dropping an old
message fails this check rather than silently under-forgetting. Any failure
discards the process and starts a fresh one with the current turn's own
context; a crash mid-turn fails just that turn, exactly like a one-shot
process exiting non-zero. See `omnigent/harnesses/pi_native/blindfold_warm.py`
for the full design and `tests/test_blindfold_warm_pi.py` for the scenarios.
Unset (default), a pi-native session runs exactly as described above (one
fresh process per turn) — this label is off by default.

## Known limits

- No streaming: the answer (and the turn's tool-call items) arrive when the one-shot finishes.
- Blindfold applies to runner-launched sessions (web UI, API); a `omnigent claude` launched by hand from a terminal is not blindfolded.
- Without the probe's "do not run any tools" instruction, agentic models may search the disk (`env`, `find`, `grep`) and read files that exist in the workspace — that's tool access, not injected context.
