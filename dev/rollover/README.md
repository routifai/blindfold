# Rollover E2E proof

Live end-to-end proof for the rollover super chat (`rollover/README.md`),
driven through the real web UI and a Docker runner container.

Own stack, own ports — never touches `:8780` / `omnigent-runner-test` or
`:8791`-`:8794`.

## One-time setup

```bash
cd <repo>
uv sync
uv sync --group dev            # pytest, ruff (for the regular test suite)
uv pip install playwright
uv run python -m playwright install chromium
```

## Server (host)

```bash
cd <repo>
mkdir -p .local-test-ro/data .local-test-ro/config .local-test-ro/logs
export OMNIGENT_DATA_DIR=<repo>/.local-test-ro/data
export OMNIGENT_CONFIG_HOME=<repo>/.local-test-ro/config
export OMNIGENT_LOCAL_SINGLE_USER=1
source .venv/bin/activate
nohup omnigent server --host 0.0.0.0 --port 8795 --no-open \
  > .local-test-ro/logs/server.log 2>&1 &
echo $! > .local-test-ro/logs/server.pid
curl -s http://127.0.0.1:8795/health   # {"status":"ok"}
```

`.local-test-ro/` is git-ignored via `.git/info/exclude` (already added for
this worktree — shared with the other `omnigent-*` worktrees' throwaway
dirs, never committed).

## Runner container

```bash
cp dev/rollover/runner-config.example.yaml .local-test-ro/runner-config.yaml
```

Fill in each `default:` model id in that copy — the example ships with
`<your-default-model>` placeholders (`dev/lint/lint_no_hardcoded_models.py`
rejects real model ids committed to the repo). The script below overrides
the model per-harness anyway (see "Models"), so the config only matters as
a fallback for a harness whose env var is unset.

Env file: `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`, reused from
`<path/to/runner.env>` (never print its values).

```bash
docker build -t omnigent-runner-ro-e2e -f dev/rollover/runner.Dockerfile .
docker rm -f omnigent-runner-ro-e2e 2>/dev/null
docker run -d --name omnigent-runner-ro-e2e \
  --env-file <path/to/runner.env> \
  -v "$(pwd)/.local-test-ro/runner-config.yaml:/root/.omnigent/config.yaml" \
  omnigent-runner-ro-e2e \
  omnigent host --server http://host.docker.internal:8795 --non-interactive --no-open

docker exec omnigent-runner-ro-e2e sh -c 'mkdir -p /root/ws'   # the sessions' workspace
docker logs omnigent-runner-ro-e2e | tail -20                 # should show "Connected as ..."
curl -s http://127.0.0.1:8795/v1/hosts | python3 -m json.tool
```

## Models

No model id is hardcoded in `rollover_e2e.py` — the `no-hardcoded-models`
pre-commit hook forbids that outside tests. Set per-harness env vars before
running (any are optional; an unset one falls back to the runner config's
`default:`):

| Env var | Harness |
|---|---|
| `E2E_CLAUDE_MODEL` | claude-native |
| `E2E_CODEX_MODEL` | codex-native |
| `E2E_PI_MODEL` | pi-native |

```bash
export E2E_CLAUDE_MODEL=<claude model id>
export E2E_CODEX_MODEL=<codex model id>
```

`ROLLOVER_E2E_HARNESSES` picks the CLIs to run (comma-separated; default all three). `ROLLOVER_E2E_AT_TOKENS` optionally overrides `omnigent.context.rollover_at_tokens`
for every harness (still floored at 100k by `resolve_rollover_threshold`) —
useful for forcing more than one compaction within the fixed filler below on
a model with a very large context window, where the real 60%-of-window
default wouldn't otherwise be crossed.

## Run the proof

```bash
source .venv/bin/activate
python dev/rollover/rollover_e2e.py
```

Env overrides: `ROLLOVER_BASE_URL` (default `http://127.0.0.1:8795`),
`ROLLOVER_OUT_DIR` (default `rollover-e2e-out/`), `ROLLOVER_HOST_ID`,
`ROLLOVER_TURN_TIMEOUT_S` (default `240`).

Writes screenshots + `rollover-e2e-out/results.json`, and prints a
PASS/FAIL table for every case × harness at the end. A harness with no
matching agent registered on the server is skipped (so pi-native runs only
if a `pi-native-ui` agent exists).

## What it exercises

For each harness, one continuous session with `omnigent.context.mode=rollover`:
a codeword turn, then 11 dense filler turns (~100k tokens total — enough to
cross the real threshold, no artificially tiny threshold), then three probe
turns.

| Case | What it checks |
|---|---|
| `compacts` | ≥1 `compaction` item appeared during the fill |
| `no_loop` | compaction count ≤ `len(fill turns) / 2`, and no two consecutive fill turns each added one (the old design's failure mode: too low a threshold recompacts every turn) |
| `continues` | the turn right after the first compaction still replies and streams |
| `sees_tool` | asked "do you see a tool called session_history?" — answer starts with yes |
| `recall_verbatim` | the answer contains the exact first message; whether it came from a `session_history` call or the kept context is reported |
| `summary_recorded` | every `compaction` item has a non-empty `summary`; a count of fallback placeholder summaries (`"[Claude Code compaction — …]"`, the hook-only path with no transcript text) is reported as a warning, not a failure |
| `baseline` | a separate, cheap (3-turn) session with the mode label unset: no `compaction` items, no `session_history` calls — see "Cost note" for why this isn't a full-scale replica |
| `side_chat` (claude-native only) | fork the rolled-over session with `side_chat: true`; the fork keeps the rollover label and answers the codeword question from its Omnigent-seeded checkpoint |

## Cost note

The main suite's 11-turn fill is ~100k input tokens per harness, plus three
follow-up turns and the summarization call(s) the CLI's own compaction
triggers. `baseline` is deliberately 3 short turns, not a full replica of the
fill — rollover is opt-in purely via the mode label, so re-running the same
~100k-token fill with the label unset would double each harness's spend to
re-prove the same on/off switch.

## Dropped from the old design

The previous version of this script tested checkpoint-restart rollover:
Omnigent wrote the checkpoint text itself, reaped the CLI pane, and relaunched
it. That design is gone (see `rollover/README.md`) — each CLI now compacts
itself in place. Removed along with it:

- Runner-log grepping for `rollover applied ... pane_reaped=True` and pane
  relaunch markers (no pane restart happens anymore).
- The self-compaction audit (there's no longer an Omnigent-only trigger to
  audit against — the CLI's own compaction *is* the trigger).
- A tiny (3000-token) threshold and its dedicated filler-until-rollover loop —
  today's threshold is real (100k-200k) and can't be shrunk without looping.
- Checkpoint header/title/"Current position" shape assertions for
  claude-native and codex-native — their own CLI writes the compaction
  summary now, not Omnigent. Only Pi and side-chat seeds still get Omnigent's
  `CHECKPOINT_HEADER` (`build_side_chat_seed` in `omnigent/context/rollover.py`).
- The `--allowedTools` / `--ask-for-approval never` auto-approve launch args —
  `session_history` is pre-approved automatically for rollover sessions
  (`_ROLLOVER_ALLOWED_TOOLS` in `omnigent/runner/native/orchestration.py`),
  so no launch-arg workaround is needed; `terminal_launch_args` is used for
  `--model` overrides instead (see "Models").

## Teardown

```bash
docker rm -f omnigent-runner-ro-e2e
kill "$(cat <repo>/.local-test-ro/logs/server.pid)" 2>/dev/null
docker rmi omnigent-runner-ro-e2e   # optional
rm -rf <repo>/.local-test-ro/data \
       <repo>/.local-test-ro/config
```
