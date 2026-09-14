"""
Format a labelled ext4 filesystem inside a regular image file.

Creating an MBR type-0x83 partition is not creating a filesystem.
This helper writes the filesystem into a new file (or a slice of a new
disk image) and never formats a live user device.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

log = logging.getLogger("nova.build.persist_fs")

NOVA_DATA_LABEL = "NOVA_DATA"


class PersistFormatError(Exception):
    """Raised when the persistent filesystem cannot be created or verified."""


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    log.info("persist-fs: %s", " ".join(cmd))
    return subprocess.run(cmd, check=False, capture_output=True, text=True, **kwargs)


def _linux_mkfs_ext4(image: Path, label: str = NOVA_DATA_LABEL) -> None:
    mkfs = shutil.which("mkfs.ext4")
    if not mkfs:
        raise PersistFormatError("mkfs.ext4 is required to format NOVA_DATA")
    proc = _run([mkfs, "-F", "-L", label, "-q", str(image)])
    if proc.returncode != 0:
        raise PersistFormatError(
            f"mkfs.ext4 failed: {proc.stderr.strip() or proc.stdout}"
        )


def _wsl_mkfs_ext4(image: Path, label: str = NOVA_DATA_LABEL) -> None:
    wsl = shutil.which("wsl")
    if not wsl:
        raise PersistFormatError(
            "ext4 formatting requires Linux mkfs.ext4 or WSL; "
            "refusing a fake Python fallback"
        )
    win = str(image.resolve())
    proc = _run([
        wsl, "-d", "Ubuntu", "--", "bash", "-lc",
        f"mkfs.ext4 -F -L {label} -q $(wslpath '{win}')",
    ])
    if proc.returncode != 0:
        raise PersistFormatError(
            f"WSL mkfs.ext4 failed: {proc.stderr.strip() or proc.stdout}"
        )


def format_ext4_file(image: Path, size_bytes: int,
                     label: str = NOVA_DATA_LABEL) -> Path:
    """Create ``image`` as a sparse file and format ext4 with ``label``."""
    image.parent.mkdir(parents=True, exist_ok=True)
    with open(image, "wb") as fh:
        fh.truncate(size_bytes)
    if sys.platform == "linux":
        _linux_mkfs_ext4(image, label)
    else:
        _wsl_mkfs_ext4(image, label)
    verify_ext4_label(image, label)
    return image


def verify_ext4_label(image: Path, label: str = NOVA_DATA_LABEL) -> None:
    """Confirm the ext superblock label without mounting a host disk."""
    with open(image, "rb") as fh:
        fh.seek(1024 + 0x78)
        raw = fh.read(16)
    got = raw.split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()
    if got != label:
        raise PersistFormatError(
            f"ext4 label mismatch: expected {label!r} got {got!r}"
        )
    # Magic at superblock offset 0x38 is 0xEF53
    with open(image, "rb") as fh:
        fh.seek(1024 + 0x38)
        magic = fh.read(2)
    if magic != b"\x53\xEF":
        raise PersistFormatError("ext4 magic 0xEF53 missing")


def write_partition_slice(disk_image: Path, start_bytes: int, size_bytes: int,
                          source_fs: Path) -> None:
    """Copy a formatted filesystem image into a partition slice."""
    src_size = source_fs.stat().st_size
    if src_size > size_bytes:
        raise PersistFormatError("filesystem image larger than partition")
    with open(source_fs, "rb") as src, open(disk_image, "r+b") as dst:
        dst.seek(start_bytes)
        remaining = src_size
        while remaining:
            chunk = src.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            dst.write(chunk)
            remaining -= len(chunk)
