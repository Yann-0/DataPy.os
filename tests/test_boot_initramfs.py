"""Regressions for bootstrap streams and device nodes in generated initramfs images."""

from __future__ import annotations

import ast
import gzip
import os
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from build import build_pi5_nova, usb_image


@pytest.fixture(params=[(build_pi5_nova.PYINIT_RPI5, "mount"),
                        (usb_image.PYINIT_BOOT, "_mount")])
def mount_helper(request):
    """Load the generated helper without executing PID-1 signal or boot code."""
    source, name = request.param
    compile(source, "<generated-init>", "exec")
    tree = ast.parse(source)
    fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
              and node.name == name)
    namespace = {"os": os, "subprocess": subprocess}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<mount-helper>", "exec"),
         namespace)
    return namespace[name]


def test_mount_bootstraps_without_devnull(mount_helper, monkeypatch, tmp_path):
    """Exercise real Popen stream setup with an absent null device and fake utility."""
    run = subprocess.run
    commands = []
    monkeypatch.setattr(os, "devnull", str(tmp_path / "missing-dev" / "null"))

    def fake_mount(command, **kwargs):
        commands.append(command)
        return run([sys.executable, "-c", "pass"], **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_mount)
    target = str(tmp_path / "dev")
    mount_helper("devtmpfs", target, "devtmpfs", "mode=0755")
    assert commands == [["mount", "-t", "devtmpfs", "-o", "mode=0755",
                         "devtmpfs", target]]


def test_mount_failure_is_not_reported_as_success(mount_helper, monkeypatch, tmp_path):
    """A real nonzero utility exit must propagate out of the mount helper."""
    run = subprocess.run

    def failing_mount(command, **kwargs):
        return run([sys.executable, "-c", "raise SystemExit(32)"], **kwargs)

    monkeypatch.setattr(subprocess, "run", failing_mount)
    with pytest.raises(subprocess.CalledProcessError) as error:
        mount_helper("proc", str(tmp_path / "proc"), "proc")
    assert error.value.returncode == 32


def newc_entries(raw: bytes) -> dict[str, dict]:
    """Decode serialized newc headers independently of the archive writers."""
    result = {}
    offset = 0
    while offset < len(raw):
        assert raw[offset:offset + 6] == b"070701"
        values = [int(raw[offset + 6 + i * 8:offset + 14 + i * 8], 16)
                  for i in range(13)]
        name_start = offset + 110
        name = raw[name_start:name_start + values[11] - 1].decode()
        data_start = (name_start + values[11] + 3) & ~3
        result[name] = {"mode": values[1], "major": values[9], "minor": values[10],
                        "data": raw[data_start:data_start + values[6]]}
        offset = (data_start + values[6] + 3) & ~3
        if name == "TRAILER!!!":
            return result
    raise AssertionError("CPIO trailer missing")


@pytest.mark.parametrize("writer_class", [build_pi5_nova.CPIOWriter, usb_image.CPIOWriter])
def test_cpio_character_device_headers(writer_class):
    """Verify character-device identity, permissions, payload and entry alignment."""
    writer = writer_class()
    writer.add_dir("dev")
    writer.add_device("dev/console", 5, 1, permissions=0o600)
    writer.add_device("dev/null", 1, 3, permissions=0o666)
    writer.add_file("after-device", b"payload")
    writer.add_symlink("init", "/sbin/init")
    entries = newc_entries(writer.build())
    for name, major, minor, permissions in [("console", 5, 1, 0o600),
                                             ("null", 1, 3, 0o666)]:
        item = entries[f"dev/{name}"]
        assert stat.S_ISCHR(item["mode"])
        assert stat.S_IMODE(item["mode"]) == permissions
        assert (item["major"], item["minor"]) == (major, minor)
        assert item["data"] == b""
    assert entries["after-device"]["data"] == b"payload"
    assert entries["init"]["data"] == b"/sbin/init"


def assert_boot_devices(entries):
    """Require usable console and null nodes before devtmpfs exists."""
    for name, major, minor in [("console", 5, 1), ("null", 1, 3)]:
        item = entries[f"dev/{name}"]
        assert stat.S_ISCHR(item["mode"])
        assert (item["major"], item["minor"]) == (major, minor)


def test_pi_initramfs_includes_boot_devices(tmp_path):
    """Inspect the actual compressed archive produced with a tiny runtime fixture."""
    runtime = tmp_path / "python"
    (runtime / "bin").mkdir(parents=True)
    (runtime / "bin" / "python3").write_bytes(b"runtime fixture, not executable")
    (runtime / "lib").mkdir()
    source = tmp_path / "source.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("nova/main.py", '"""Fixture entry point."""\n')
    output = tmp_path / "initramfs.gz"
    build_pi5_nova.build_initramfs(runtime, source, output)
    entries = newc_entries(gzip.decompress(output.read_bytes()))
    assert_boot_devices(entries)
    assert entries["nova/boot/pyinit_rpi5.py"]["data"] == build_pi5_nova.PYINIT_RPI5.encode()


def test_usb_initramfs_includes_boot_devices(tmp_path):
    """Inspect the USB builder's emitted bootstrap directory and config entries."""
    builder = usb_image.InitramfsBuilder(Path(tmp_path))
    builder._add_directories()
    builder._add_system_utils()
    assert_boot_devices(newc_entries(builder.cpio.build()))
