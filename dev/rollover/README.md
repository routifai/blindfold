# Rollover E2E proof

Live end-to-end proof for the rollover super chat (`rollover/DESIGN.md`,
`rollover/PLAN.md`), driven through the real web UI + a Docker runner
container, the same shape as `dev/blindfold/blindfold_e2e.py` (see
`rollover-muse:dev/blindfold/README.md` for that pattern).

Own stack, own ports — never touches `:8780` / `omnigent-runner-test` or
`:8791`-`:8794`.

## One-time setup

```bash
cd /Users/simo/pttx/omnigent-ro-e2e
uv sync
uv sync --group dev            # pytest, ruff (for the regular test suite)
uv pip install playwright
uv run python -m playwright install chromium
```

## Server (host)

```bash
cd /Users/simo/pttx/omnigent-ro-e2e
mkdir -p .local-test-ro/data .local-test-ro/config .local-test-ro/logs
export OMNIGENT_DATA_DIR=/Users/simo/pttx/omnigent-ro-e2e/.local-test-ro/data
export OMNIGENT_CONFIG_HOME=/Users/simo/pttx/omnigent-ro-e2e/.local-test-ro/config
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

Provider config (models pinned per the task: `claude-haiku-4-5-20251001`,
`gpt-5-nano`):

```bash
cp dev/rollover/runner-config.example.yaml .local-test-ro/runner-config.yaml
```

Env file: `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`, reused from
`/Users/simo/pttx/omnigent-fresh/.local-test/runner.env` (never print its
values).

```bash
docker build -t omnigent-runner-ro-e2e -f dev/rollover/runner.Dockerfile .
docker rm -f omnigent-runner-ro-e2e 2>/dev/null
docker run -d --name omnigent-runner-ro-e2e \
  --env-file /Users/simo/pttx/omnigent-fresh/.local-test/runner.env \
  -v "$(pwd)/.local-test-ro/runner-config.yaml:/root/.omnigent/config.yaml" \
  omnigent-runner-ro-e2e \
  omnigent host --server http://host.docker.internal:8795 --non-interactive --no-open

docker exec omnigent-runner-ro-e2e sh -c 'mkdir -p /root/ws'   # the sessions' workspace
docker logs omnigent-runner-ro-e2e | tail -20                 # should show "Connected as ..."
curl -s http://127.0.0.1:8795/v1/hosts | python3 -m json.tool
```

## Run the proof

```bash
source .venv/bin/activate
python dev/rollover/rollover_e2e.py
```

Env overrides: `ROLLOVER_BASE_URL` (default `http://127.0.0.1:8795`),
`ROLLOVER_OUT_DIR` (default `rollover-e2e-out/`), `ROLLOVER_HOST_ID`,
`ROLLOVER_RUNNER_CONTAINER` (default `omnigent-runner-ro-e2e`).

Writes screenshots + `rollover-e2e-out/results.json`, and prints a
PASS/FAIL table for every case × harness at the end.

## What it exercises

For claude-native and codex-native, one continuous session with
`omnigent.context.mode=rollover` and a deliberately tiny
`omnigent.context.rollover_at_tokens=3000`:

1. **rollover_happens** — send a codeword, then filler turns (each asking
   for a ~150-word answer) until a `compaction` item appears. Checked: the
   fixed checkpoint header, the `## Context checkpoint — <date>` title, a
   `Current position / next step` closing section, the runner's
   `rollover applied ... pane_reaped=True` log line, and a pane-relaunch log
   line on the next turn.
2. **works_after_rollover** — the very next turn succeeds and streams
   (`first_output_s` measured from the DOM, like blindfold's).
3. **recall_verbatim** — "what was my first message, quote it exactly" —
   expects a `session_history` `function_call` item in the record and the
   exact original text in the answer.
4. **codeword_survives** — "what's my codeword" — answer must contain it
   (notes whether a `session_history` call fired, i.e. summary vs. recall).
5. **no_double_rollover** — the turn right after a rollover must not add a
   second `compaction` item.
6. **baseline** — same conversation shape, `omnigent.context.mode` unset:
   no `compaction` items, no `session_history` tool calls anywhere.
7. **side_chat** (claude-native only) — fork the rolled-over session with
   `side_chat: true`; the fork keeps the rollover label and must answer the
   codeword question from its seeded checkpoint.

It also runs a self-compaction audit: every `compaction` item in a rollover
session must have a matching `rollover applied ... pane_reaped=True` log
line; an extra item with no matching line would mean the CLI compacted on
its own.

## Auto-approving `session_history`

Claude Code's own interactive permission prompt would otherwise block the
first `session_history` tool call. The mechanism used here is the
documented, already-supported one — **not** tmux send-keys: session-create's
`terminal_launch_args` (the web UI's permission-mode / allowlist selector,
`omnigent/server/schemas.py`), merged into the CLI's own argv
(`_merge_allowed_tools` in `omnigent/harnesses/claude_native/bridge.py`):

- claude-native: `["--allowedTools", "mcp__omnigent__session_history"]`
  (the MCP server name is `omnigent`, so the relayed tool is
  `mcp__omnigent__session_history` — see `_MCP_SERVER_NAME` in
  `omnigent/harnesses/claude_native/bridge.py`).
- codex-native: `["--ask-for-approval", "never"]` (no narrower per-tool
  allowlist exists for codex-native today).

Omnigent's own policy engine (`omnigent/native/native_policy_hook.py`)
deliberately does **not** auto-approve a harness's own permission prompt —
by design, `POLICY_ACTION_ALLOW` (including the engine's no-policy default)
returns "no opinion" so the harness's native consent gate still runs; only
`terminal_launch_args` pre-arms it.

## Teardown

```bash
docker rm -f omnigent-runner-ro-e2e
kill "$(cat /Users/simo/pttx/omnigent-ro-e2e/.local-test-ro/logs/server.pid)" 2>/dev/null
docker rmi omnigent-runner-ro-e2e   # optional
rm -rf /Users/simo/pttx/omnigent-ro-e2e/.local-test-ro/data \
       /Users/simo/pttx/omnigent-ro-e2e/.local-test-ro/config
```
