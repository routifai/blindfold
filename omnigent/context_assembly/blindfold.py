"""Runner-side glue for blindfold-mode sessions.

One helper, used identically by claude-native, pi-native and codex-native:
call the server's ``POST /v1/sessions/{id}/context`` for this turn and fail
closed locally if anything goes wrong. Each harness's own guarded seam (see
``omnigent/runner/native/orchestration.py``) formats the result into its
native history-file format; this module owns only the network call and the
fail-closed default, so that logic lives in exactly one place.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import urllib.parse
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from omnigent.context_assembly.labels import BLINDFOLD_LABEL
from omnigent.context_assembly.models import AssembleResponse

_logger = logging.getLogger(__name__)

# Bridge-local file recording how a session's blindfold turns should reach
# the Omnigent server. Written once by the runner-side code that first
# prepares a native harness's bridge dir (it already holds a working server
# URL + auth headers there); a blindfold turn runs inside the harness's
# executor process, which may not share that code's environment but always
# shares its filesystem — bridge_dir is the established place for exactly
# this kind of local, co-located state (cf. each native harness's own bridge
# state file).
_CONNECTION_FILE = "omnigent-server-connection.json"

# Contract §2 example; v0.2 has no per-harness/model budget catalog yet, so
# every blindfolded turn asks for the same ceiling. Generous enough for the
# small proving-test conversations this mode is built for.
_DEFAULT_MAX_INPUT_TOKENS = 24_000

_REQUEST_TIMEOUT_SECONDS = 10.0

# How many of the session's most recent items to fetch locally before asking
# the assembler which of them to keep. The assembler's own default window
# (DEFAULT_MAX_MESSAGES=20 messages, each with at most a handful of attached
# tool items) fits comfortably inside this; a test that asks for a bigger
# omnigent.context.max_messages than this can support will see it clamped.
_RECENT_ITEMS_FETCH_LIMIT = 300


def is_blindfolded(labels: dict[str, str] | None) -> bool:
    """Whether *labels* mark a session for blindfold-mode context assembly.

    :param labels: A session's labels, or ``None``.
    :returns: ``True`` only when ``omnigent.blindfold`` is exactly ``"true"``.
    """
    return bool(labels) and labels.get(BLINDFOLD_LABEL) == "true"


def write_server_connection(
    bridge_dir: Path,
    *,
    base_url: str,
    headers: dict[str, str],
    labels: dict[str, str] | None = None,
) -> None:
    """Persist how this session's blindfold turns should reach the server.

    Best-effort and overwritten on every prepare, so a rotated/refreshed
    token is picked up by the next turn. Static (not refresh-capable) — fine
    for the single-user local test deployment this feature ships against
    first; a multi-user deployment with short-lived bearer tokens would need
    this refreshed more often than "once per bridge prepare".

    :param bridge_dir: This session's native-harness bridge directory.
    :param base_url: Omnigent server base URL, e.g.
        ``"http://host.docker.internal:8780"``.
    :param headers: Static auth headers to replay on the assemble/observe
        calls.
    :param labels: This session's labels, when cheaply available at the call
        site (e.g. from an already-fetched session snapshot). Persisting
        whether the session is blindfolded here lets every turn's
        :func:`read_server_connection` skip a network round trip for the
        (overwhelmingly common) non-blindfolded case — a turn otherwise pays
        one ``GET /v1/sessions/{id}`` just to learn "no". ``None`` when
        unavailable at the call site: the reader then reports "unknown" and
        the caller falls back to checking over the network once.
    """
    path = bridge_dir / _CONNECTION_FILE
    if labels is None:
        # A caller without the session's labels must never overwrite a state
        # already recorded by one that had them (the runner writes first).
        if path.exists():
            return
    elif not is_blindfolded(labels):
        # Not blindfolded: leave nothing behind (no auth headers on disk), so
        # every turn short-circuits on a missing file with zero network calls.
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        return
    payload: dict[str, Any] = {"base_url": base_url, "headers": dict(headers)}
    if labels is not None:
        payload["blindfolded"] = True
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except OSError:
        _logger.warning("Could not persist blindfold server connection at %s", path, exc_info=True)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


@dataclass
class ServerConnection:
    """How to reach the server, and whether this session is blindfolded.

    :param base_url: Omnigent server base URL.
    :param headers: Static auth headers.
    :param blindfolded: ``True``/``False`` when known from the local file
        (set at prepare time — see :func:`write_server_connection`), or
        ``None`` when unknown — the caller must check over the network.
    """

    base_url: str
    headers: dict[str, str]
    blindfolded: bool | None


def read_server_connection(bridge_dir: Path) -> ServerConnection | None:
    """Read back what :func:`write_server_connection` persisted.

    :param bridge_dir: This session's native-harness bridge directory.
    :returns: The connection, or ``None`` when it was never written (not a
        native-launch session) or is unreadable/malformed.
    """
    path = bridge_dir / _CONNECTION_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    base_url = raw.get("base_url")
    headers = raw.get("headers")
    if not isinstance(base_url, str) or not base_url or not isinstance(headers, dict):
        return None
    blindfolded = raw.get("blindfolded")
    return ServerConnection(
        base_url=base_url,
        headers={str(k): str(v) for k, v in headers.items()},
        blindfolded=blindfolded if isinstance(blindfolded, bool) else None,
    )


@dataclass
class BlindfoldTurnContext:
    """What a blindfolded turn's CLI should see.

    :param system_prompt: The rendered system text (agent instructions +
        the ``<long_term_memory>`` block, when present) for the harness's
        system-prompt channel (e.g. ``--append-system-prompt``).
    :param prior_history_items: Flat record item dicts (the same shape
        ``GET /sessions/{id}/items`` returns) to feed a harness's own
        transcript/session-file rebuilder, in order — **excluding** the new
        message itself. The new message is still delivered through the
        harness's normal live-input channel (typing into the pane / the
        turn's prompt), exactly as an unblindfolded turn would; only what
        came *before* it is injected as synthesized history. Baking the new
        message into both the resume file and the live input would show it
        to the model twice.
    :param fallback: ``True`` when this is the fail-closed shape (contract
        §7) — either the assembler itself failed closed, or this call
        couldn't reach/parse the endpoint at all.
    """

    system_prompt: str
    prior_history_items: list[dict[str, Any]] = field(default_factory=list)
    fallback: bool = False


async def record_user_message(
    server_client: httpx.AsyncClient,
    *,
    session_id: str,
    text: str,
) -> None:
    """Persist the new user message as a record item before assembling context.

    Native-terminal harnesses normally record a turn's input as a side
    effect of the forwarder mirroring it back out of the CLI's own
    transcript (see e.g. ``claude_native/forwarder.py``'s
    ``external_conversation_item`` posts) — there is no separate
    "persist-then-forward" step upstream of the executor for these harnesses.
    A blindfold turn never starts that forwarder (no live pane for it to
    tail), so nothing else will ever record this message. Without this call,
    the assembler's next call would anchor on the *previous* turn's last
    item instead of this one, and this turn's own message would never enter
    later history.

    Uses the same ``external_conversation_item`` event the forwarders use,
    so this is indistinguishable from a normal mirrored item and does not
    trigger a new turn dispatch (unlike posting a plain ``"message"`` event,
    which is the client-submission path).

    :param server_client: The runner's own Omnigent server client.
    :param session_id: Omnigent conversation id.
    :param text: The user's message text.
    :raises httpx.HTTPError: If the server rejects the append. Callers
        should treat this as fatal to the turn (fail closed with the new
        message's own text only) rather than proceeding to assemble against
        a record that doesn't yet contain it.
    """
    quoted_id = urllib.parse.quote(session_id, safe="")
    resp = await server_client.post(
        f"/v1/sessions/{quoted_id}/events",
        json={
            "type": "external_conversation_item",
            "data": {
                "item_type": "message",
                "item_data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
            },
        },
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()


async def fetch_blindfold_turn_context(
    server_client: httpx.AsyncClient,
    *,
    session_id: str,
    labels: dict[str, str],
    harness_name: str,
    model: str,
    context_window_tokens: int,
    history_format: str,
    fallback_instructions: str | None,
) -> BlindfoldTurnContext:
    """Assemble one blindfolded turn's context via the server.

    Never raises: any failure (network, non-2xx, malformed body) fails
    closed to *fallback_instructions* and no history — contract §7's
    "Timeout or error -> base prompt + the new message only", applied here
    as an extra outer layer in case the server call itself never lands
    (the server's own ``/context`` endpoint already fails closed for an
    in-process assembler error; this covers the call failing to reach it).

    :param server_client: The runner's own Omnigent server client.
    :param session_id: Omnigent conversation id.
    :param labels: This session's labels — carries any
        ``omnigent.context.*`` test-hook keys through to the assembler.
    :param harness_name: e.g. ``"claude-native"``.
    :param model: The model this turn runs under.
    :param context_window_tokens: The model's context window (informational).
    :param history_format: e.g. ``"claude_jsonl"``, ``"pi_session_v3"``,
        ``"codex_rollout"`` (informational, contract §2 `capabilities`).
    :param fallback_instructions: The harness's own already-resolved agent
        instructions, used as the fail-closed system prompt so the failure
        path needs no second network call.
    :returns: The turn's context, or the fail-closed shape.
    """
    try:
        quoted_id = urllib.parse.quote(session_id, safe="")
        # Fetch the recent window ourselves (desc, newest first) — one round
        # trip covers both finding the new message (items[0]) and resolving
        # the assembler's refs back to full item dicts below, with no need
        # for a second items call.
        recent_resp = await server_client.get(
            f"/v1/sessions/{quoted_id}/items",
            params={"limit": _RECENT_ITEMS_FETCH_LIMIT, "order": "desc"},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        recent_resp.raise_for_status()
        recent_page = recent_resp.json()
        recent_items_desc = recent_page.get("data") if isinstance(recent_page, dict) else None
        if not isinstance(recent_items_desc, list) or not recent_items_desc:
            raise RuntimeError(f"session {session_id!r} has no items yet")
        items_by_id = {
            item["id"]: item
            for item in recent_items_desc
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        new_item_id = recent_items_desc[0].get("id")
        if not isinstance(new_item_id, str) or not new_item_id:
            raise RuntimeError(f"session {session_id!r}'s last item has no id")

        request_body = {
            "contract_version": "0.2",
            "turn_id": f"turn_{uuid.uuid4().hex[:12]}",
            "session": {"id": session_id, "owner": "local", "labels": labels},
            "harness": {
                "name": harness_name,
                "model": model,
                "context_window_tokens": context_window_tokens,
                "capabilities": {"history_format": history_format, "images": True},
            },
            "new_item_id": new_item_id,
            "record": {"item_count": 0, "last_item_id": new_item_id},
            "budget": {"max_input_tokens": _DEFAULT_MAX_INPUT_TOKENS},
        }
        resp = await server_client.post(
            f"/v1/sessions/{quoted_id}/context",
            json=request_body,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        response = AssembleResponse.model_validate(resp.json())
    except Exception:  # noqa: BLE001 — any failure must fail closed, never break the turn
        _logger.warning(
            "blindfold context fetch failed for session=%s harness=%s; "
            "failing closed to new message only",
            session_id,
            harness_name,
            exc_info=True,
        )
        return BlindfoldTurnContext(
            system_prompt=fallback_instructions or "",
            prior_history_items=[],
            fallback=True,
        )

    # Local import: avoids a module-load cycle (render_system_text lives in
    # the same package's assembler module, which this module doesn't
    # otherwise need at import time).
    from omnigent.context_assembly.assembler import render_system_text

    refs = [item.ref for item in response.history.items]
    # The last ref is always the new message (contract §3) — drop it here;
    # see the field docstring on BlindfoldTurnContext for why.
    prior_refs = refs[:-1] if refs and refs[-1] == new_item_id else refs
    # Resolve refs back to full item dicts using the window already fetched
    # above. A ref the window doesn't cover (the assembler's own record read
    # saw further back than our _RECENT_ITEMS_FETCH_LIMIT window) is dropped
    # rather than fetched again — contract §7's "Invalid ref -> drop; flag"
    # extends naturally to "unreachable from here".
    prior_items = [items_by_id[ref] for ref in prior_refs if ref in items_by_id]

    return BlindfoldTurnContext(
        system_prompt=render_system_text(response),
        prior_history_items=prior_items,
        fallback=response.audit.fallback,
    )
