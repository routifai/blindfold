"""Blindfold-mode end-to-end proof, driven through the real web UI.

Not part of the pytest suite (this is a one-off proving script, run by hand
against the live local server + docker runner per .local-test/RUNNING.md) —
lives under .local-test/ so it's git-ignored, matching the rest of that dir.

Usage: source .venv/bin/activate && python .local-test/blindfold_e2e.py
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
from playwright.async_api import Page, async_playwright, expect

# Configure with env vars; defaults match dev/blindfold/README.md's local setup.
CONTAINER = os.environ.get("BLINDFOLD_RUNNER_CONTAINER", "omnigent-runner-test")
BASE_URL = os.environ.get("BLINDFOLD_BASE_URL", "http://127.0.0.1:8780")
SCREENSHOT_DIR = Path(os.environ.get("BLINDFOLD_OUT_DIR", "blindfold-e2e-out"))
RESULTS_PATH = SCREENSHOT_DIR / "results.json"

# Resolved at startup from the server (agent ids and the runner host id differ
# per deployment): see resolve_agents_and_host().
AGENT_NAMES = {
    "claude-native": "claude-native-ui",
    "pi-native": "pi-native-ui",
    "codex-native": "codex-native-ui",
}
AGENTS: dict[str, str] = {}
HOST_ID = os.environ.get("BLINDFOLD_HOST_ID", "")


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


# Per-turn wait budget. Codex/claude cold model calls plus a fresh CLI boot
# can take a while; generous but bounded. (A second+ turn in a blindfolded
# session has an extra ~60s tax from the runner's native-turn idle tracking —
# see the post_external_session_status call in each harness's blindfold.py.)
TURN_TIMEOUT_S = 150.0

results: list[dict[str, Any]] = []

# One unambiguous probe for every blindfolded case. "What was the last message
# I sent?" is ambiguous on Codex, which inserts its own <environment_context>
# user message whenever it resumes a thread.
QUESTION = (
    "What's my codeword? Answer only from what you already know in this conversation: "
    "do not run any tools or read any files. Reply with the codeword or UNKNOWN."
)


async def create_session(
    client: httpx.AsyncClient, agent_id: str, labels: dict[str, str] | None = None
) -> str:
    # Labels go in at creation: the runner snapshots them when it prepares the
    # harness, so a later PATCH can race that and silently skip blindfold.
    resp = await client.post(
        f"{BASE_URL}/v1/sessions",
        json={
            "agent_id": agent_id,
            "host_id": HOST_ID,
            "workspace": "/root/ws",
            "labels": labels or {},
        },
    )
    resp.raise_for_status()
    return resp.json()["id"]


async def set_labels(client: httpx.AsyncClient, session_id: str, labels: dict[str, str]) -> None:
    resp = await client.patch(
        f"{BASE_URL}/v1/sessions/{session_id}",
        json={"labels": labels},
        params={"include_usage": "false"},
    )
    resp.raise_for_status()


async def last_item_ids(client: httpx.AsyncClient, session_id: str, limit: int = 20) -> list[dict]:
    resp = await client.get(
        f"{BASE_URL}/v1/sessions/{session_id}/items", params={"limit": limit, "order": "desc"}
    )
    resp.raise_for_status()
    return resp.json().get("data") or []


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
    reply to land in the record. Returns timing + the reply text."""
    seen_before = {item["id"] for item in await last_item_ids(client, session_id)}
    composer = page.get_by_label("Message the agent")
    await expect(composer).to_be_visible(timeout=30_000)
    await composer.fill(text)
    t0 = time.monotonic()
    await page.get_by_role("button", name="Send", exact=True).click()

    # First visible output: poll the DOM for a NEW assistant bubble with
    # non-empty text. Best-effort — a one-shot blindfold turn has no partial
    # streaming, so this lands at (or very near) full completion; a
    # streaming baseline turn should show a real, smaller value here.
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

    # Completion: poll the record (authoritative) for a new assistant
    # message item that wasn't there before we sent.
    reply_text = ""
    reply_item_id = None
    while time.monotonic() < deadline:
        items = await last_item_ids(client, session_id)
        for item in items:
            if (
                item["id"] not in seen_before
                and item.get("type") == "message"
                and item.get("role") == "assistant"
            ):
                reply_text = _assistant_text(item)
                reply_item_id = item["id"]
                break
        if reply_item_id is not None:
            break
        await asyncio.sleep(0.3)
    done_s = time.monotonic() - t0

    shot_path = SCREENSHOT_DIR / f"{label}.png"
    await page.screenshot(path=str(shot_path))

    return {
        "sent": text,
        "reply": reply_text,
        "first_output_s": first_output_s,
        "done_s": done_s,
        "screenshot": shot_path.name,
    }


def blindfold_log_lines(session_id: str) -> list[str]:
    """Grep the container's runner logs for this session's blindfold turn
    lines — proof the turn actually ran the one-shot blindfold path (not the
    normal pane/app-server path) and, for max_messages>1, how many prior
    items it resumed with (resumed_with_items=N)."""
    try:
        proc = subprocess.run(
            [
                "docker",
                "exec",
                CONTAINER,
                "sh",
                "-c",
                f"grep -h 'blindfold .*session={session_id}' "
                "/root/.omnigent/logs/runner/runner-*.log 2>/dev/null",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return [line for line in proc.stdout.splitlines() if line.strip()]
    except Exception as exc:  # noqa: BLE001 — best-effort evidence gathering
        return [f"<could not fetch blindfold log evidence: {exc}>"]


async def run_case(
    context,
    client: httpx.AsyncClient,
    *,
    harness: str,
    case: str,
    labels: dict[str, str],
    messages: list[str],
) -> dict[str, Any]:
    session_id = await create_session(client, AGENTS[harness], labels)
    page = await context.new_page()
    await page.goto(f"{BASE_URL}/c/{session_id}")
    turns = []
    for i, msg in enumerate(messages):
        label = f"{harness}_{case}_{i + 1}"
        turn = await send_message_and_wait(page, client, session_id, msg, label=label)
        turns.append(turn)
    await page.close()
    blindfold_evidence = (
        blindfold_log_lines(session_id) if labels.get("omnigent.blindfold") else []
    )
    record = {
        "harness": harness,
        "case": case,
        "session_id": session_id,
        "labels": labels,
        "turns": turns,
        "blindfold_log_evidence": blindfold_evidence,
    }
    results.append(record)
    print(f"[{harness}/{case}] session={session_id} done", flush=True)
    for t in turns:
        print(f"    sent={t['sent']!r}", flush=True)
        print(f"    reply={t['reply']!r}", flush=True)
    for line in blindfold_evidence:
        print(f"    evidence: {line}", flush=True)
    return record


async def main() -> None:
    resolve_agents_and_host()
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=30.0) as client, async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context(viewport={"width": 1400, "height": 900})

        for harness in ("claude-native", "pi-native", "codex-native"):
            # Blind: max_messages=1 -> must not know the codeword.
            await run_case(
                context,
                client,
                harness=harness,
                case="blind",
                labels={"omnigent.blindfold": "true", "omnigent.context.max_messages": "1"},
                messages=["my codeword is PAPAYA-42", QUESTION],
            )
            # Injected (positive control): max_messages=3, 2 messages so the
            # codeword is still inside the window -> must quote it.
            await run_case(
                context,
                client,
                harness=harness,
                case="injected",
                labels={"omnigent.blindfold": "true", "omnigent.context.max_messages": "3"},
                messages=["my codeword is PAPAYA-42", QUESTION],
            )
            # Window edge: max_messages=3, 3 messages -> the codeword message
            # is now the 1st of 4 total messages, outside the last-3 window
            # by the time the 3rd is asked -> must NOT know it. This is the
            # real negative proof (injected alone only shows the positive
            # control); together they show the window boundary is honored,
            # not that history is either always-on or always-off.
            await run_case(
                context,
                client,
                harness=harness,
                case="window_edge",
                labels={"omnigent.blindfold": "true", "omnigent.context.max_messages": "3"},
                messages=[
                    "my codeword is PAPAYA-42",
                    "tell me a short fact about the ocean",
                    QUESTION,
                ],
            )
            # Memory fixture: max_messages=1 + memory_fixture -> must answer from memory alone.
            await run_case(
                context,
                client,
                harness=harness,
                case="memory_fixture",
                labels={
                    "omnigent.blindfold": "true",
                    "omnigent.context.max_messages": "1",
                    "omnigent.context.memory_fixture": "The user's codeword is MANGO-7",
                },
                messages=[QUESTION],
            )
            # No-leaks: BANANA-99 is planted in the workspace's parent folder
            # (/root/CLAUDE.md, /root/AGENTS.md: auto-discovered project docs)
            # and in each CLI's home memory; a blindfolded turn must never see it.
            await run_case(
                context,
                client,
                harness=harness,
                case="no_leaks",
                labels={"omnigent.blindfold": "true", "omnigent.context.max_messages": "1"},
                messages=[QUESTION],
            )
            # Baseline: no blindfold label at all, same codeword probe.
            await run_case(
                context,
                client,
                harness=harness,
                case="baseline",
                labels={},
                messages=["my codeword is PAPAYA-42", "what was the last message I sent?"],
            )

        await browser.close()

    # Post-hoc check for the memory-fixture case: the fixture text must
    # never be persisted in the session's own record (only the harness's
    # in-memory system prompt for that one turn should ever have seen it).
    async with httpx.AsyncClient(timeout=30.0) as client:
        for record in results:
            if record["case"] != "memory_fixture":
                continue
            items = await last_item_ids(client, record["session_id"], limit=100)
            # The model's own answer ("MANGO-7") is naturally recorded; what must
            # never be recorded is the memory item itself or the rendered block.
            leaked = [
                i["id"]
                for i in items
                if "The user's codeword is MANGO-7" in json.dumps(i)
                or "long_term_memory" in json.dumps(i)
            ]
            record["memory_fixture_leaked_into_record"] = bool(leaked)

    RESULTS_PATH.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nWrote {RESULTS_PATH}", flush=True)

    print("\n=== Results table ===", flush=True)
    for record in results:
        harness, case = record["harness"], record["case"]
        turns = record["turns"]
        last_reply = turns[-1]["reply"] if turns else ""
        verdict = "?"
        if case != "baseline" and "BANANA-99" in last_reply:
            verdict = "FAIL (vendor memory leaked)"
        elif case == "blind":
            verdict = "PASS" if "PAPAYA-42" not in last_reply else "FAIL"
        elif case == "injected":
            verdict = "PASS" if "PAPAYA-42" in last_reply else "FAIL"
        elif case == "window_edge":
            verdict = "PASS" if "PAPAYA-42" not in last_reply else "FAIL"
        elif case == "memory_fixture":
            verdict = "PASS" if "MANGO-7" in last_reply else "FAIL"
            if record.get("memory_fixture_leaked_into_record"):
                verdict += " (BUT LEAKED INTO RECORD)"
        elif case == "no_leaks":
            verdict = "PASS" if "BANANA-99" not in last_reply else "FAIL"
        elif case == "baseline":
            verdict = "PASS" if "PAPAYA-42" in last_reply else "FAIL"
        print(f"{harness:14s} {case:16s} {verdict:10s} reply={last_reply[:120]!r}")


if __name__ == "__main__":
    asyncio.run(main())
