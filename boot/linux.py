"""
Linux platform bootstrap adapter for DataPy.os PID-1.

This module is the supported Linux/Pi boundary: mounts, labelled-volume
discovery, child reaping and reboot/halt. Persistent application data still
belongs in SOS. Host development must not import this as a generic runtime.
"""

from __future__ import annotations

import logging
import os
import stat
import sys
import time
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger("nova.boot.linux")

NOVA_DATA_LABEL = "NOVA_DATA"
DEFAULT_MOUNTPOINT = "/mnt/nova_data"


class BootstrapError(Exception):
    """Raised when an essential Linux bootstrap step fails."""


@dataclass
class ReapResult:
    """One waitpid result from the PID-1 reaper."""

    pid: int
    status: int


def reap_zombies(
    waitpid: Callable[[int, int], tuple[int, int]] | None = None,
    *,
    flags: int | None = None,
) -> list[ReapResult]:
    """Reap exited children without spinning when no child has exited.

    ``os.waitpid(-1, WNOHANG)`` returns ``(0, 0)`` when children still
    live. The previous loop only broke on ``ChildProcessError``, so a
    live child caused a busy-spin. A zero pid ends the loop. The child's
    wait status is preserved so shutdown can inspect it.
    """
    wait = waitpid or os.waitpid
    wnohang = flags if flags is not None else getattr(os, "WNOHANG", 1)
    reaped: list[ReapResult] = []
    while True:
        try:
            pid, status = wait(-1, wnohang)
        except ChildProcessError:
            break
        except OSError as exc:
            import errno as errno_mod

            if getattr(exc, "errno", None) == errno_mod.ECHILD:
                break
            raise
        if pid == 0:
            break
        reaped.append(ReapResult(pid=pid, status=status))
    return reaped


def linux_mount(src: str, dst: str, fstype: str, opts: str = "") -> None:
    """Mount using the Linux ``os.mount`` syscall, not a userspace binary.

    Essential mounts must raise ``BootstrapError`` with the OS error. This
    replaces the missing ``mount`` utility in the initramfs.
    """
    if sys.platform != "linux":
        raise BootstrapError(f"linux_mount is not available on {sys.platform}")
    os.makedirs(dst, exist_ok=True)
    flags = 0
    try:
        os.mount(src, dst, fstype, flags, opts or None)
    except OSError as exc:
        raise BootstrapError(
            f"mount {src} -> {dst} ({fstype},{opts}) failed: {exc}"
        ) from exc


def _read_ext_label(dev_path: str) -> str | None:
    """Read an ext filesystem volume label from a block device or image."""
    try:
        fd = os.open(dev_path, os.O_RDONLY)
    except OSError:
        return None
    try:
        os.lseek(fd, 1024 + 0x78, os.SEEK_SET)  # s_volume_name
        raw = os.read(fd, 16)
    except OSError:
        return None
    finally:
        os.close(fd)
    if not raw:
        return None
    label = raw.split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()
    return label or None


def _candidate_block_devices() -> list[str]:
    """Return candidate block devices without guessing a host /dev/sdX."""
    found: list[str] = []
    by_label = "/dev/disk/by-label/" + NOVA_DATA_LABEL
    if os.path.exists(by_label):
        found.append(os.path.realpath(by_label))
    sys_block = "/sys/block"
    if not os.path.isdir(sys_block):
        return found
    for name in sorted(os.listdir(sys_block)):
        if name.startswith(("loop", "ram", "zram", "sr")):
            continue
        base = f"/dev/{name}"
        if os.path.exists(base):
            found.append(base)
        # Partition nodes: sda1, mmcblk0p2, nvme0n1p2
        for part in sorted(os.listdir(os.path.join(sys_block, name))):
            if not part.startswith(name):
                continue
            path = f"/dev/{part}"
            if os.path.exists(path) and path not in found:
                found.append(path)
    return found


def discover_labeled_partition(
    label: str = NOVA_DATA_LABEL,
    *,
    timeout_s: float = 8.0,
    poll_s: float = 0.25,
) -> str:
    """Return the unique block device whose filesystem label matches.

    Device enumeration on USB can lag. Ambiguous matches are errors: never
    mount an arbitrary disk and never format a live user partition here.
    """
    deadline = time.monotonic() + timeout_s
    last_seen: list[str] = []
    while True:
        matches: list[str] = []
        for dev in _candidate_block_devices():
            try:
                mode = os.stat(dev).st_mode
            except OSError:
                continue
            if not (stat.S_ISBLK(mode) or stat.S_ISREG(mode)):
                continue
            got = _read_ext_label(dev)
            if got == label:
                matches.append(dev)
        last_seen = matches
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise BootstrapError(
                f"ambiguous {label} devices: {matches}; refusing to mount"
            )
        if time.monotonic() >= deadline:
            raise BootstrapError(
                f"no filesystem labelled {label!r} after {timeout_s}s "
                f"(seen={last_seen})"
            )
        time.sleep(poll_s)


def mount_persistent_data(
    *,
    require: bool = True,
    mountpoint: str = DEFAULT_MOUNTPOINT,
    volatile_ok: bool = False,
) -> str:
    """Mount the labelled data volume or fail if persistence is required.

    A volatile fallback is unmistakable: the returned path is never
    advertised as durable when ``require`` is true and the volume is
    missing. ``volatile_ok`` is an explicit operator opt-in.
    """
    try:
        dev = discover_labeled_partition()
    except BootstrapError as exc:
        if require and not volatile_ok:
            raise BootstrapError(
                f"persistence required but unavailable: {exc}"
            ) from exc
        log.error("NOVA persistence unavailable: %s", exc)
        vol = "/tmp/nova_data_volatile"
        os.makedirs(vol, exist_ok=True)
        log.error(
            "VOLATILE MODE: data will not survive reboot. "
            "Set NOVA_REQUIRE_PERSISTENCE=0 only as an explicit opt-in."
        )
        return vol
    os.makedirs(mountpoint, exist_ok=True)
    linux_mount(dev, mountpoint, "ext4", "rw,noatime")
    return mountpoint


def linux_reboot(halt: bool = False) -> None:
    """Request reboot or halt via the reboot syscall; never a missing binary."""
    if sys.platform != "linux":
        raise BootstrapError(f"linux_reboot is not available on {sys.platform}")
    import ctypes

    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    # LINUX_REBOOT_MAGIC1 / MAGIC2 / CMD_RESTART or POWER_OFF
    LINUX_REBOOT_MAGIC1 = 0xFEE1DEAD
    LINUX_REBOOT_MAGIC2 = 672274793
    cmd = 0x4321FEDC if halt else 0x01234567  # POWER_OFF / RESTART
    rc = libc.reboot(LINUX_REBOOT_MAGIC1, LINUX_REBOOT_MAGIC2, cmd, None)
    if rc != 0:
        err = ctypes.get_errno()
        raise BootstrapError(f"reboot syscall failed errno={err}")
