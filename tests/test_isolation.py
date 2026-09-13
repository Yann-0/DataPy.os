"""SSH fallback refusal and sandbox isolation contract."""

from __future__ import annotations

import os

import pytest

from net.ssh_server import NovaSSHServer
from system.sandbox import Sandbox


def test_ssh_refuses_plaintext_without_paramiko(fake_kernel, monkeypatch):
    monkeypatch.setattr(
        NovaSSHServer, "_has_paramiko", lambda self: False
    )
    ssh = NovaSSHServer(fake_kernel, port=0)
    with pytest.raises(RuntimeError, match="plaintext"):
        ssh.start()
    with pytest.raises(RuntimeError, match="plaintext"):
        ssh._start_tcp_fallback()


def test_sandbox_reject_is_not_host_exec(fake_kernel):
    os.environ.pop("NOVA_SANDBOX_TRUSTED_DEV", None)
    box = Sandbox(fake_kernel, timeout=1)
    result = box.run_code("open('owned.txt','w').write('x')")
    assert result.returncode != 0
    assert "rejected" in result.stderr.lower()
