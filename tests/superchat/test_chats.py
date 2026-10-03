"""Unit tests for ``omnigent.superchat.chats``: side_chat_open eligibility and
request bodies, the auto-archive sweep, and the unarchive-on-message rule.

No DB, no server, no live calls — a tiny in-memory fake stands in for
``ConversationStore`` (only the handful of methods this module calls).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from omnigent.superchat.chats import (
    ARCHIVE_AFTER_SECONDS_ENV,
    DEFAULT_ARCHIVE_AFTER_SECONDS,
    SideChatArchiveSweeper,
    build_side_chat_blank_create_body,
    build_side_chat_fork_body,
    is_stale_side_chat,
    maybe_unarchive_on_user_message,
    refuse_side_chat_open,
    resolve_archive_after_seconds,
    sweep_side_chats,
)

_SUPERSIDE = {"omnigent.context.mode": "superside-chat"}
_SIDE_CHAT = {**_SUPERSIDE, "omnigent.side_chat": "1"}


# ── refuse_side_chat_open ──────────────────────────────────────────────────


def test_refuses_outside_superside_chat_mode() -> None:
    assert refuse_side_chat_open(labels={}, kind="default", parent_session_id=None) is not None


def test_refuses_rollover_mode_too() -> None:
    """Rollover (native-CLI) is a different mode value, never superside-chat."""
    labels = {"omnigent.context.mode": "rollover"}
    assert refuse_side_chat_open(labels=labels, kind="default", parent_session_id=None) is not None


def test_refuses_from_a_side_chat() -> None:
    reason = refuse_side_chat_open(labels=_SIDE_CHAT, kind="default", parent_session_id=None)
    assert reason is not None
    assert "Side Chat" in reason


def test_refuses_from_a_sub_agent_by_kind() -> None:
    reason = refuse_side_chat_open(labels=_SUPERSIDE, kind="sub_agent", parent_session_id=None)
    assert reason is not None
    assert "Sub-agent" in reason


def test_refuses_from_a_sub_agent_by_parent_session_id() -> None:
    """A sub-agent's ``kind`` may read 'default' on some paths; parent_session_id
    alone is enough to refuse."""
    reason = refuse_side_chat_open(
        labels=_SUPERSIDE, kind="default", parent_session_id="conv_parent"
    )
    assert reason is not None


def test_allowed_from_the_super_chat() -> None:
    assert refuse_side_chat_open(labels=_SUPERSIDE, kind="default", parent_session_id=None) is None


# ── request bodies ──────────────────────────────────────────────────────


def test_fork_body_has_side_chat_flag_and_title() -> None:
    body = build_side_chat_fork_body("Checking the spreadsheet")
    assert body == {"side_chat": True, "title": "Checking the spreadsheet"}


def test_fork_body_omits_title_when_blank() -> None:
    assert build_side_chat_fork_body(None) == {"side_chat": True}
    assert build_side_chat_fork_body("") == {"side_chat": True}


def test_blank_create_body_carries_discovery_labels() -> None:
    body = build_side_chat_blank_create_body(
        agent_id="ag_123", title="Unrelated thing", source_conversation_id="conv_super"
    )
    assert body["agent_id"] == "ag_123"
    assert body["title"] == "Unrelated thing"
    assert body["labels"] == {
        "omnigent.context.mode": "superside-chat",
        "omnigent.side_chat": "1",
        "omnigent.fork.source_id": "conv_super",
    }


# ── resolve_archive_after_seconds ───────────────────────────────────────


def test_default_archive_after_seconds_is_30_days(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ARCHIVE_AFTER_SECONDS_ENV, raising=False)
    assert resolve_archive_after_seconds() == DEFAULT_ARCHIVE_AFTER_SECONDS
    assert DEFAULT_ARCHIVE_AFTER_SECONDS == 2592000


def test_archive_after_seconds_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ARCHIVE_AFTER_SECONDS_ENV, "3600")
    assert resolve_archive_after_seconds() == 3600


def test_archive_after_seconds_falls_back_on_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ARCHIVE_AFTER_SECONDS_ENV, "not-a-number")
    assert resolve_archive_after_seconds() == DEFAULT_ARCHIVE_AFTER_SECONDS


def test_archive_after_seconds_falls_back_on_non_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ARCHIVE_AFTER_SECONDS_ENV, "0")
    assert resolve_archive_after_seconds() == DEFAULT_ARCHIVE_AFTER_SECONDS
    monkeypatch.setenv(ARCHIVE_AFTER_SECONDS_ENV, "-5")
    assert resolve_archive_after_seconds() == DEFAULT_ARCHIVE_AFTER_SECONDS


# ── is_stale_side_chat ──────────────────────────────────────────────────


def test_is_stale_side_chat_true_when_idle_past_threshold() -> None:
    assert is_stale_side_chat(
        labels=_SIDE_CHAT, archived=False, updated_at=0, now=3600, after_seconds=3600
    )


def test_is_stale_side_chat_false_when_not_idle_long_enough() -> None:
    assert not is_stale_side_chat(
        labels=_SIDE_CHAT, archived=False, updated_at=100, now=3600, after_seconds=3600
    )


def test_is_stale_side_chat_false_when_already_archived() -> None:
    assert not is_stale_side_chat(
        labels=_SIDE_CHAT, archived=True, updated_at=0, now=10_000, after_seconds=3600
    )


def test_is_stale_side_chat_false_for_the_super_chat() -> None:
    """Super Chat labels carry the mode but no side-chat label."""
    assert not is_stale_side_chat(
        labels=_SUPERSIDE, archived=False, updated_at=0, now=10_000, after_seconds=3600
    )


def test_is_stale_side_chat_false_outside_superside_chat_mode() -> None:
    assert not is_stale_side_chat(
        labels={"omnigent.side_chat": "1"},
        archived=False,
        updated_at=0,
        now=10_000,
        after_seconds=3600,
    )


# ── sweep_side_chats (fake store) ────────────────────────────────────────


@dataclass
class _FakeConversation:
    id: str
    updated_at: int
    archived: bool = False
    labels: dict[str, str] = field(default_factory=dict)


class _FakeConversationStore:
    """Minimal stand-in: only the methods ``sweep_side_chats`` calls."""

    def __init__(self, conversations: list[_FakeConversation]) -> None:
        # Sorted ascending by updated_at, as the real store's
        # ``sort_by="updated_at", order="asc"`` would return.
        self._conversations = sorted(conversations, key=lambda c: c.updated_at)

    def list_conversations(self, *, limit: int, after: str | None, **_kwargs: object):
        from omnigent.entities import PagedList

        start = 0
        if after is not None:
            for i, conv in enumerate(self._conversations):
                if conv.id == after:
                    start = i + 1
                    break
        page = self._conversations[start : start + limit]
        has_more = start + limit < len(self._conversations)
        return PagedList(data=list(page), has_more=has_more)

    def update_conversation(self, conversation_id: str, *, archived: bool | None = None) -> None:
        for conv in self._conversations:
            if conv.id == conversation_id and archived is not None:
                conv.archived = archived

    def get_conversation(self, conversation_id: str) -> _FakeConversation | None:
        return next((c for c in self._conversations if c.id == conversation_id), None)


def test_sweep_archives_only_stale_side_chats() -> None:
    store = _FakeConversationStore(
        [
            # idle but not a side chat
            _FakeConversation("super", updated_at=0, labels=_SUPERSIDE),
            _FakeConversation("stale_side", updated_at=0, labels=_SIDE_CHAT),
            _FakeConversation("fresh_side", updated_at=9_999, labels=_SIDE_CHAT),
        ]
    )
    archived = sweep_side_chats(store, now=10_000, after_seconds=3600)
    assert archived == 1
    assert store.get_conversation("stale_side").archived is True
    assert store.get_conversation("fresh_side").archived is False
    assert store.get_conversation("super").archived is False


def test_sweep_never_archives_the_super_chat() -> None:
    store = _FakeConversationStore([_FakeConversation("super", updated_at=0, labels=_SUPERSIDE)])
    archived = sweep_side_chats(store, now=10_000, after_seconds=3600)
    assert archived == 0
    assert store.get_conversation("super").archived is False


def test_sweep_stops_early_once_rows_are_fresh() -> None:
    """Ascending sort means the first fresh row ends the scan — covered by
    behavior: a stale row AFTER a fresh one (impossible in sorted order) is
    not constructed; instead verify a page of only-fresh rows archives none
    without raising/looping."""
    store = _FakeConversationStore(
        [_FakeConversation(f"side_{i}", updated_at=10_000, labels=_SIDE_CHAT) for i in range(5)]
    )
    archived = sweep_side_chats(store, now=10_000, after_seconds=3600, page_size=2)
    assert archived == 0


def test_sweep_respects_custom_after_seconds() -> None:
    store = _FakeConversationStore([_FakeConversation("side", updated_at=0, labels=_SIDE_CHAT)])
    assert sweep_side_chats(store, now=100, after_seconds=3600) == 0
    assert sweep_side_chats(store, now=100, after_seconds=50) == 1


# ── maybe_unarchive_on_user_message ──────────────────────────────────────


def test_unarchives_an_archived_side_chat() -> None:
    store = _FakeConversationStore(
        [_FakeConversation("side", updated_at=0, archived=True, labels=_SIDE_CHAT)]
    )
    assert maybe_unarchive_on_user_message(store, "side") is True
    assert store.get_conversation("side").archived is False


def test_does_not_unarchive_the_super_chat() -> None:
    store = _FakeConversationStore(
        [_FakeConversation("super", updated_at=0, archived=True, labels=_SUPERSIDE)]
    )
    assert maybe_unarchive_on_user_message(store, "super") is False
    assert store.get_conversation("super").archived is True


def test_noop_when_not_archived() -> None:
    store = _FakeConversationStore(
        [_FakeConversation("side", updated_at=0, archived=False, labels=_SIDE_CHAT)]
    )
    assert maybe_unarchive_on_user_message(store, "side") is False


def test_noop_when_conversation_unknown() -> None:
    store = _FakeConversationStore([])
    assert maybe_unarchive_on_user_message(store, "nope") is False


# ── SideChatArchiveSweeper ────────────────────────────────────────────────


def test_sweeper_sweep_once_uses_its_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ARCHIVE_AFTER_SECONDS_ENV, "3600")
    store = _FakeConversationStore([_FakeConversation("side", updated_at=0, labels=_SIDE_CHAT)])
    sweeper = SideChatArchiveSweeper(store, clock=lambda: 10_000)
    archived = sweeper.sweep_once()
    assert archived == 1
    assert store.get_conversation("side").archived is True
