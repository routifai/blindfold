# Rollover: the super chat, Muse-style

Status: implemented on branch `rollover`. The contract other components rely on
is in [CONTEXT-CONTRACT.md](CONTEXT-CONTRACT.md).

## Goal

One long-running **super chat** that feels like a normal session: streaming,
steering mid-turn, the prompt cache and tool calls all work. The model context
stays bounded, and the agent can still reach every message ever sent.

It has three parts, the same shape as Muse:

1. **Live session.** One normal, resident native CLI session (claude-native,
   codex-native, pi-native).
2. **Rollover.** When the context reaches the session's threshold, the CLI
   compacts itself in place. Omnigent sets the ceiling and records the result.
3. **Recall.** A read-only tool pages and searches the full session record.
   The rule: *the summary is a pointer, not the truth.*

## Why this design

| Option | Cost | Decision |
|---|---|---|
| New CLI session every turn (blindfold) | No streaming, steering or warm cache | Kept only as an optional strict mode |
| Omnigent writes the checkpoint and restarts the CLI | Restart per rollover; must never cut a turn in flight; more code to own | Replaced |
| **The CLI compacts itself at Omnigent's ceiling** | Summary format is the CLI's own on Claude Code and Codex | **Chosen**: no restart, no turn-in-flight race, least code |

## How each harness is set up

| Harness | Ceiling | Where |
|---|---|---|
| claude-native | `CLAUDE_CODE_AUTO_COMPACT_WINDOW` + `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` | `claude_auto_compact_env` in `harnesses/claude_native/main.py` |
| codex-native | `model_auto_compact_token_limit` | `codex_rollover_config_overrides` in `harnesses/codex_native/launch_args.py` |
| pi-native | Extension compacts after a settled turn at the threshold, with Omnigent's summary | `resources/pi_native/omnigent_pi_native_extension.js` |

Each compaction is recorded as a `compaction` item by the harness forwarder or
the Pi extension. Recall is the `session_history` built-in tool, and the rule
for using it is `ROLLOVER_CONTEXT_INSTRUCTION` in `runtime/prompt.py`.

## Open items

- Models don't reliably call recall on their own; automatic per-turn
  retrieval is the next step.
- Refresh after inactivity, source links in summaries, pruning between
  rollovers.
- Memory: see "Integration: long-term memory" in the contract.
