"""Shared fixtures for omnigent.superchat tests."""

from __future__ import annotations

import pytest

from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


@pytest.fixture()
def conversation_store(db_uri: str) -> SqlAlchemyConversationStore:
    """:returns: A SqlAlchemyConversationStore backed by the test database."""
    return SqlAlchemyConversationStore(db_uri)
