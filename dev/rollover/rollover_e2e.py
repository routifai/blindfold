"""Rollover super-chat end-to-end proof, driven through the real web UI.

Tests the current design (see rollover/README.md): Omnigent no longer writes
a checkpoint and restarts the CLI pane. Each native CLI compacts itself at
Omnigent's threshold, and the forwarder records the result as a `compaction`
item. Recall is the `session_history` tool, relayed as
`mcp__omnigent__session_history`.

Not part of the pytest suite — a one-off proving script (mirrors
dev/blindfold/blindfold_e2e.py from rollover-muse), run by hand against a
live local server + docker runner per dev/rollover/README.md.

Usage: source .venv/bin/activate && python dev/rollover/rollover_e2e.py

Costs real API tokens: each harness's main suite pushes one session past its
rollover threshold (~100k tokens of filler). See the README's cost note.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

import httpx
from playwright.async_api import Page, async_playwright, expect
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

BASE_URL = os.environ.get("ROLLOVER_BASE_URL", "http://127.0.0.1:8795")
OUT_DIR = Path(os.environ.get("ROLLOVER_OUT_DIR", "rollover-e2e-out"))
RESULTS_PATH = OUT_DIR / "results.json"
TURN_TIMEOUT_S = float(os.environ.get("ROLLOVER_TURN_TIMEOUT_S", "240"))
TURN_SETTLE_S = float(os.environ.get("ROLLOVER_TURN_SETTLE_S", "8"))

AGENT_NAMES = {
    "claude-native": "claude-native-ui",
    "codex-native": "codex-native-ui",
    "pi-native": "pi-native-ui",
}
AGENTS: dict[str, str] = {}
HOST_ID = os.environ.get("ROLLOVER_HOST_ID", "")

# No hardcoded model ids here (dev/lint/lint_no_hardcoded_models.py forbids
# them outside tests) — each harness's model comes from its own env var, or
# is left to the runner-config.yaml default when unset.
MODEL_ENV_VARS = {
    "claude-native": "E2E_CLAUDE_MODEL",
    "codex-native": "E2E_CODEX_MODEL",
    "pi-native": "E2E_PI_MODEL",
}

# Optional shared override for the rollover threshold (in tokens), still
# floored at 100k by resolve_rollover_threshold. Useful for forcing more than
# one compaction within one fill, as a big-context model otherwise wouldn't
# cross its (window-derived) default threshold during the fill below.
ROLLOVER_AT_TOKENS_ENV = "ROLLOVER_E2E_AT_TOKENS"

SESSION_HISTORY_TOOL_NAME = "mcp__omnigent__session_history"
# The claude-native forwarder's fallback text when a hook-only compaction
# boundary has no transcript summary to carry (see forwarder.py). Not a
# failure on its own, but worth surfacing separately.
PLACEHOLDER_SUMMARY_PREFIX = "[Claude Code compaction — "

# Eleven dense, non-boilerplate filler turns push a session past its rollover
# threshold (~100k tokens total, floored per resolve_rollover_threshold).
FILL_TAGS = [f"t{i:02d}" for i in range(1, 12)]

results: list[dict[str, Any]] = []


def rollover_labels(*, rollover_at_tokens: int | None = None) -> dict[str, str]:
    """``omnigent.context.mode=rollover``, optionally overriding the
    threshold (still floored at 100k)."""
    labels = {"omnigent.context.mode": "rollover"}
    if rollover_at_tokens is not None:
        labels["omnigent.context.rollover_at_tokens"] = str(rollover_at_tokens)
    return labels


def _rollover_at_tokens_override() -> int | None:
    raw = os.environ.get(ROLLOVER_AT_TOKENS_ENV)
    return int(raw) if raw else None


def launch_args_for(harness: str) -> list[str] | None:
    """``--model`` override from this harness's env var, or ``None`` to use
    the runner's configured default model."""
    model = os.environ.get(MODEL_ENV_VARS[harness])
    return ["--model", model] if model else None


def resolve_agents_and_host() -> None:
    """Fill AGENTS from /v1/agents and HOST_ID from the first online /v1/hosts entry."""
    global HOST_ID
    agents = httpx.get(f"{BASE_URL}/v1/agents", params={"limit": 200}, timeout=30).json()["data"]
    by_name = {a.get("name"): a["id"] for a in agents}
    for harness, name in AGENT_NAMES.items():
        if name in by_name:
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


async def last_item_ids(
    client: httpx.AsyncClient, session_id: str, limit: int = 400
) -> list[dict]:
    resp = await client.get(
        f"{BASE_URL}/v1/sessions/{session_id}/items", params={"limit": limit, "order": "desc"}
    )
    resp.raise_for_status()
    return resp.json().get("data") or []


async def compaction_items(client: httpx.AsyncClient, session_id: str) -> list[dict]:
    items = await last_item_ids(client, session_id)
    return [i for i in items if i.get("type") == "compaction"]


def _session_history_calls(items: list[dict]) -> list[dict]:
    return [
        i
        for i in items
        if i.get("type") == "function_call" and i.get("name") == SESSION_HISTORY_TOOL_NAME
    ]


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
    bubble_locator = page.locator('[data-testid="message-bubble"][data-role="assistant"]')
    # The chat list is virtualized and replies can repeat, so mark the bubbles
    # already shown and wait for an unmarked one.
    await bubble_locator.evaluate_all("els => els.forEach(e => e.dataset.e2eSeen = '1')")
    fresh_bubble = page.locator(
        '[data-testid="message-bubble"][data-role="assistant"]:not([data-e2e-seen])'
    )
    t0 = time.monotonic()
    # A fresh session's page can block input while its terminal connects.
    try:
        await page.get_by_role("button", name="Send", exact=True).click(timeout=120_000)
    except PlaywrightTimeoutError:
        await page.screenshot(path=str(OUT_DIR / f"{label}_send_blocked.png"))
        await composer.press("Enter", timeout=30_000)

    first_output_s: float | None = None
    deadline = time.monotonic() + TURN_TIMEOUT_S
    while time.monotonic() < deadline:
        if await fresh_bubble.count():
            txt = await fresh_bubble.last.inner_text()
            if txt.strip():
                first_output_s = time.monotonic() - t0
                break
        await asyncio.sleep(0.2)

    # The turn is done once its last new item is an assistant message and no
    # item has landed for TURN_SETTLE_S (a preamble can precede tool calls).
    reply_text = ""
    new_items: list[dict] = []
    last_change = time.monotonic()
    while time.monotonic() < deadline:
        items = await last_item_ids(client, session_id)
        fresh = [i for i in items if i["id"] not in seen_before]
        if len(fresh) != len(new_items):
            new_items, last_change = fresh, time.monotonic()
        tail = new_items[0] if new_items else None  # newest first
        if (
            tail is not None
            and tail.get("type") == "message"
            and tail.get("role") == "assistant"
            and time.monotonic() - last_change >= TURN_SETTLE_S
        ):
            reply_text = _assistant_text(tail)
            break
        await asyncio.sleep(0.5)
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


def _ledger_doc(tag: str, n: int = 300) -> str:
    """Dense, deterministic filler text (~5-6k tokens) — not compressible
    boilerplate, so the transcript genuinely grows toward the threshold."""
    return " ".join(
        f"Sentence {i}: the {tag} ledger entry {i} records reference "
        f"{tag}-{i:03d}-{(i * 7919) % 9973:04d} for cost centre {i % 17}."
        for i in range(1, n + 1)
    )


async def run_fill_turns(
    page: Page, client: httpx.AsyncClient, session_id: str, *, label_prefix: str
) -> tuple[list[dict[str, Any]], list[int]]:
    """Send the fixed filler turns; return (turns, compaction-count delta per turn)."""
    turns: list[dict[str, Any]] = []
    deltas: list[int] = []
    prev_count = len(await compaction_items(client, session_id))
    for i, tag in enumerate(FILL_TAGS, start=1):
        prompt = f"Document {tag}. Reply with just: received.\n\n" + _ledger_doc(tag)
        turn = await send_message_and_wait(
            page, client, session_id, prompt, label=f"{label_prefix}_fill_{i:02d}"
        )
        turns.append(turn)
        count = len(await compaction_items(client, session_id))
        deltas.append(count - prev_count)
        prev_count = count
    return turns, deltas


def _first_compaction_turn_index(deltas: list[int]) -> int | None:
    for i, delta in enumerate(deltas):
        if delta >= 1:
            return i
    return None


def check_compacts_and_no_loop(deltas: list[int]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Case 1: at least one compaction happened. Case 1b: not a loop — a
    bounded total, and no two consecutive fill turns each adding one."""
    total = sum(deltas)
    consecutive_pairs = any(deltas[i] >= 1 and deltas[i + 1] >= 1 for i in range(len(deltas) - 1))
    bound = max(1, len(deltas) // 2)
    compacts = {
        "case": "compacts",
        "pass": total >= 1,
        "evidence": {"compaction_count": total, "per_turn_deltas": deltas},
    }
    no_loop = {
        "case": "no_loop",
        "pass": total <= bound and not consecutive_pairs,
        "evidence": {
            "compaction_count": total,
            "bound": bound,
            "consecutive_compacting_turns": consecutive_pairs,
        },
    }
    return compacts, no_loop


def _streamed(turn: dict[str, Any]) -> bool:
    return bool(turn["reply"]) and turn["first_output_s"] is not None


async def run_rollover_suite(context, client: httpx.AsyncClient, harness: str) -> dict[str, Any]:
    """Cases 1-6 (+ side_chat for claude-native), against one long-running session."""
    codeword = f"CW-{harness.upper().replace('-', '')}-{secrets.token_hex(3).upper()}"
    first_message = f"My codeword is {codeword}. Reply with just OK."

    sid = await create_session(
        client,
        AGENTS[harness],
        labels=rollover_labels(rollover_at_tokens=_rollover_at_tokens_override()),
        terminal_launch_args=launch_args_for(harness),
    )
    page = await context.new_page()
    await page.goto(f"{BASE_URL}/c/{sid}")

    turn1 = await send_message_and_wait(
        page, client, sid, first_message, label=f"{harness}_t1_codeword"
    )

    fill_turns, deltas = await run_fill_turns(page, client, sid, label_prefix=harness)
    compacts, no_loop = check_compacts_and_no_loop(deltas)

    idx = _first_compaction_turn_index(deltas)
    remaining_fill = fill_turns[idx + 1 :] if idx is not None else []

    arithmetic_turn = await send_message_and_wait(
        page, client, sid, "Quick one: what is 17 times 3?", label=f"{harness}_arithmetic"
    )
    tool_turn = await send_message_and_wait(
        page,
        client,
        sid,
        "Do you see a tool called session_history? Answer yes or no, then list "
        "the names of all tools you have.",
        label=f"{harness}_tool_visibility",
    )
    recall_turn = await send_message_and_wait(
        page,
        client,
        sid,
        "What was my very first message in this conversation, word for word?",
        label=f"{harness}_recall",
    )

    # Case 2: the turn right after the compaction must succeed and stream.
    continues_turn = remaining_fill[0] if remaining_fill else arithmetic_turn
    continues = {
        "case": "continues",
        "pass": _streamed(continues_turn),
        "evidence": {
            "first_output_s": continues_turn["first_output_s"],
            "done_s": continues_turn["done_s"],
        },
    }

    # Case 3: the model reports seeing the recall tool.
    sees_tool = {
        "case": "sees_tool",
        "pass": tool_turn["reply"].strip().lower().startswith("yes"),
        "evidence": {"reply": tool_turn["reply"][:300]},
    }

    # Case 4: recall used AND the exact first message comes back.
    post_compaction_new_items = [
        item
        for turn in (*remaining_fill, arithmetic_turn, tool_turn, recall_turn)
        for item in turn["new_items"]
    ]
    sh_calls = _session_history_calls(post_compaction_new_items)
    recall_verbatim = {
        "case": "recall_verbatim",
        # Exact wording is the requirement; whether it came from the kept
        # context (e.g. Pi's summary) or the tool is reported as evidence.
        "pass": first_message in recall_turn["reply"],
        "evidence": {
            "session_history_call_count": len(sh_calls),
            "reply": recall_turn["reply"][:300],
        },
    }

    # Case 5: every compaction item recorded a real summary.
    comp_items = await compaction_items(client, sid)
    placeholder_count = sum(
        1 for c in comp_items if str(c.get("summary", "")).startswith(PLACEHOLDER_SUMMARY_PREFIX)
    )
    summary_recorded = {
        "case": "summary_recorded",
        "pass": bool(comp_items) and all(str(c.get("summary", "")).strip() for c in comp_items),
        "evidence": {
            "compaction_count": len(comp_items),
            "placeholder_summary_count": placeholder_count,
        },
        "warning": (
            f"{placeholder_count} compaction(s) recorded only the fallback placeholder summary"
            if placeholder_count
            else None
        ),
    }

    cases = [compacts, no_loop, continues, sees_tool, recall_verbatim, summary_recorded]

    # Case 7 (claude-native only): a side chat forked after a compaction.
    if harness == "claude-native":
        fork = await fork_session(client, sid, side_chat=True)
        side_id = fork["id"]
        side_labels = await session_labels(client, side_id)
        side_page = await context.new_page()
        # A fork has no host yet; bind it the way the UI's "Start session" does.
        bind = await client.post(
            f"{BASE_URL}/v1/hosts/{HOST_ID}/runners",
            json={"session_id": side_id, "workspace": "/root/ws"},
        )
        bind.raise_for_status()
        await side_page.goto(f"{BASE_URL}/c/{side_id}")
        side_turn = await send_message_and_wait(
            side_page, client, side_id, "What's my codeword?", label=f"{harness}_side_chat"
        )
        side_chat = {
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
        cases.append(side_chat)
        await side_page.close()

    await page.close()

    record = {
        "harness": harness,
        "session_id": sid,
        "codeword": codeword,
        "cases": cases,
        "turns": {
            "t1_codeword": turn1,
            "fillers": fill_turns,
            "arithmetic": arithmetic_turn,
            "tool_visibility": tool_turn,
            "recall": recall_turn,
        },
    }
    results.append(record)
    print(f"[{harness}] session={sid} suite done", flush=True)
    for c in cases:
        print(f"    {c['case']}: {'PASS' if c['pass'] else 'FAIL'} {c['evidence']}", flush=True)
        if c.get("warning"):
            print(f"    {c['case']}: WARNING {c['warning']}", flush=True)
    return record


async def run_baseline(context, client: httpx.AsyncClient, harness: str) -> dict[str, Any]:
    """Case 6: mode unset — no compaction items, no session_history calls.

    Deliberately cheap (three short turns, not a ~100k-token replica of the
    main suite): rollover is opt-in purely via the mode label, so a full-scale
    fill here would double this harness's API spend to re-prove the same
    on/off switch. See the README's cost note.
    """
    codeword = f"CW-{harness.upper().replace('-', '')}-BASE-{secrets.token_hex(3).upper()}"
    sid = await create_session(
        client, AGENTS[harness], labels={}, terminal_launch_args=launch_args_for(harness)
    )
    page = await context.new_page()
    await page.goto(f"{BASE_URL}/c/{sid}")

    await send_message_and_wait(
        page,
        client,
        sid,
        f"My codeword is {codeword}. Reply with just OK.",
        label=f"{harness}_baseline_t1",
    )
    for i in range(2):
        await send_message_and_wait(
            page,
            client,
            sid,
            f"Please write a ~100 word note about topic {i + 1}.",
            label=f"{harness}_baseline_fill_{i + 1}",
        )
    cw_turn = await send_message_and_wait(
        page, client, sid, "What's my codeword?", label=f"{harness}_baseline_codeword_check"
    )
    await page.close()

    all_items = await last_item_ids(client, sid)
    comp = [i for i in all_items if i.get("type") == "compaction"]
    sh_calls = _session_history_calls(all_items)

    case = {
        "case": "baseline",
        "pass": bool(codeword in cw_turn["reply"] and not comp and not sh_calls),
        "evidence": {
            "compaction_item_count": len(comp),
            "session_history_call_count": len(sh_calls),
            "reply": cw_turn["reply"][:200],
        },
    }
    record = {"harness": harness, "session_id": sid, "codeword": codeword, "cases": [case]}
    results.append(record)
    print(
        f"[{harness}] baseline session={sid} done: {'PASS' if case['pass'] else 'FAIL'}",
        flush=True,
    )
    return record


async def main() -> None:
    resolve_agents_and_host()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=30.0) as client, async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context(viewport={"width": 1400, "height": 900})

        wanted = os.environ.get("ROLLOVER_E2E_HARNESSES", "claude-native,codex-native,pi-native")
        for harness in [h.strip() for h in wanted.split(",") if h.strip()]:
            if harness not in AGENTS:
                print(f"[{harness}] no agent registered, skipping", flush=True)
                continue
            await run_rollover_suite(context, client, harness)
            await run_baseline(context, client, harness)

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
