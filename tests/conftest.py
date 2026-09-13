"""
PyOS NOVA — pytest conftest  (v0.0008)
=======================================
Shared fixtures used across all test modules.

Every test that needs a SOS or kernel instance gets a fresh,
isolated, temporary database via these fixtures.  No test
touches the real ~/.nova data directory.
"""

from __future__ import annotations

import os
import sys
import tempfile
import shutil
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ── environment isolation ─────────────────────────────────────────────────────

@pytest.fixture(scope="session", autouse=True)
def isolate_nova_data(tmp_path_factory):
    """
    Redirect NOVA_DATA to a session-scoped temp directory.

    Runs once per test session before any test, undoes after the
    last test completes.  Prevents any test from reading or writing
    to the real ~/.nova directory.
    """
    data_dir = str(tmp_path_factory.mktemp("nova_data"))
    old_value = os.environ.get("NOVA_DATA")
    os.environ["NOVA_DATA"] = data_dir
    yield data_dir
    if old_value is None:
        os.environ.pop("NOVA_DATA", None)
    else:
        os.environ["NOVA_DATA"] = old_value


# ── SOS fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_dir(tmp_path):
    """Return a fresh temporary directory path (function-scoped)."""
    return str(tmp_path)


@pytest.fixture
def sos(tmp_path):
    """
    Return a fresh, isolated SemanticObjectStore backed by a temp SQLite DB.

    Each test function gets its own database; no state bleeds between tests.
    """
    from store.sos import SemanticObjectStore

    db_path = str(tmp_path / "test.db")
    store   = SemanticObjectStore(db_path=db_path)
    yield store
    # Cleanup is handled by tmp_path fixture


@pytest.fixture
def populated_sos(sos):
    """
    Return a SOS pre-populated with 50 objects across several directories.

    Useful for tests that need data to query, search, or iterate over.
    """
    for i in range(10):
        sos.write(f"/home/root/file{i}.py",
                   f"def func_{i}():\n    return {i} * 2\n",
                   kind="code",
                   tags=["python", f"file{i}"])
    for i in range(10):
        sos.write(f"/home/root/data/record{i}.json",
                   f'{{"id": {i}, "value": {i * 10}}}',
                   kind="data",
                   tags=["json"])
    for i in range(10):
        sos.write(f"/docs/doc{i}.md",
                   f"# Document {i}\n\nContent for document {i}.\n",
                   kind="text",
                   tags=["markdown"])
    for i in range(10):
        sos.write(f"/logs/app{i}.log",
                   f"INFO  2024-01-{i+1:02d} Application started\n"
                   f"DEBUG 2024-01-{i+1:02d} Processing event {i}\n",
                   kind="text",
                   tags=["log"])
    for i in range(10):
        sos.write(f"/config/setting{i}.toml",
                   f"[settings]\nkey_{i} = {i}\nenabled = true\n",
                   kind="data",
                   tags=["config"])
    return sos


# ── Kernel fixtures ───────────────────────────────────────────────────────────

class _FakeAI:
    """Minimal AI engine stub for tests that don't need real inference."""

    tier = "rag"

    def ask(self, prompt: str, max_tokens: int = 200, **kw) -> str:
        """Return a deterministic stub response."""
        return f"[stub response to: {prompt[:40]}]"

    def chat(self, messages, **kw):
        """Yield a single stub token."""
        yield "[stub]"


class _FakeKernel:
    """
    Minimal kernel stub that exposes sos, ai, and common attributes.

    Used by tests that need a kernel reference but don't want to
    boot the full NovaKernel (which loads AI models, starts threads, etc.).
    """

    def __init__(self, sos):
        """Initialise with a SOS instance."""
        self.sos    = sos
        self.ai     = _FakeAI()
        self.user   = "root"
        self.cwd    = "/home/root"

        # Lazy stubs — only created if accessed
        self._tracer  = None
        self._prefetch = None

    @property
    def tracer(self):
        """Return a stub tracer."""
        if self._tracer is None:
            class _T:
                def recent_traces(self, n=20): return []
                def get_trace(self, tid): return []
                def start_span(self, name, **kw):
                    from unittest.mock import MagicMock
                    return MagicMock()
            self._tracer = _T()
        return self._tracer

    @property
    def prefetch(self):
        """Return a stub prefetch engine."""
        if self._prefetch is None:
            class _P:
                _transitions: dict = {}
                def _predict(self, path, k=2): return []
            self._prefetch = _P()
        return self._prefetch


@pytest.fixture
def fake_kernel(sos):
    """Return a lightweight fake kernel wired to the test SOS."""
    return _FakeKernel(sos)


# ── Network fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def free_port():
    """Return a free TCP port on localhost."""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port
