"""FAT32 writer, PID-1 reaper, NIDS lock, Raft majority, sandbox reject."""

from __future__ import annotations

import os
import threading
import time

import pytest

from boot.linux import reap_zombies
from build.fat32 import Fat32Error, build_fat32_files, parse_fat32
from security.innovations import NetworkIDS
from system.enterprise import RaftNode
from system.sandbox import Sandbox


def test_fat32_cluster_chains_and_long_names(tmp_path):
    files = {
        "/config.txt": b"arm_64bit=1\n",
        "/very-long-firmware-filename-kernel.img": b"K" * 9000,
        "/overlays/custom.dtbo": b"dtb",
    }
    raw = build_fat32_files(8 * 1024 * 1024, files, label="NOVABOOT")
    parsed = parse_fat32(raw)
    assert parsed.read_file("/config.txt") == files["/config.txt"]
    assert parsed.read_file("/very-long-firmware-filename-kernel.img") == files[
        "/very-long-firmware-filename-kernel.img"
    ]
    assert parsed.read_file("/overlays/custom.dtbo") == b"dtb"


def test_fat32_capacity_bounds():
    with pytest.raises(Fat32Error):
        build_fat32_files(2 * 1024 * 1024, {"/huge.bin": b"x" * 8 * 1024 * 1024})


def test_reap_breaks_on_zero_pid():
    calls = {"n": 0}

    def fake_wait(_pid, flags):
        calls["n"] += 1
        if calls["n"] == 1:
            return (42, 0x100)
        return (0, 0)

    reaped = reap_zombies(fake_wait, flags=1)
    assert [r.pid for r in reaped] == [42]
    assert calls["n"] == 2


def test_nids_inspect_does_not_deadlock(sos):
    nids = NetworkIDS(sos)
    errors = []

    def worker(i: int) -> None:
        try:
            for _ in range(20):
                nids.inspect("1.2.3.4", "/login", body="x" * 10)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive()
    assert not errors


def test_raft_majority_even_clusters():
    assert RaftNode.majority(1) == 1
    assert RaftNode.majority(2) == 2
    assert RaftNode.majority(4) == 3
    node = RaftNode("n1", ["http://a", "http://b", "http://c"], sos=None)  # type: ignore[arg-type]
    with pytest.raises(NotImplementedError):
        node.append_entries()


def test_sandbox_rejects_untrusted_without_isolation(fake_kernel):
    os.environ.pop("NOVA_SANDBOX_TRUSTED_DEV", None)
    box = Sandbox(fake_kernel, timeout=1)
    result = box.run_code("print('hi')")
    assert result.returncode != 0
    assert "rejected" in result.stderr.lower() or "isolation" in result.stderr.lower()
