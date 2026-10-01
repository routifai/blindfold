"""Environment configuration for the long-term memory feature."""

from __future__ import annotations

import os
from dataclasses import dataclass

# litellm reads the provider key itself (e.g. OPENAI_API_KEY for the default
# model); Omnigent never reads or forwards the key, it only names the model.
_DEFAULT_MODEL = "openai/text-embedding-3-small"
_MODEL_ENV_VAR = "OMNIGENT_MEMORY_EMBEDDINGS_MODEL"


@dataclass(frozen=True)
class MemoryConfig:
    """
    Resolved configuration for the memory index.

    :param embeddings_model: The litellm-style model id passed to txtai's
        ``method="litellm"`` vectorizer, e.g. ``"openai/text-embedding-3-small"``.
    """

    embeddings_model: str = _DEFAULT_MODEL

    @classmethod
    def from_env(cls) -> MemoryConfig:
        """Build a :class:`MemoryConfig` from the process environment."""
        return cls(embeddings_model=os.environ.get(_MODEL_ENV_VAR, _DEFAULT_MODEL))
