"""Runner-side glue for blindfold-mode sessions.

One helper, used identically by claude-native, pi-native and codex-native:
call the server's ``POST /v1/sessions/{id}/context`` for this turn and fail
closed locally if anything goes wrong. Each harness's own guarded seam (see
``omnigent/runner/native/orchestration.py``) formats the result into its
native history-file format; this module owns only the network call and the
fail-closed default, so that logic lives in exactly one place.
"""

from __future__ import annotations

import logging
import urllib.parse
import uuid
from dataclasses import dataclass

import httpx

from omnigent.context_assembly.labels import BLINDFOLD_LABEL
from omnigent.context_assembly.models import AssembleResponse

_logger = logging.getLogger(__name__)

# Contract §2 example; v0.2 has no per-harness/model budget catalog yet, so
# every blindfolded turn asks for the same ceiling. Generous enough for the
# small proving-test conversations this mode is built for.
_DEFAULT_MAX_INPUT_TOKENS = 24_000

_REQUEST_TIMEOUT_SECONDS = 10.0


def is_blindfolded(labels: dict[str, str] | None) -> bool:
    """Whether *labels* mark a session for blindfold-mode context assembly.

    :param labels: A session's labels, or ``None``.
    :returns: ``True`` only when ``omnigent.blindfold`` is exactly ``"true"``.
    """
    return bool(labels) and labels.get(BLINDFOLD_LABEL) == "true"


@dataclass
class BlindfoldTurnContext:
    """What a blindfolded turn's CLI should see.

    :param system_prompt: The rendered system text (agent instructions +
        the ``<long_term_memory>`` block, when present) for the harness's
        system-prompt channel (e.g. ``--append-system-prompt``).
    :param prior_history_item_ids: Record item ids to replay in the CLI's
        native history format, in order — **excluding** the new message
        itself. The new message is still delivered through the harness's
        normal live-input channel (typing into the pane / the turn's
        prompt), exactly as an unblindfolded turn would; only what came
        *before* it is injected as synthesized history. Baking the new
        message into both the resume file and the live input would show it
        to the model twice.
    :param fallback: ``True`` when this is the fail-closed shape (contract
        §7) — either the assembler itself failed closed, or this call
        couldn't reach/parse the endpoint at all.
    """

    system_prompt: str
    prior_history_item_ids: list[str]
    fallback: bool


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
        last_item_resp = await server_client.get(
            f"/v1/sessions/{quoted_id}/items",
            params={"limit": 1, "order": "desc"},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        last_item_resp.raise_for_status()
        last_page = last_item_resp.json()
        last_items = last_page.get("data") if isinstance(last_page, dict) else None
        if not isinstance(last_items, list) or not last_items:
            raise RuntimeError(f"session {session_id!r} has no items yet")
        new_item_id = last_items[0].get("id")
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
    except Exception:
        _logger.warning(
            "blindfold context fetch failed for session=%s harness=%s; "
            "failing closed to new message only",
            session_id,
            harness_name,
            exc_info=True,
        )
        return BlindfoldTurnContext(
            system_prompt=fallback_instructions or "",
            prior_history_item_ids=[],
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

    return BlindfoldTurnContext(
        system_prompt=render_system_text(response),
        prior_history_item_ids=prior_refs,
        fallback=response.audit.fallback,
    )
