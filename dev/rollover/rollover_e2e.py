"""Rollover super-chat end-to-end proof, driven through the real web UI.

Not part of the pytest suite — a one-off proving script (mirrors
dev/blindfold/blindfold_e2e.py from rollover-muse), run by hand against a
live local server + docker runner per dev/rollover/README.md.

Usage: source .venv/bin/activate && python dev/rollover/rollover_e2e.py
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
from playwright.async_api import Page, async_playwright, expect

CONTAINER = os.environ.get("ROLLOVER_RUNNER_CONTAINER", "omnigent-runner-ro-e2e")
BASE_URL = os.environ.get("ROLLOVER_BASE_URL", "http://127.0.0.1:8795")
OUT_DIR = Path(os.environ.get("ROLLOVER_OUT_DIR", "rollover-e2e-out"))
RESULTS_PATH = OUT_DIR / "results.json"

AGENT_NAMES = {
    "claude-native": "claude-native-ui",
    "codex-native": "codex-native-ui",
    "pi-native": "pi-native-ui",
}
AGENTS: dict[str, str] = {}
HOST_ID = os.environ.get("ROLLOVER_HOST_ID", "")

# terminal_launch_args merged into the CLI's own argv at create time (the
# web UI's documented permission-mode/allowlist mechanism — see
# omnigent/server/schemas.py's SessionCreateRequest.terminal_launch_args).
# Auto-approves ONLY the read-only session_history recall tool so Claude
# Code's own interactive permission prompt never blocks the run; codex's
# approval gate is turned off entirely (no narrower per-tool allowlist
# exists there today).
AUTO_APPROVE_ARGS: dict[str, list[str]] = {
    "claude-native": ["--allowedTools", "mcp__omnigent__session_history"],
    "codex-native": ["--ask-for-approval", "never"],
}

TURN_TIMEOUT_S = 150.0
MAX_FILLER_TURNS = 8

FILLER_TOPICS = [
    "the printing press",
    "the telegraph",
    "the steam engine",
    "the compass",
    "the abacus",
    "the sextant",
    "the lighthouse",
    "the telephone",
]

results: list[dict[str, Any]] = []


def rollover_labels(
    *,
    rollover_at_tokens: int = 3000,
    keep_tokens: int | None = None,
    keep_messages: int | None = None,
) -> dict[str, str]:
    labels = {
        "omnigent.context.mode": "rollover",
        "omnigent.context.rollover_at_tokens": str(rollover_at_tokens),
    }
    if keep_tokens is not None:
        labels["omnigent.context.rollover_keep_tokens"] = str(keep_tokens)
    if keep_messages is not None:
        labels["omnigent.context.rollover_keep_messages"] = str(keep_messages)
    return labels


def resolve_agents_and_host() -> None:
    """Fill AGENTS from /v1/agents and HOST_ID from the first online /v1/hosts entry."""
    global HOST_ID
    agents = httpx.get(f"{BASE_URL}/v1/agents", params={"limit": 200}, timeout=30).json()["data"]
    by_name = {a.get("name"): a["id"] for a in agents}
    for harness, name in AGENT_NAMES.items():
        AGENTS[harness] = by_name[name]
    if not HOST_ID:
        body = httpx.get(f"{BASE_URL}/v1/hosts", timeout=30).json()
        hosts = body.get("hosts", body.get("data", []))
        HOST_ID = next(
            h.get("id") or h.get("host_id") for h in hosts if h.get("status") == "online"
        )


async def create_session(
    client: httpx.AsyncClient,
    agent_id: str,
    *,
    labels: dict[str, str] | None = None,
    terminal_launch_args: list[str] | None = None,
) -> str:
    body: dict[str, Any] = {
        "agent_id": agent_id,
        "host_id": HOST_ID,
        "workspace": "/root/ws",
        "labels": labels or {},
    }
    if terminal_launch_args is not None:
        body["terminal_launch_args"] = terminal_launch_args
    resp = await client.post(f"{BASE_URL}/v1/sessions", json=body)
    resp.raise_for_status()
    return resp.json()["id"]


async def fork_session(
    client: httpx.AsyncClient, source_id: str, *, side_chat: bool
) -> dict[str, Any]:
    resp = await client.post(
        f"{BASE_URL}/v1/sessions/{source_id}/fork", json={"side_chat": side_chat}
    )
    resp.raise_for_status()
    return resp.json()


async def session_labels(client: httpx.AsyncClient, session_id: str) -> dict[str, str]:
    resp = await client.get(f"{BASE_URL}/v1/sessions/{session_id}")
    resp.raise_for_status()
    return resp.json().get("labels") or {}


async def last_item_ids(client: httpx.AsyncClient, session_id: str, limit: int = 50) -> list[dict]:
    resp = await client.get(
        f"{BASE_URL}/v1/sessions/{session_id}/items", params={"limit": limit, "order": "desc"}
    )
    resp.raise_for_status()
    return resp.json().get("data") or []


async def compaction_items(client: httpx.AsyncClient, session_id: str) -> list[dict]:
    items = await last_item_ids(client, session_id, limit=200)
    return [i for i in items if i.get("type") == "compaction"]


def _assistant_text(item: dict) -> str:
    content = item.get("content") or []
    return "".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "output_text"
    )


async def send_message_and_wait(
    page: Page,
    client: httpx.AsyncClient,
    session_id: str,
    text: str,
    *,
    label: str,
) -> dict[str, Any]:
    """Type *text* into the composer, send it, and wait for the assistant's
    reply to land in the record. Returns timing + the reply text + the new
    items posted during this turn (for tool-call / compaction evidence)."""
    seen_before = {item["id"] for item in await last_item_ids(client, session_id)}
    composer = page.get_by_label("Message the agent")
    await expect(composer).to_be_visible(timeout=30_000)
    await composer.fill(text)
    t0 = time.monotonic()
    await page.get_by_role("button", name="Send", exact=True).click()

    first_output_s: float | None = None
    bubble_locator = page.locator('[data-testid="message-bubble"][data-role="assistant"]')
    deadline = time.monotonic() + TURN_TIMEOUT_S
    while time.monotonic() < deadline:
        count = await bubble_locator.count()
        if count > 0:
            txt = await bubble_locator.last.inner_text()
            if txt.strip():
                first_output_s = time.monotonic() - t0
                break
        await asyncio.sleep(0.2)

    reply_text = ""
    reply_item_id = None
    new_items: list[dict] = []
    while time.monotonic() < deadline:
        items = await last_item_ids(client, session_id, limit=100)
        new_items = [i for i in items if i["id"] not in seen_before]
        for item in new_items:
            if item.get("type") == "message" and item.get("role") == "assistant":
                reply_text = _assistant_text(item)
                reply_item_id = item["id"]
        if reply_item_id is not None:
            break
        await asyncio.sleep(0.3)
    done_s = time.monotonic() - t0

    shot_path = OUT_DIR / f"{label}.png"
    await page.screenshot(path=str(shot_path))

    return {
        "sent": text,
        "reply": reply_text,
        "first_output_s": first_output_s,
        "done_s": done_s,
        "screenshot": shot_path.name,
        "new_items": new_items,
    }


def _runner_log_text(session_id: str) -> str:
    """Concatenate every runner log file this session ever wrote (across
    container restarts — each restart rotates to a new timestamped file)."""
    try:
        proc = subprocess.run(
            [
                "docker",
                "exec",
                CONTAINER,
                "sh",
                "-c",
                f"cat /root/.omnigent/logs/runner/runner-{session_id}-*.log 2>/dev/null",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return proc.stdout
    except Exception as exc:  # noqa: BLE001 — best-effort evidence gathering
        return f"<could not fetch runner log: {exc}>"


def rollover_applied_log_lines(session_id: str) -> list[str]:
    """Grep for this session's 'rollover applied ... pane_reaped=True' lines."""
    text = _runner_log_text(session_id)
    return [
        line
        for line in text.splitlines()
        if f"rollover applied for {session_id}" in line and "pane_reaped=True" in line
    ]


def terminal_relaunch_log_lines(session_id: str) -> list[str]:
    """Grep for pane (re)launch lines: terminal auto-create / launch markers."""
    text = _runner_log_text(session_id)
    markers = ("terminal auto-create starting", "Codex terminal launch:")
    return [line for line in text.splitlines() if any(m in line for m in markers)]


def self_compaction_audit(session_id: str, our_compaction_count: int) -> dict[str, Any]:
    """Check for a compaction item NOT written by our own trigger.

    Our trigger writes exactly one 'rollover applied ... pane_reaped=True'
    log line per compaction item it creates. If the session holds more
    compaction items than we have matching log lines for, something else
    (a CLI's own self-compaction) wrote one.
    """
    our_log_lines = rollover_applied_log_lines(session_id)
    suspect = our_compaction_count > len(our_log_lines)
    return {
        "compaction_item_count": our_compaction_count,
        "rollover_applied_log_count": len(our_log_lines),
        "self_compaction_suspected": suspect,
    }


CHECKPOINT_HEADER_START = "This conversation grew past its context limit"


def checkpoint_shape_ok(summary: str) -> dict[str, bool]:
    return {
        "starts_with_fixed_header": summary.startswith(CHECKPOINT_HEADER_START),
        "has_checkpoint_title": "## Context checkpoint" in summary,
        "ends_with_current_position": "Current position" in summary.splitlines()[-4:][0]
        if len(summary.splitlines()) >= 1
        else False,
    }


def _has_current_position_section(summary: str) -> bool:
    return "Current position" in summary and "next step" in summary.lower()


async def wait_for_rollover(
    page: Page,
    client: httpx.AsyncClient,
    session_id: str,
    *,
    label_prefix: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Send filler turns until a compaction item appears (or the cap is hit).

    :returns: (filler_turns, compaction_items_after)
    """
    filler_turns = []
    for i in range(MAX_FILLER_TURNS):
        topic = FILLER_TOPICS[i % len(FILLER_TOPICS)]
        prompt = f"Please write a ~150 word answer about the history of {topic}."
        turn = await send_message_and_wait(
            page, client, session_id, prompt, label=f"{label_prefix}_filler_{i + 1}"
        )
        filler_turns.append(turn)
        comp = await compaction_items(client, session_id)
        if comp:
            return filler_turns, comp
    comp = await compaction_items(client, session_id)
    return filler_turns, comp


def _session_history_calls(items: list[dict]) -> list[dict]:
    return [
        i for i in items if i.get("type") == "function_call" and i.get("name") == "session_history"
    ]


async def run_rollover_suite(context, client: httpx.AsyncClient, harness: str) -> dict[str, Any]:
    """Cases 1-5 (+7 for claude-native), all against one long-running session."""
    codeword = f"CW-{harness.upper().replace('-', '')}-{secrets.token_hex(3).upper()}"
    first_message = f"My codeword is {codeword}. Remember it."

    sid = await create_session(
        client,
        AGENTS[harness],
        labels=rollover_labels(rollover_at_tokens=3000),
        terminal_launch_args=AUTO_APPROVE_ARGS[harness],
    )
    page = await context.new_page()
    await page.goto(f"{BASE_URL}/c/{sid}")

    turn1 = await send_message_and_wait(
        page, client, sid, first_message, label=f"{harness}_t1_codeword"
    )

    filler_turns, comp_items = await wait_for_rollover(page, client, sid, label_prefix=harness)
    rollover_happened = bool(comp_items)
    checkpoint = comp_items[-1] if comp_items else None
    checkpoint_summary = checkpoint.get("summary", "") if checkpoint else ""
    shape = checkpoint_shape_ok(checkpoint_summary) if checkpoint else {}
    rollover_log = rollover_applied_log_lines(sid)
    relaunch_log = terminal_relaunch_log_lines(sid)

    case1 = {
        "case": "rollover_happens",
        "pass": bool(
            rollover_happened
            and shape.get("starts_with_fixed_header")
            and shape.get("has_checkpoint_title")
            and _has_current_position_section(checkpoint_summary)
            and rollover_log
            and len(relaunch_log) >= 2
        ),
        "evidence": {
            "compaction_item_id": checkpoint.get("id") if checkpoint else None,
            "checkpoint_shape": shape,
            "rollover_applied_log_line": rollover_log[0] if rollover_log else None,
            "terminal_relaunch_log_line_count": len(relaunch_log),
            "filler_turns_until_rollover": len(filler_turns),
        },
    }

    # Case 2: the very next turn after the rollover must succeed and stream.
    post_rollover_turn = await send_message_and_wait(
        page,
        client,
        sid,
        "Please write a ~80 word fact about clocks.",
        label=f"{harness}_post_rollover",
    )
    case2 = {
        "case": "works_after_rollover",
        "pass": bool(
            post_rollover_turn["reply"]
            and post_rollover_turn["first_output_s"] is not None
            and post_rollover_turn["first_output_s"] < post_rollover_turn["done_s"] + 0.01
        ),
        "evidence": {
            "first_output_s": post_rollover_turn["first_output_s"],
            "done_s": post_rollover_turn["done_s"],
        },
    }

    # Case 5: that same turn must not have triggered a second rollover.
    comp_after_post = await compaction_items(client, sid)
    case5 = {
        "case": "no_double_rollover",
        "pass": len(comp_after_post) == len(comp_items),
        "evidence": {
            "compaction_count_before": len(comp_items),
            "compaction_count_after_next_turn": len(comp_after_post),
        },
    }

    # Case 3: recall the exact first message via session_history.
    recall_turn = await send_message_and_wait(
        page,
        client,
        sid,
        "What was my very first message in this conversation? Quote it exactly.",
        label=f"{harness}_recall",
    )
    recall_calls = _session_history_calls(recall_turn["new_items"])
    case3 = {
        "case": "recall_verbatim",
        "pass": bool(recall_calls) and first_message in recall_turn["reply"],
        "evidence": {
            "session_history_call_count": len(recall_calls),
            "reply": recall_turn["reply"][:300],
        },
    }

    # Case 4: codeword survives the rollover (summary or recall).
    cw_turn = await send_message_and_wait(
        page, client, sid, "What's my codeword?", label=f"{harness}_codeword_check"
    )
    cw_calls = _session_history_calls(cw_turn["new_items"])
    case4 = {
        "case": "codeword_survives",
        "pass": codeword in cw_turn["reply"],
        "evidence": {
            "source": "recall" if cw_calls else "summary",
            "reply": cw_turn["reply"][:200],
        },
    }

    cases = [case1, case2, case3, case4, case5]

    # Case 7: side chat forked after a rollover (claude-native only).
    if harness == "claude-native":
        fork = await fork_session(client, sid, side_chat=True)
        side_id = fork["id"]
        side_labels = fork.get("labels") or {}
        side_page = await context.new_page()
        await side_page.goto(f"{BASE_URL}/c/{side_id}")
        side_turn = await send_message_and_wait(
            side_page, client, side_id, "What's my codeword?", label=f"{harness}_side_chat"
        )
        case7 = {
            "case": "side_chat",
            "pass": bool(
                side_labels.get("omnigent.context.mode") == "rollover"
                and codeword in side_turn["reply"]
            ),
            "evidence": {
                "side_chat_session_id": side_id,
                "side_chat_labels": side_labels,
                "reply": side_turn["reply"][:200],
            },
        }
        cases.append(case7)
        await side_page.close()

    audit = self_compaction_audit(
        sid, len(comp_items) + (0 if len(comp_after_post) == len(comp_items) else 1)
    )

    await page.close()

    record = {
        "harness": harness,
        "session_id": sid,
        "codeword": codeword,
        "cases": cases,
        "turns": {
            "t1_codeword": turn1,
            "fillers": filler_turns,
            "post_rollover": post_rollover_turn,
            "recall": recall_turn,
            "codeword_check": cw_turn,
        },
        "checkpoint_summary_full": checkpoint_summary,
        "self_compaction_audit": audit,
    }
    results.append(record)
    print(f"[{harness}] session={sid} suite done", flush=True)
    for c in cases:
        print(f"    {c['case']}: {'PASS' if c['pass'] else 'FAIL'} {c['evidence']}", flush=True)
    return record


async def run_baseline(
    context, client: httpx.AsyncClient, harness: str, filler_count: int
) -> dict[str, Any]:
    """Case 6: mode unset — no compaction items, no session_history tool ever."""
    codeword = f"CW-{harness.upper().replace('-', '')}-BASE-{secrets.token_hex(3).upper()}"
    sid = await create_session(client, AGENTS[harness], labels={}, terminal_launch_args=None)
    page = await context.new_page()
    await page.goto(f"{BASE_URL}/c/{sid}")

    await send_message_and_wait(
        page,
        client,
        sid,
        f"My codeword is {codeword}. Remember it.",
        label=f"{harness}_baseline_t1",
    )
    for i in range(filler_count):
        topic = FILLER_TOPICS[i % len(FILLER_TOPICS)]
        await send_message_and_wait(
            page,
            client,
            sid,
            f"Please write a ~150 word answer about the history of {topic}.",
            label=f"{harness}_baseline_filler_{i + 1}",
        )
    cw_turn = await send_message_and_wait(
        page, client, sid, "What's my codeword?", label=f"{harness}_baseline_codeword_check"
    )
    await page.close()

    all_items = await last_item_ids(client, sid, limit=200)
    comp = [i for i in all_items if i.get("type") == "compaction"]
    sh_calls = _session_history_calls(all_items)

    case6 = {
        "case": "baseline",
        "pass": bool(codeword in cw_turn["reply"] and not comp and not sh_calls),
        "evidence": {
            "compaction_item_count": len(comp),
            "session_history_call_count": len(sh_calls),
            "reply": cw_turn["reply"][:200],
        },
    }
    record = {
        "harness": harness,
        "session_id": sid,
        "codeword": codeword,
        "cases": [case6],
    }
    results.append(record)
    print(
        f"[{harness}] baseline session={sid} done: {'PASS' if case6['pass'] else 'FAIL'}",
        flush=True,
    )
    return record


async def main() -> None:
    resolve_agents_and_host()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=30.0) as client, async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context(viewport={"width": 1400, "height": 900})

        harness_records: dict[str, dict[str, Any]] = {}
        for harness in ("claude-native", "codex-native"):
            record = await run_rollover_suite(context, client, harness)
            harness_records[harness] = record
            filler_count = len(record["turns"]["fillers"])
            await run_baseline(context, client, harness, filler_count=max(filler_count, 1))

        await browser.close()

    RESULTS_PATH.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote {RESULTS_PATH}", flush=True)

    print("\n=== PASS/FAIL table ===", flush=True)
    for record in results:
        harness = record["harness"]
        for c in record["cases"]:
            verdict = "PASS" if c["pass"] else "FAIL"
            print(f"{harness:14s} {c['case']:20s} {verdict}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
