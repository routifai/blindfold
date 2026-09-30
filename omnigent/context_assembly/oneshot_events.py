"""Parsers for a blindfolded one-shot CLI's structured event stream.

Each blindfold-mode turn runs one disposable CLI process (see e.g.
``omnigent.harnesses.claude_native.blindfold``). Before this module existed,
that process's own stdout was consumed only for its final text, so the tool
calls (bash, file reads, edits) it made along the way never reached the
Omnigent session record — the web UI showed only the answer.

Each CLI has its own structured, line-delimited event format for a one-shot
run (Claude Code's ``--output-format stream-json``, Pi's ``--mode json``,
Codex's ``exec --json``). The three ``parse_*`` functions here turn that
format into the same flat :class:`OneShotItem` shape, whose ``item_type`` /
``item_data`` mirror exactly what the resident native forwarders record for
a live session (``function_call``, ``function_call_output``, ``message``,
``reasoning`` — see e.g. ``claude_native/bridge.py``'s transcript parser or
the pi-native extension's ``postToolCall``/``postToolResult``). A harness's
``blindfold.py`` posts the result via
:func:`omnigent.context_assembly.blindfold.post_oneshot_items`.

Deliberately NOT reused: the JSONL-transcript / RPC-notification parsers
each harness already has for its *resident*, long-lived session (e.g.
``claude_native/bridge.py``'s ``_assistant_transcript_items_from_entry``,
``codex_native/forwarder.py``'s ``_codex_tool_call_from_item``). Those carry
years of edge cases specific to a persistent session replayed from disk
(compaction summaries, slash-command markup, streaming reconciliation) that
a single fresh one-shot process never hits — pattern-matching their
input/output shape here would add coupling without buying anything.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

_JsonObject = dict[str, Any]


@dataclass(frozen=True)
class OneShotItem:
    """One conversation item parsed from a one-shot CLI's event stream.

    :param item_type: Omnigent conversation item type, e.g.
        ``"function_call"`` — see ``omnigent.entities.conversation`` for the
        payload schema each type expects.
    :param item_data: Item payload, shaped like the other native forwarders'
        ``external_conversation_item`` payloads.
    """

    item_type: str
    item_data: _JsonObject


def _iter_json_lines(raw: str) -> list[_JsonObject]:
    """Decode each non-blank line of *raw* as JSON, dropping ones that aren't.

    A CLI's own diagnostic noise or a truncated final line (process killed
    mid-write) must not abort parsing everything before it.
    """
    events: list[_JsonObject] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _text_from_blocks(content: Any) -> str:
    """Concatenate ``text``-bearing blocks from a Claude/Pi style content list.

    :param content: A content string, a list of ``{"type": ..., "text": ...}``
        style blocks, or anything else (returns ``""``).
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        # Thinking/reasoning blocks are surfaced as separate `reasoning`
        # items by the callers below; skip them here so they aren't also
        # folded into the assistant message text.
        if block.get("type") in ("thinking", "reasoning"):
            continue
        text = block.get("text") or block.get("input_text") or block.get("output_text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _message_item(*, agent: str, text: str) -> OneShotItem:
    return OneShotItem(
        "message",
        {"role": "assistant", "agent": agent, "content": [{"type": "output_text", "text": text}]},
    )


def _reasoning_item(*, agent: str, text: str) -> OneShotItem:
    return OneShotItem(
        "reasoning",
        {"agent": agent, "summary": [], "content": [{"type": "reasoning_text", "text": text}]},
    )


def _function_call_item(
    *, agent: str, name: str, call_id: str, arguments: _JsonObject
) -> OneShotItem:
    return OneShotItem(
        "function_call",
        {
            "agent": agent,
            "name": name,
            "arguments": json.dumps(arguments, separators=(",", ":")),
            "call_id": call_id,
        },
    )


def _function_call_output_item(*, call_id: str, output: str) -> OneShotItem:
    return OneShotItem("function_call_output", {"call_id": call_id, "output": output})


# ── Claude Code: `-p --output-format stream-json --verbose` ───────────────
#
# One JSON object per line. ``{"type": "assistant", "message": {...}}`` and
# ``{"type": "user", "message": {...}}`` carry the same ``message.content``
# block shapes (text/thinking/tool_use/tool_result) Claude's own JSONL
# transcript uses. ``{"type": "result", "result": "..."}`` is the final
# answer — the same text `--output-format text` would have printed alone.


def _claude_assistant_blocks(content: Any, agent: str) -> list[OneShotItem]:
    items: list[OneShotItem] = []
    if not isinstance(content, list):
        return items
    text_parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                text_parts.append(text)
        elif block_type == "thinking":
            thinking = block.get("thinking")
            if isinstance(thinking, str) and thinking.strip():
                items.append(_reasoning_item(agent=agent, text=thinking))
        elif block_type == "tool_use":
            call_id = block.get("id")
            name = block.get("name")
            if isinstance(call_id, str) and call_id and isinstance(name, str) and name:
                arguments = block.get("input")
                items.append(
                    _function_call_item(
                        agent=agent,
                        name=name,
                        call_id=call_id,
                        arguments=arguments if isinstance(arguments, dict) else {},
                    )
                )
    if text_parts:
        items.append(_message_item(agent=agent, text="".join(text_parts)))
    return items


def _claude_tool_result_blocks(content: Any) -> list[OneShotItem]:
    items: list[OneShotItem] = []
    if not isinstance(content, list):
        return items
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        call_id = block.get("tool_use_id")
        if not isinstance(call_id, str) or not call_id:
            continue
        block_content = block.get("content")
        output = _text_from_blocks(block_content) or _json_or_str(block_content)
        items.append(_function_call_output_item(call_id=call_id, output=output))
    return items


def _json_or_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value)
    except TypeError:
        return str(value)


def parse_claude_stream_json(
    raw: str, *, agent: str = "Claude"
) -> tuple[list[OneShotItem], str | None]:
    """Parse Claude Code's ``--output-format stream-json --verbose`` stdout.

    :param raw: The one-shot process's full stdout.
    :param agent: Agent name attached to assistant/tool items.
    :returns: ``(items, final_text)`` — parsed items in stream order, and the
        turn's final answer (the ``result`` event's text), or ``None`` if no
        ``result`` event was seen (e.g. the process was killed mid-stream).
    """
    items: list[OneShotItem] = []
    final_text: str | None = None
    for event in _iter_json_lines(raw):
        event_type = event.get("type")
        message = event.get("message")
        if event_type == "assistant" and isinstance(message, dict):
            items.extend(_claude_assistant_blocks(message.get("content"), agent))
        elif event_type == "user" and isinstance(message, dict):
            items.extend(_claude_tool_result_blocks(message.get("content")))
        elif event_type == "result":
            result_text = event.get("result")
            if isinstance(result_text, str):
                final_text = result_text
    return items, final_text


# ── Pi: `--print --mode json` ──────────────────────────────────────────────
#
# A stream of ``message_start``/``message_update``/``message_end`` (plus
# ``tool_execution_*``/``turn_*``/``agent_*`` bookkeeping). Only
# ``message_end`` carries the complete, authoritative message — the same
# event the omnigent pi-native extension keys its own mirroring off of
# (see ``mirrorAssistantMessage``/``postToolResult`` in
# ``omnigent_pi_native_extension.js``) — so deltas and the partial
# ``message_start`` are ignored here.


def _pi_assistant_blocks(content: Any, agent: str) -> list[OneShotItem]:
    items: list[OneShotItem] = []
    if not isinstance(content, list):
        return items
    text_parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                text_parts.append(text)
        elif block_type == "thinking":
            thinking = block.get("thinking")
            if isinstance(thinking, str) and thinking.strip():
                items.append(_reasoning_item(agent=agent, text=thinking))
        elif block_type == "toolCall":
            call_id = block.get("id") or block.get("toolCallId")
            name = block.get("name") or block.get("toolName")
            if isinstance(call_id, str) and call_id and isinstance(name, str) and name:
                arguments = block.get("arguments")
                if not isinstance(arguments, dict):
                    arguments = block.get("input") if isinstance(block.get("input"), dict) else {}
                items.append(
                    _function_call_item(
                        agent=agent, name=name, call_id=call_id, arguments=arguments
                    )
                )
    if text_parts:
        items.append(_message_item(agent=agent, text="".join(text_parts)))
    return items


def parse_pi_json_events(raw: str, *, agent: str = "Pi") -> tuple[list[OneShotItem], str | None]:
    """Parse Pi's ``--print --mode json`` stdout.

    :param raw: The one-shot process's full stdout.
    :param agent: Agent name attached to assistant/tool items.
    :returns: ``(items, final_text)`` — parsed items in stream order, and the
        text of the turn's last assistant message (the same text
        ``--print`` without ``--mode json`` would have printed alone), or
        ``None`` if no assistant message was seen.
    """
    items: list[OneShotItem] = []
    final_text: str | None = None
    for event in _iter_json_lines(raw):
        if event.get("type") != "message_end":
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "assistant":
            items.extend(_pi_assistant_blocks(message.get("content"), agent))
            text = _text_from_blocks(message.get("content"))
            if text:
                final_text = text
        elif role == "toolResult":
            call_id = message.get("toolCallId")
            if isinstance(call_id, str) and call_id:
                output = _text_from_blocks(message.get("content")) or _json_or_str(
                    message.get("content")
                )
                items.append(_function_call_output_item(call_id=call_id, output=output))
    return items, final_text


# ── Codex: `exec --json` ───────────────────────────────────────────────────
#
# A stream of ``thread.started``/``turn.started``/``item.started``/
# ``item.completed``/``turn.completed``. Only ``item.completed`` is mirrored:
# an item that never completes (killed mid-command) has nothing durable to
# record anyway, and ``item.started`` for a ``command_execution`` duplicates
# the same id once it completes. Item ``type`` values are snake_case here,
# unlike the app-server RPC's camelCase (``commandExecution`` etc. — see
# ``codex_native/forwarder.py``'s ``_TOOL_ITEM_BUILDERS``), so the field
# names below (``aggregated_output``, ``exit_code``, ...) differ accordingly
# even though the mapping to Omnigent's ``function_call``/
# ``function_call_output`` is the same idea.


def _codex_command_execution(item: _JsonObject, agent: str) -> list[OneShotItem]:
    call_id = item.get("id")
    command = item.get("command")
    if not isinstance(call_id, str) or not call_id or not isinstance(command, str) or not command:
        return []
    arguments: _JsonObject = {"command": command}
    cwd = item.get("cwd")
    if isinstance(cwd, str) and cwd:
        arguments["cwd"] = cwd
    output = item.get("aggregated_output")
    output_text = output if isinstance(output, str) else ""
    exit_code = item.get("exit_code")
    if isinstance(exit_code, int) and exit_code != 0:
        suffix = f"[exit code: {exit_code}]"
        output_text = f"{output_text}\n{suffix}" if output_text else suffix
    return [
        _function_call_item(agent=agent, name="shell", call_id=call_id, arguments=arguments),
        _function_call_output_item(call_id=call_id, output=output_text),
    ]


def _codex_file_change(item: _JsonObject, agent: str) -> list[OneShotItem]:
    call_id = item.get("id")
    changes = item.get("changes")
    if not isinstance(call_id, str) or not call_id or not isinstance(changes, list) or not changes:
        return []
    summary_lines: list[str] = []
    for change in changes:
        if not isinstance(change, dict):
            continue
        path = change.get("path")
        kind = change.get("kind")
        kind_type = kind.get("type") if isinstance(kind, dict) else None
        label = kind_type if isinstance(kind_type, str) and kind_type else "change"
        summary_lines.append(f"{label} {path}")
    return [
        _function_call_item(
            agent=agent, name="apply_patch", call_id=call_id, arguments={"changes": changes}
        ),
        _function_call_output_item(call_id=call_id, output="\n".join(summary_lines)),
    ]


def _codex_web_search(item: _JsonObject, agent: str) -> list[OneShotItem]:
    call_id = item.get("id")
    if not isinstance(call_id, str) or not call_id:
        return []
    action = item.get("action")
    queries = action.get("queries") if isinstance(action, dict) else None
    query_list = [q for q in queries if isinstance(q, str)] if isinstance(queries, list) else []
    if not query_list:
        query = item.get("query")
        if isinstance(query, str) and query:
            query_list = [query]
    if not query_list:
        return []
    return [
        _function_call_item(
            agent=agent, name="web_search", call_id=call_id, arguments={"query": query_list[0]}
        ),
        _function_call_output_item(call_id=call_id, output="\n".join(query_list)),
    ]


# Codex item types this parser mirrors as a function_call/function_call_output
# pair. mcp_tool_call/image_view/image_generation are left unmapped (as the
# app-server forwarder's _TOOL_ITEM_BUILDERS also leaves mcpToolCall unmapped)
# rather than guessing at a shape not yet verified against the real CLI.
_CODEX_ITEM_BUILDERS = {
    "command_execution": _codex_command_execution,
    "file_change": _codex_file_change,
    "web_search": _codex_web_search,
}


def parse_codex_exec_json(
    raw: str, *, agent: str = "Codex"
) -> tuple[list[OneShotItem], str | None]:
    """Parse Codex's ``exec --json`` stdout.

    :param raw: The one-shot process's full stdout.
    :param agent: Agent name attached to assistant/tool items.
    :returns: ``(items, final_text)`` — parsed items in stream order, and the
        text of the turn's last ``agent_message`` item, or ``None`` if none
        was seen. Callers should still prefer ``--output-last-message``'s
        file for the authoritative final answer when it is available; this
        is the fallback when that file could not be read.
    """
    items: list[OneShotItem] = []
    final_text: str | None = None
    for event in _iter_json_lines(raw):
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "agent_message":
            text = item.get("text")
            if isinstance(text, str) and text:
                items.append(_message_item(agent=agent, text=text))
                final_text = text
        elif item_type == "reasoning":
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                items.append(_reasoning_item(agent=agent, text=text))
        else:
            builder = _CODEX_ITEM_BUILDERS.get(item_type) if isinstance(item_type, str) else None
            if builder is not None:
                items.extend(builder(item, agent))
    return items, final_text
