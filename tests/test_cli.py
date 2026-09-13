"""CLI dispatch, exit codes and durable put/get across processes."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(data_dir: Path, cmd: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["NOVA_DATA"] = str(data_dir)
    env["NOVA_NO_AI"] = "1"
    env["NOVA_SRC"] = str(ROOT)
    return subprocess.run(
        [sys.executable, str(ROOT / "main.py"), "--no-ai", "--cmd", cmd],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=40,
    )


def test_unknown_command_nonzero_exit(tmp_path):
    proc = _run(tmp_path / "d1", "definitely-not-a-command")
    assert proc.returncode != 0


def test_put_get_persists_across_invocations(tmp_path):
    data = tmp_path / "nova"
    put = _run(data, "data put clip hello-from-cli --tag ideas")
    assert put.returncode == 0, put.stderr + put.stdout
    got = _run(data, "data get clip")
    assert got.returncode == 0, got.stderr + got.stdout
    assert "hello-from-cli" in got.stdout
