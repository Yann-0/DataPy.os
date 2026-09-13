"""Persistent image filesystem: label/magic and WSL/Linux formatting."""

from __future__ import annotations

import shutil
import sys

import pytest

from build.persist_fs import (
    NOVA_DATA_LABEL,
    PersistFormatError,
    format_ext4_file,
    verify_ext4_label,
)


def test_verify_ext4_label_from_crafted_superblock(tmp_path):
    image = tmp_path / "fs.img"
    raw = bytearray(4096)
    raw[1024 + 0x38:1024 + 0x3A] = b"\x53\xEF"
    raw[1024 + 0x78:1024 + 0x78 + 16] = NOVA_DATA_LABEL.encode("ascii").ljust(16, b"\x00")
    image.write_bytes(raw)
    verify_ext4_label(image, NOVA_DATA_LABEL)


def test_verify_ext4_rejects_wrong_magic(tmp_path):
    image = tmp_path / "bad.img"
    image.write_bytes(b"\x00" * 4096)
    with pytest.raises(PersistFormatError):
        verify_ext4_label(image)


@pytest.mark.skipif(
    sys.platform != "linux" and shutil.which("wsl") is None,
    reason="mkfs.ext4/WSL not available on this host",
)
def test_format_ext4_inside_new_regular_file(tmp_path):
    image = tmp_path / "nova_data.img"
    format_ext4_file(image, 8 * 1024 * 1024, NOVA_DATA_LABEL)
    verify_ext4_label(image, NOVA_DATA_LABEL)
    assert image.is_file()
    assert image.stat().st_size >= 8 * 1024 * 1024
