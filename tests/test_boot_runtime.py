"""Python prefix normalization used by the initramfs builder."""

from __future__ import annotations

from pathlib import Path

from build.build_pi5_nova import python_prefix


def test_python_prefix_strips_nested_python_dir(tmp_path):
    nested = tmp_path / "python" / "bin"
    nested.mkdir(parents=True)
    (nested / "python3").write_bytes(b"x")
    assert python_prefix(tmp_path) == tmp_path / "python"


def test_python_prefix_keeps_flat_prefix(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "python3").write_bytes(b"x")
    assert python_prefix(tmp_path) == tmp_path
