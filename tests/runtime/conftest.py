"""
Ambient-discovery calibration helpers for the runtime spawn-env tests.

The suite-wide ``_isolate_ambient_provider_discovery`` autouse fixture
(``tests/conftest.py``) severs ambient provider detection (vendor API keys,
a live local Ollama, a macOS Keychain Claude login) for every test. This
module only keeps the pieces specific to ``tests/runtime``: the unpatched
probes, captured before that fixture rebinds them, so a calibration
assertion here can prove the ambient state would have been visible without
the isolation.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omnigent.onboarding import ambient

# Captured at import time (before the suite-wide autouse fixture patches
# them per test), so the calibration controls can prove the ambient state is
# real rather than coincidentally absent.
_REAL_OLLAMA_REACHABLE = ambient._ollama_reachable
_REAL_CLAUDE_LOGIN_DETECTED = ambient._claude_login_detected


@pytest.fixture
def real_ambient_probes() -> SimpleNamespace:
    """
    Expose the unpatched ambient probes for calibration assertions.

    A test that isolates ambient state should first prove the state would
    have been visible without the isolation; otherwise it passes vacuously on
    a machine where no Ollama runs and no Claude login exists.

    :returns: Namespace with ``ollama_reachable`` and ``claude_login_detected``
        bound to the real, unpatched probe functions.
    """
    return SimpleNamespace(
        ollama_reachable=_REAL_OLLAMA_REACHABLE,
        claude_login_detected=_REAL_CLAUDE_LOGIN_DETECTED,
    )
