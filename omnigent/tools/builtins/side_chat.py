"""Built-in tool: open a Side Chat from the Super Chat (Muse's ``chat.create``).

Schema-only — like the browser tools, execution needs the runner's
authenticated server client (fork/create a session, bind a host/runner),
which :class:`~omnigent.tools.base.ToolContext` does not carry. The runner
dispatches it via ``omnigent/runner/tool_dispatch.py``.
"""

from __future__ import annotations

from typing import Any

from omnigent.superchat.chats import SIDE_CHAT_START_VALUES
from omnigent.tools.base import Tool


class SideChatOpenTool(Tool):
    """
    Open a Side Chat branched from the Super Chat.

    Only callable from the Super Chat itself — refused from a Side Chat
    (Side Chats branch only from the Super Chat, never from each other) and
    from a Sub-agent. Two starting modes:

    - ``with_context``: forks the Super Chat with a seeded summary of the
      current discussion plus its recent turns, for continuing a topic
      without cluttering the Super Chat.
    - ``blank``: a fresh chat with only the Memory Profile, for something
      unrelated to the current discussion.

    Runs on the same host/runner as the Super Chat. Returns a handle with
    the new Side Chat's ``conversation_id``.
    """

    @classmethod
    def name(cls) -> str:
        """:returns: ``"side_chat_open"``."""
        return "side_chat_open"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Open a Side Chat branched from this Super Chat, for a focused aside "
            "without cluttering the main conversation. Only callable from the "
            "Super Chat itself — refused from a Side Chat or a Sub-agent. "
            "start='with_context' seeds the new chat with a summary of this "
            "discussion plus its recent turns, for continuing the current topic. "
            "start='blank' opens it with only the standing Memory Profile, for "
            "something unrelated. Optionally queue first_message as the Side "
            "Chat's first user turn. Returns {conversation_id, title, start}. "
            "The user reads it later via session_history's list_chats/read, or "
            "opens it directly; it is archived automatically after a period of "
            "inactivity, never deleted."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI-format tool schema.

        :returns: Dict with ``"type": "function"`` and a ``"function"``
            sub-dict. ``title`` and ``start`` are required; ``first_message``
            is optional.
        """
        return {
            "type": "function",
            "function": {
                "name": SideChatOpenTool.name(),
                "description": SideChatOpenTool.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": (
                                "Human-readable title for the new Side Chat, "
                                "e.g. 'Checking the Q3 spreadsheet'."
                            ),
                        },
                        "start": {
                            "type": "string",
                            "enum": sorted(SIDE_CHAT_START_VALUES),
                            "description": (
                                "'with_context' seeds the Side Chat with a summary "
                                "of this discussion plus recent turns; 'blank' "
                                "starts with only the Memory Profile."
                            ),
                        },
                        "first_message": {
                            "type": "string",
                            "description": (
                                "Optional first user message to queue in the new "
                                "Side Chat. Omit to open it idle."
                            ),
                        },
                    },
                    "required": ["title", "start"],
                    "additionalProperties": False,
                },
            },
        }
