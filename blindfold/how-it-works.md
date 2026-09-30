# How one blindfolded turn works

```
web UI / API ──message──▶ server ──turn──▶ runner ──▶ native executor.run_turn
                                                         │
                                  blindfolded?  no ──────┴──▶ upstream path (resident CLI)
                                                yes
                                                 │
   1. record the user message           POST /v1/sessions/{id}/events (external_conversation_item)
   2. assemble the context              POST /v1/sessions/{id}/context
   3. build a disposable CLI session    fresh config dir + history file + system text
   4. run the CLI once                  claude -p / pi --print / codex exec
   5. record tool calls and the answer   external_conversation_item events + TurnComplete
   6. observe, and mark the turn done   POST /context/observe, external_session_status
   7. delete the config dir
```

## Where the switch is checked

- **Launch** (`omnigent/runner/native/orchestration.py`): when the runner
  prepares a native session, it writes `omnigent-server-connection.json` (mode
  600) into the session's bridge directory, but **only for blindfolded
  sessions**. For a blindfolded session it also launches **no resident CLI
  pane**: the one-shot CLI replaces it. `_ensure_native_terminal` answers
  200 with no terminal, so the server doesn't treat the missing pane as a
  failed turn.
- **Every turn** (`omnigent/inner/{claude,pi,codex}_native_executor.py`): the
  top of `run_turn` calls `maybe_run_blindfold_turn`. With no connection file
  it returns `handled=False` at once and the upstream code runs unchanged: a
  non-blindfolded session pays for one missing-file check and makes no network
  calls.

## Where the context gets injected

| | Claude Code | Pi | Codex |
|---|---|---|---|
| Disposable home | `CLAUDE_CONFIG_DIR` | `PI_CODING_AGENT_DIR` | `CODEX_HOME` (under `~/.omnigent-blindfold`; Codex refuses a temp dir) |
| System text + memory | `--append-system-prompt` | `--append-system-prompt` | `developer_instructions` in the fresh `config.toml` |
| History | Project JSONL written by `_claude_transcript_records_from_session_items`, then `--resume <id>` | Session file written by `pi_session_records_from_session_items`, then `--session <file>` | Rollout written by `_codex_rollout_records_from_session_items`, then `exec resume <id>` |
| New message | argument to `-p` | argument to `--print` | argument to `exec` |
| Vendor memory off | `--setting-sources ""`, `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`, `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` | `--no-context-files` | `-c project_doc_max_bytes=0`, a fresh `CODEX_HOME` with no `AGENTS.md` |
| Output | `--output-format stream-json --verbose` | `--mode json` | `--json --output-last-message` |
| Only its own key | `ANTHROPIC_API_KEY` | `OPENROUTER_API_KEY` | none in env; `auth.json` in the fresh home |

The history writers already existed in Omnigent (they rebuild a CLI's
history file on resume). Blindfold feeds them the assembler's **selected**
items instead of all items.

**Memory** is rendered as a block appended to the system text:

```
<agent instructions>

<long_term_memory>
- (fact) The user's codeword is MANGO-7
</long_term_memory>
```

The memory block is never written into the session record. Only the
model's own answer or reasoning can quote it.

## Credentials

`one_shot_env(keep=...)` in `omnigent/context_assembly/blindfold.py` removes
every `*_API_KEY`, `*_TOKEN`, `*_SECRET`, `*_PASSWORD` variable except the one
the CLI needs. Why: in testing, an agentic model ran `env` on its own, which
would otherwise have shown it every provider key on the host.

## Failure behaviour: fail closed

If the assembler errors or takes longer than 2 s, the turn gets **only the new
message**: never the whole history. If recording the user message fails, the
turn also runs with no history, rather than anchoring the window on the wrong
turn.

## Tool calls

The one-shot CLI replaces the resident pane, so the pane's own forwarder that
normally records tool calls never runs. `omnigent/context_assembly/oneshot_events.py`
parses each CLI's JSON events into `function_call`, `function_call_output`,
`reasoning` and `message` items. `post_oneshot_items` posts them to the
session. The final answer is posted once, by the normal `TurnComplete` path.
