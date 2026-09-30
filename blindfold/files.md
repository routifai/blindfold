# Files touched

27 files compared with upstream `main`: about 3,100 lines of production code
plus tests and dev tooling.

## New files: the blindfold logic

| File | What it does |
|---|---|
| `omnigent/context_assembly/__init__.py` | Package exports |
| `omnigent/context_assembly/labels.py` | Label keys (`omnigent.blindfold`, `omnigent.context.max_messages`, `omnigent.context.memory_fixture`) and `BLINDFOLD_SESSION_LABELS` |
| `omnigent/context_assembly/models.py` | Contract v0.2 request and response models |
| `omnigent/context_assembly/assembler.py` | The default policy: the message window (`select_history_refs`), the token ceiling, memory rendering (`render_system_text`), fail closed |
| `omnigent/context_assembly/blindfold.py` | Shared runner-side helpers: the connection file, `record_user_message`, `fetch_blindfold_turn_context`, `post_oneshot_items`, `one_shot_env` |
| `omnigent/context_assembly/oneshot_events.py` | Parsers for Claude `stream-json`, Pi `--mode json` and Codex `exec --json` output into session items |
| `omnigent/harnesses/claude_native/blindfold.py` | Claude Code one-shot turn |
| `omnigent/harnesses/pi_native/blindfold.py` | Pi one-shot turn |
| `omnigent/harnesses/codex_native/blindfold.py` | Codex one-shot turn |
| `omnigent/server/routes/sessions/routes_context.py` | `POST /context` and `POST /context/observe` |

## Existing files: small guarded branches

| File | Change | Guard |
|---|---|---|
| `omnigent/inner/claude_native_executor.py` | `run_turn` calls `maybe_run_blindfold_turn` first | Returns at once when there is no connection file |
| `omnigent/inner/pi_native_executor.py` | Same | Same |
| `omnigent/inner/codex_native_executor.py` | Same | Same |
| `omnigent/runner/native/orchestration.py` | Writes the connection file at launch; launch configs carry the labels; no resident pane for blindfolded sessions; `_ensure_native_terminal` answers 200 with no terminal | `is_blindfolded(labels)` |
| `omnigent/runner/app.py` | `_native_session_confirmed_blindfolded`; early return in `_ensure_native_terminal_for_turn` | Same |
| `omnigent/server/routes/sessions/__init__.py` | Registers the context routes | New routes only |
| `omnigent/server/routes/sessions/routes_core.py` | A side-chat fork drops `BLINDFOLD_SESSION_LABELS` | Only when `side_chat` is true |

## Tests

| File | Covers |
|---|---|
| `tests/test_context_assembly.py` | The window, counting, the ceiling, memory, fail closed |
| `tests/test_context_assembly_blindfold.py` | The connection file, env scrubbing, OFF unchanged |
| `tests/test_blindfold_harness_turn.py` | A one-shot turn per CLI, with fake CLIs |
| `tests/test_oneshot_events.py` | The event parsers |
| `tests/runner/test_app_sessions_native_blindfold_no_pane.py` | No resident pane; ensure-terminal returns 200 |
| `tests/server/routes/test_sessions_fork.py` | A side chat drops the blindfold labels |

## Dev tooling

`dev/blindfold/`: the E2E script (`blindfold_e2e.py`), the runner image
(`runner.Dockerfile`), the runner config example, and a README with the setup
steps.
