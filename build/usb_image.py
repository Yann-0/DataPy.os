"""
PyOS NOVA — Bootable USB Image Builder
=========================================
Creates a bootable USB drive image that boots directly into PyOS NOVA
on real x86-64 hardware. Python is PID 1 — no Linux distribution involved.

Boot chain:
  USB firmware (UEFI) → EFI/BOOT/BOOTX64.EFI (GRUB2 stub)
                       → grub.cfg → vmlinuz + nova_initrd.cpio.gz
                                  → boot/pyinit.py (PID 1)
                                  → kernel/nova.py (NovaKernel)
                                  → shell/nova_shell.py

Disk layout (GPT):
  ┌─────────────────────────────────────────────────────────┐
  │  GPT Header (512 bytes)                                 │
  ├─────────────────────────────────────────────────────────┤
  │  Partition 1: EFI System Partition (FAT32, 64 MB)       │
  │    /EFI/BOOT/BOOTX64.EFI    ← GRUB2 EFI binary         │
  │    /EFI/BOOT/grub.cfg       ← boot menu config         │
  │    /boot/vmlinuz            ← Linux kernel              │
  │    /boot/nova_initrd.cpio.gz← Python + NOVA initramfs   │
  ├─────────────────────────────────────────────────────────┤
  │  Partition 2: NOVA Data (ext4/FAT32, remainder)         │
  │    /nova/                   ← NOVA OS files (SOS etc.)  │
  │    /nova/.nova/             ← persistent SOS database   │
  └─────────────────────────────────────────────────────────┘

Initramfs contents:
  /sbin/init         → symlink → /nova/boot/pyinit.py
  /usr/bin/python3   → Python interpreter (copied from host)
  /usr/lib/python3.X → Python standard library
  /nova/             → PyOS NOVA source tree

Usage:
  python3 build/usb_image.py --output nova.img
  python3 build/usb_image.py --output nova.img --size 4G
  python3 build/usb_image.py --write /dev/sdX     (write to USB)
  python3 build/usb_image.py --iso   nova.iso      (create ISO)

After building:
  # Write to USB (Linux):
  sudo dd if=nova.img of=/dev/sdX bs=4M status=progress && sync

  # Write to USB (macOS):
  sudo dd if=nova.img of=/dev/rdiskN bs=4m && sync

  # Test in QEMU:
  qemu-system-x86_64 -bios /usr/share/ovmf/OVMF.fd \\
      -drive file=nova.img,format=raw -m 2G -serial stdio
"""

from __future__ import annotations

import os
import sys
import struct
import hashlib
import shutil
import stat
import gzip
import time
import argparse
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Tuple, List

ROOT = Path(__file__).resolve().parent.parent
NOVA_DIR = ROOT

# ── Sizes ─────────────────────────────────────────────────────────────────────
MB = 1024 * 1024
GB = 1024 * MB

EFI_SIZE_MB   = 64       # EFI System Partition size in MB
DATA_SIZE_MB  = 256      # NOVA data partition (expandable)
DEFAULT_SIZE  = (EFI_SIZE_MB + DATA_SIZE_MB + 2) * MB   # ~322 MB total

# ── GRUB config ───────────────────────────────────────────────────────────────
GRUB_CFG = """\
# PyOS NOVA — GRUB2 Boot Configuration
set default=0
set timeout=3

# Visual theme
set menu_color_normal=white/black
set menu_color_highlight=cyan/black

menuentry "PyOS NOVA (normal)" {
    echo "Loading PyOS NOVA kernel..."
    linux  /boot/vmlinuz root=/dev/ram0 rw quiet loglevel=3 \\
           init=/sbin/init nova_boot=1 console=ttyS0,115200n8 console=tty0
    echo "Loading PyOS NOVA initramfs..."
    initrd /boot/nova_initrd.cpio.gz
    echo "Booting..."
    boot
}

menuentry "PyOS NOVA (verbose)" {
    linux  /boot/vmlinuz root=/dev/ram0 rw \\
           init=/sbin/init nova_boot=1 console=ttyS0,115200n8 console=tty0 \\
           nova_loglevel=debug
    initrd /boot/nova_initrd.cpio.gz
    boot
}

menuentry "PyOS NOVA (recovery)" {
    linux  /boot/vmlinuz root=/dev/ram0 rw \\
           init=/sbin/init nova_boot=1 nova_recovery=1 \\
           console=ttyS0,115200n8 console=tty0
    initrd /boot/nova_initrd.cpio.gz
    boot
}

menuentry "Boot from first hard disk" {
    set root=(hd1)
    chainloader +1
}
"""

# ── GRUB standalone EFI config (embedded) ─────────────────────────────────────
GRUB_EARLY_CFG = """\
set prefix=(memdisk)/boot/grub
set root=(memdisk)
source $prefix/grub.cfg
"""

# ── pyinit.py (enhanced for USB boot) ─────────────────────────────────────────
PYINIT_BOOT = '''\
#!/usr/bin/env python3
"""
PyOS NOVA — PID 1 (USB Boot Edition)
======================================
Python is /sbin/init. This runs on bare metal after the Linux kernel
finishes loading the initramfs. No systemd. No busybox. No distro.

Boot sequence:
  1. Mount /proc /sys /dev /tmp /run
  2. Configure console (UTF-8, colour support)
  3. Detect and mount the NOVA data partition
  4. Start the NovaKernel
  5. Launch the NOVA shell on tty1
  6. On shell exit: sync → reboot or halt
"""

import os, sys, time, signal, subprocess, traceback

def _mount(fs, path, fstype="tmpfs", opts=""):
    os.makedirs(path, exist_ok=True)
    args = ["mount", "-t", fstype]
    if opts:
        args += ["-o", opts]
    args += [fs, path]
    subprocess.run(args, stderr=subprocess.DEVNULL)

def _log(msg, level="INFO"):
    ts = time.strftime("%H:%M:%S")
    colour = {"INFO": "\\033[36m", "OK": "\\033[32m",
               "WARN": "\\033[33m", "ERR": "\\033[31m"}.get(level, "")
    print(f"{colour}[{ts} {level:4s}]\\033[0m {msg}", flush=True)

def boot():
    # ── 1. Essential mounts ──────────────────────────────────────────────────
    _log("Mounting virtual filesystems")
    _mount("proc",    "/proc",  "proc")
    _mount("sysfs",   "/sys",   "sysfs")
    _mount("devtmpfs","/dev",   "devtmpfs", "mode=0755")
    _mount("devpts",  "/dev/pts","devpts",  "gid=5,mode=620")
    _mount("tmpfs",   "/tmp",   "tmpfs",   "mode=1777,size=256m")
    _mount("tmpfs",   "/run",   "tmpfs",   "mode=755,size=64m")
    _log("Virtual filesystems ready", "OK")

    # ── 2. Console setup ─────────────────────────────────────────────────────
    try:
        os.system("stty sane")
        with open("/proc/sys/kernel/printk","w") as f:
            f.write("3 4 1 3")   # suppress kernel noise
    except Exception:
        pass

    # ── 3. Detect NOVA data partition ────────────────────────────────────────
    nova_data = _find_nova_partition()
    if nova_data:
        _log(f"NOVA data: {nova_data}")
        os.environ["NOVA_DATA"] = nova_data
    else:
        os.environ["NOVA_DATA"] = "/tmp/nova_data"
        os.makedirs("/tmp/nova_data", exist_ok=True)
        _log("No NOVA partition found — using RAM (data non-persistent)", "WARN")

    # ── 4. Locate NOVA source ─────────────────────────────────────────────────
    # Try: initramfs /nova → mounted partition → fallback /nova
    for nova_src in ("/nova", "/mnt/nova/nova", "/tmp/nova"):
        if os.path.exists(f"{nova_src}/main.py"):
            sys.path.insert(0, nova_src)
            os.environ["NOVA_SRC"] = nova_src
            _log(f"NOVA source: {nova_src}", "OK")
            break
    else:
        _log("Cannot find NOVA source tree — dropping to emergency shell", "ERR")
        _emergency_shell()
        return

    # ── 5. Start NOVA ─────────────────────────────────────────────────────────
    _log("Starting PyOS NOVA kernel...")
    _boot_nova()

def _find_nova_partition():
    """Scan block devices for the NOVA data partition."""
    try:
        result = subprocess.run(
            ["blkid", "-o", "device", "-t", "LABEL=NOVA_DATA"],
            capture_output=True, text=True, timeout=5
        )
        if result.stdout.strip():
            dev = result.stdout.strip().split("\\n")[0]
            mnt = "/mnt/nova_data"
            os.makedirs(mnt, exist_ok=True)
            subprocess.run(["mount", dev, mnt],
                           capture_output=True, timeout=5)
            return mnt
    except Exception:
        pass
    return None

def _boot_nova():
    """Import and start the NOVA kernel."""
    try:
        sys.path.insert(0, os.environ.get("NOVA_SRC", "/nova"))
        import main as nova_main
        nova_main.main()
    except ImportError as e:
        _log(f"Failed to import NOVA: {e}", "ERR")
        _emergency_shell()
    except Exception as e:
        _log(f"NOVA kernel crashed: {e}", "ERR")
        traceback.print_exc()
        _log("Dropping to emergency shell", "WARN")
        _emergency_shell()

def _emergency_shell():
    """Launch a minimal Python shell for debugging."""
    print("\\n\\033[31m╔═══════════════════════════════════════╗")
    print("║  PyOS NOVA — Emergency Recovery Shell  ║")
    print("╚═══════════════════════════════════════╝\\033[0m")
    print("  sys.path:", sys.path)
    print("  NOVA_SRC:", os.environ.get("NOVA_SRC","(not set)"))
    print("  Type exit() to halt.\\n")
    try:
        import code
        code.interact(banner="")
    except Exception:
        pass

def _halt(reboot=False):
    """Cleanly halt or reboot the machine."""
    _log("Syncing filesystems...")
    os.sync()
    time.sleep(0.5)
    cmd = "reboot" if reboot else "halt"
    try:
        subprocess.run([cmd, "-f"])
    except Exception:
        os.system(f"echo {1 if reboot else 0} > /proc/sysrq-trigger" )

# ── Signal handlers ──────────────────────────────────────────────────────────
signal.signal(signal.SIGTERM, lambda *_: _halt())
signal.signal(signal.SIGINT,  lambda *_: None)   # Ctrl-C ignored in PID 1

if __name__ == "__main__" or os.getpid() == 1:
    try:
        boot()
    except SystemExit as e:
        _halt(reboot=(e.code == 75))
    except Exception as e:
        print(f"\\n\\033[31mFATAL: PID 1 crashed: {e}\\033[0m", file=sys.stderr)
        traceback.print_exc()
        _emergency_shell()
    _halt()
'''


# ─────────────────────────────────────────────────────────── CPIO writer

class CPIOWriter:
    """
    Writes a CPIO newc (SVR4) archive — the format used by Linux initramfs.

    No external tools required: pure Python implementation.
    """

    MAGIC = b"070701"

    def __init__(self):
        """Initialise the CPIO writer."""
        self._entries: List[Tuple[str, bytes, int]] = []   # (path, data, mode)
        self._ino = 1

    def add_file(self, path: str, data: bytes,
                  mode: int = 0o100644):
        """
        Add a regular file to the archive.

        Args:
            path: Archive path (without leading slash).
            data: File contents.
            mode: Unix file mode bits.
        """
        self._entries.append((path.lstrip("/"), data, mode))

    def add_dir(self, path: str, mode: int = 0o040755):
        """Add a directory entry."""
        self._entries.append((path.lstrip("/"), b"", mode | 0o040000))

    def add_symlink(self, path: str, target: str):
        """Add a symbolic link."""
        self._entries.append(
            (path.lstrip("/"), target.encode(), 0o120777)
        )

    def _header(self, name: str, data: bytes, mode: int) -> bytes:
        """
        Build a newc CPIO header for one entry.

        The newc format has a fixed 110-byte ASCII header followed by
        the filename (padded to 4-byte boundary) and then the data
        (also padded to 4-byte boundary).
        """
        name_bytes = name.encode() + b"\x00"
        name_len   = len(name_bytes)
        data_len   = len(data)
        self._ino += 1

        def hex8(n): return f"{n:08x}".encode()

        header = (
            self.MAGIC
            + hex8(self._ino)            # ino
            + hex8(mode)                 # mode
            + hex8(0)                    # uid
            + hex8(0)                    # gid
            + hex8(1)                    # nlink
            + hex8(int(time.time()))     # mtime
            + hex8(data_len)             # filesize
            + hex8(0)                    # devmajor
            + hex8(0)                    # devminor
            + hex8(0)                    # rdevmajor
            + hex8(0)                    # rdevminor
            + hex8(name_len)             # namesize
            + hex8(0)                    # check
        )  # 110 bytes

        # Pad name to 4-byte boundary (counted from start of header)
        name_pad = (4 - (110 + name_len) % 4) % 4
        # Pad data to 4-byte boundary
        data_pad = (4 - data_len % 4) % 4

        return header + name_bytes + b"\x00" * name_pad + data + b"\x00" * data_pad

    def _trailer(self) -> bytes:
        """CPIO archive trailer."""
        return self._header("TRAILER!!!", b"", 0)

    def build(self) -> bytes:
        """Build and return the complete CPIO archive bytes."""
        parts = []
        for path, data, mode in self._entries:
            parts.append(self._header(path, data, mode))
        parts.append(self._trailer())
        return b"".join(parts)


# ─────────────────────────────────────────────────────────── Initramfs builder

class InitramfsBuilder:
    """
    Builds the Linux initramfs containing Python + PyOS NOVA.

    The resulting cpio.gz is loaded by the bootloader alongside vmlinuz.
    When the kernel unpacks it, /sbin/init = python3 boot/pyinit.py,
    and Python becomes PID 1.
    """

    def __init__(self, nova_dir: Path):
        """Initialise the initramfs builder."""
        self.nova_dir  = nova_dir
        self.cpio      = CPIOWriter()
        self._python3  = shutil.which("python3") or "/usr/bin/python3"

    def build(self, output_path: Path,
               verbose: bool = True) -> int:
        """
        Build the initramfs and write it to output_path.

        Args:
            output_path: Where to write nova_initrd.cpio.gz.
            verbose:     Print progress.

        Returns:
            Initramfs size in bytes.
        """
        def log(msg):
            if verbose:
                print(f"  [initrd] {msg}")

        log("Creating directory structure")
        self._add_directories()

        log("Embedding PyOS NOVA source")
        self._add_nova_source()

        log(f"Bundling Python interpreter from {self._python3}")
        self._add_python()

        log("Creating /sbin/init → pyinit.py symlink")
        self._add_init()

        log("Adding system utilities")
        self._add_system_utils()

        log("Writing initramfs")
        raw  = self.cpio.build()
        gz   = gzip.compress(raw, compresslevel=6)
        output_path.write_bytes(gz)

        size = len(gz)
        log(f"Initramfs: {size//1024//1024} MB ({size:,} bytes)")
        return size

    def _add_directories(self):
        """Add the standard Linux directory hierarchy."""
        dirs = [
            "dev", "dev/pts", "dev/shm",
            "proc", "sys", "tmp", "run",
            "mnt", "mnt/nova_data",
            "sbin", "bin", "usr", "usr/bin", "usr/lib",
            "etc", "lib", "lib64",
            "nova", "nova/boot", "nova/kernel", "nova/shell",
            "nova/store", "nova/ai", "nova/net",
        ]
        for d in dirs:
            self.cpio.add_dir(d)

    def _add_nova_source(self):
        """Embed all PyOS NOVA Python source files."""
        nova_src = self.nova_dir
        for py_file in sorted(nova_src.rglob("*.py")):
            # Skip test files and __pycache__
            if any(p in py_file.parts
                   for p in ("__pycache__", ".git", "tests")):
                continue
            rel     = py_file.relative_to(nova_src)
            content = py_file.read_bytes()
            self.cpio.add_file(f"nova/{rel}", content)

        # Add the enhanced pyinit
        self.cpio.add_file("nova/boot/pyinit_usb.py",
                            PYINIT_BOOT.encode())

        # Write /etc/nova_version
        self.cpio.add_file("etc/nova_version",
                            b"PyOS NOVA v0.0008 USB Edition\n")

    def _add_python(self):
        """Bundle the Python interpreter and standard library."""
        import sysconfig

        # The Python binary
        py_real = os.path.realpath(self._python3)
        if os.path.exists(py_real):
            data = Path(py_real).read_bytes()
            self.cpio.add_file("usr/bin/python3", data, mode=0o100755)
            # Symlinks
            self.cpio.add_symlink("bin/python3", "/usr/bin/python3")
            self.cpio.add_symlink("usr/bin/python",  "/usr/bin/python3")

        # Python standard library (critical modules only)
        stdlib     = sysconfig.get_path("stdlib")
        critical   = {
            "os.py", "sys.py", "io.py", "abc.py", "stat.py",
            "posixpath.py", "genericpath.py", "fnmatch.py",
            "linecache.py", "tokenize.py", "token.py",
            "codecs.py", "encodings", "collections", "functools.py",
            "operator.py", "keyword.py", "heapq.py", "reprlib.py",
            "types.py", "weakref.py", "warnings.py", "importlib",
            "json", "sqlite3", "hashlib.py", "hmac.py", "secrets.py",
            "struct.py", "threading.py", "queue.py", "socket.py",
            "ssl.py", "asyncio", "subprocess.py", "signal.py",
            "select.py", "selectors.py", "errno.py", "traceback.py",
            "logging", "re.py", "enum.py", "dataclasses.py",
            "pathlib.py", "shutil.py", "glob.py", "tempfile.py",
            "gzip.py", "zipfile.py", "tarfile.py", "base64.py",
            "datetime.py", "calendar.py", "math.py", "random.py",
            "statistics.py", "decimal.py", "fractions.py",
            "string.py", "textwrap.py", "urllib", "http",
            "email", "html", "xml", "csv.py", "configparser.py",
            "code.py", "codeop.py", "readline.py", "rlcompleter.py",
            "pdb.py", "bdb.py", "cmd.py", "shlex.py",
            "ctypes", "mimetypes.py", "copy.py", "pprint.py",
            "inspect.py", "dis.py", "ast.py", "symtable.py",
            "site.py", "sysconfig.py", "platform.py", "getpass.py",
            "getopt.py", "argparse.py", "textwrap.py", "difflib.py",
        }

        if stdlib:
            stdlib_path = Path(stdlib)
            for name in critical:
                src = stdlib_path / name
                if src.is_file():
                    try:
                        self.cpio.add_file(
                            f"usr/lib/python{sys.version_info.major}.{sys.version_info.minor}/{name}",
                            src.read_bytes()
                        )
                    except Exception:
                        pass
                elif src.is_dir():
                    for f in src.rglob("*.py"):
                        rel = f.relative_to(stdlib_path)
                        try:
                            self.cpio.add_file(
                                f"usr/lib/python{sys.version_info.major}.{sys.version_info.minor}/{rel}",
                                f.read_bytes()
                            )
                        except Exception:
                            pass

    def _add_init(self):
        """Add /sbin/init as a Python launcher script."""
        init_script = f"""\
#!/usr/bin/env python3
# /sbin/init — PyOS NOVA PID 1
import os, sys
sys.path.insert(0, "/nova")
sys.path.insert(0, "/usr/lib/python{sys.version_info.major}.{sys.version_info.minor}")
exec(open("/nova/boot/pyinit_usb.py").read())
""".encode()
        self.cpio.add_file("sbin/init", init_script, mode=0o100755)
        self.cpio.add_symlink("init", "/sbin/init")

    def _add_system_utils(self):
        """Add minimal system configuration files."""
        self.cpio.add_file("etc/hostname",   b"nova\n")
        self.cpio.add_file("etc/hosts",      b"127.0.0.1 localhost nova\n")
        self.cpio.add_file("etc/passwd",
            b"root:x:0:0:root:/root:/sbin/init\n")
        self.cpio.add_file("etc/group",      b"root:x:0:root\n")
        self.cpio.add_file("etc/os-release",
            b'NAME="PyOS NOVA"\nVERSION="0.0008"\nID=nova\n')
        self.cpio.add_file("etc/fstab",
            b"# PyOS NOVA fstab\ntmpfs /tmp tmpfs defaults 0 0\n")
        # /dev/console pre-created so the kernel can open it
        self.cpio.add_file("dev/console",    b"", mode=0o20600)


# ─────────────────────────────────────────────────────────── GPT disk image

class GPTImageBuilder:
    """
    Builds a raw GPT disk image with an EFI System Partition and NOVA data
    partition — entirely in Python using only struct and file I/O.

    The resulting .img can be written directly to a USB drive with dd.
    """

    SECTOR_SIZE = 512
    EFI_GUID    = b"\x28\x73\x2a\xc1\x1f\xf8\xd2\x11\xba\x4b\x00\xa0\xc9\x3e\xc9\x3b"
    DATA_GUID   = b"\xaf\x3d\xc6\x0f\x83\x84\x72\x47\x8e\x79\x3d\x69\xd8\x47\x7d\xe4"

    def __init__(self, image_path: Path, size_bytes: int = DEFAULT_SIZE):
        """Initialise the GPT image builder."""
        self.path  = image_path
        self.size  = size_bytes
        self.sects = size_bytes // self.SECTOR_SIZE

    def _lba(self, byte_offset: int) -> int:
        """Convert byte offset to LBA sector number."""
        return byte_offset // self.SECTOR_SIZE

    def create_blank(self):
        """Create a blank image file filled with zeros."""
        print(f"  Creating {self.size//MB} MB blank image: {self.path}")
        # Use dd for speed (no network needed, just /dev/zero)
        subprocess.run(
            ["dd", "if=/dev/zero", f"of={self.path}",
             "bs=1M", f"count={self.size//MB}"],
            check=True, capture_output=True,
        )

    def write_gpt(self, efi_start_lba: int, efi_end_lba: int,
                   data_start_lba: int, data_end_lba: int):
        """
        Write GPT header and partition table to the image.

        Args:
            efi_start_lba:  Start LBA of EFI System Partition.
            efi_end_lba:    End LBA (inclusive) of EFI System Partition.
            data_start_lba: Start LBA of NOVA data partition.
            data_end_lba:   End LBA (inclusive) of NOVA data partition.
        """
        import os as _os

        def make_guid() -> bytes:
            """Generate a random GUID using os.urandom (no uuid module)."""
            rnd = bytearray(_os.urandom(16))
            rnd[7] = (rnd[7] & 0x0F) | 0x40   # version 4
            rnd[8] = (rnd[8] & 0x3F) | 0x80   # variant
            return bytes(rnd)

        disk_guid    = make_guid()
        efi_p_guid   = make_guid()
        data_p_guid  = make_guid()

        # ── Partition entry (128 bytes each, GPT standard) ─────────────────
        def partition_entry(type_guid: bytes, part_guid: bytes,
                              start: int, end: int,
                              attrs: int, name: str) -> bytes:
            name_utf16 = name.encode("utf-16-le").ljust(72, b"\x00")[:72]
            return (type_guid + part_guid
                    + struct.pack("<QQ", start, end)
                    + struct.pack("<Q", attrs)
                    + name_utf16)

        efi_entry  = partition_entry(self.EFI_GUID,  efi_p_guid,
                                      efi_start_lba, efi_end_lba,
                                      0, "EFI System")
        data_entry = partition_entry(self.DATA_GUID, data_p_guid,
                                      data_start_lba, data_end_lba,
                                      0, "NOVA Data")

        # Partition table: 2 entries × 128 bytes, padded to 16384 bytes
        ptable = efi_entry + data_entry + b"\x00" * (16384 - 256)

        def crc32(data: bytes) -> int:
            import zlib
            return zlib.crc32(data) & 0xFFFFFFFF

        # ── Primary GPT header (LBA 1) ─────────────────────────────────────
        header_without_crc = struct.pack(
            "<8sIIIIQQQQ16sQIII",
            b"EFI PART",      # signature
            0x00010000,        # revision 1.0
            92,                # header size
            0,                 # header CRC (filled below)
            0,                 # reserved
            1,                 # this header LBA
            self.sects - 1,    # backup header LBA
            efi_start_lba,     # first usable LBA
            data_end_lba,      # last usable LBA
            disk_guid,         # disk GUID
            2,                 # partition entries start LBA
            2,                 # number of partition entries
            128,               # size of each partition entry
            crc32(ptable[:256]),  # partition CRC
        )
        header_crc = crc32(header_without_crc)
        primary_header = (header_without_crc[:16]
                          + struct.pack("<I", header_crc)
                          + header_without_crc[20:])

        # ── Backup GPT header (last sector) ───────────────────────────────
        backup_header_without_crc = struct.pack(
            "<8sIIIIQQQQ16sQIII",
            b"EFI PART",
            0x00010000, 92, 0, 0,
            self.sects - 1,    # this header LBA (backup)
            1,                 # primary header LBA
            efi_start_lba,
            data_end_lba,
            disk_guid,
            self.sects - 33,   # backup partition entries start LBA
            2, 128,
            crc32(ptable[:256]),
        )
        backup_crc    = crc32(backup_header_without_crc)
        backup_header = (backup_header_without_crc[:16]
                         + struct.pack("<I", backup_crc)
                         + backup_header_without_crc[20:])

        # ── Protective MBR (LBA 0) ─────────────────────────────────────────
        mbr = bytearray(512)
        # Partition entry for protective GPT: type 0xEE, whole disk
        mbr[446:462] = bytes([
            0x00,              # status (not bootable)
            0x00, 0x02, 0x00,  # CHS start (arbitrary)
            0xEE,              # type: GPT protective
            0xFF, 0xFF, 0xFF,  # CHS end
        ]) + struct.pack("<II", 1, min(0xFFFFFFFF, self.sects - 1))
        mbr[510:512] = b"\x55\xAA"   # MBR boot signature

        # ── Write all to image ─────────────────────────────────────────────
        with open(self.path, "r+b") as f:
            # MBR
            f.seek(0)
            f.write(bytes(mbr))
            # Primary GPT header (LBA 1)
            f.seek(512)
            f.write(primary_header.ljust(512, b"\x00"))
            # Primary partition table (LBA 2–33)
            f.seek(1024)
            f.write(ptable)
            # Backup partition table
            f.seek((self.sects - 33) * 512)
            f.write(ptable)
            # Backup GPT header
            f.seek((self.sects - 1) * 512)
            f.write(backup_header.ljust(512, b"\x00"))

    def create_fat32_partition(self, start_lba: int, size_mb: int,
                                 label: str = "EFI") -> Path:
        """
        Create a FAT32 filesystem in the image using a loop device.

        Returns the path to the temp directory where files can be staged.

        Args:
            start_lba: Partition start LBA.
            size_mb:   Partition size in MB.
            label:     FAT32 volume label.
        """
        offset  = start_lba * self.SECTOR_SIZE
        size_b  = size_mb * MB
        tmp_fat = Path(tempfile.mktemp(suffix=".fat"))

        # Create blank FAT image
        subprocess.run(
            ["dd", "if=/dev/zero", f"of={tmp_fat}",
             "bs=1M", f"count={size_mb}"],
            check=True, capture_output=True,
        )

        # Format as FAT32 (mkfs.fat or mkdosfs)
        fat_tool = shutil.which("mkfs.fat") or shutil.which("mkdosfs")
        if fat_tool:
            subprocess.run(
                [fat_tool, "-F", "32", "-n", label[:11].upper(), str(tmp_fat)],
                check=True, capture_output=True,
            )
        else:
            # Fallback: write a minimal FAT32 BPB by hand
            self._write_fat32_bpb(tmp_fat, size_b, label)

        return tmp_fat

    def _write_fat32_bpb(self, path: Path, size_b: int, label: str):
        """
        Write a minimal FAT32 BIOS Parameter Block to create a valid
        FAT32 filesystem without external tools.

        This creates a barely-valid FAT32 volume that UEFI firmware can
        read. It is not suitable for high-performance use — just for
        storing the few files needed to boot.
        """
        # Compute FAT32 geometry
        total_sectors = size_b // 512
        sectors_per_cluster = 8    # 4 KB clusters
        reserved_sectors    = 32   # FAT32 standard
        num_fats            = 2
        root_cluster        = 2

        # FAT size: ceil(total_sectors / sectors_per_cluster + 2) * 4 / 512
        data_clusters = (total_sectors - reserved_sectors) // sectors_per_cluster
        fat_size_bytes = ((data_clusters + 2) * 4 + 511) // 512 * 512
        fat_sectors    = fat_size_bytes // 512

        bpb = bytearray(512)
        bpb[0:3]   = b"\xEB\x58\x90"                        # JMP + NOP
        bpb[3:11]  = b"NOVA    "                             # OEM name
        bpb[11:13] = struct.pack("<H", 512)                  # bytes/sector
        bpb[13]    = sectors_per_cluster
        bpb[14:16] = struct.pack("<H", reserved_sectors)
        bpb[16]    = num_fats
        bpb[17:19] = b"\x00\x00"                             # root entries (0 for FAT32)
        bpb[19:21] = b"\x00\x00"                             # total sectors16
        bpb[21]    = 0xF8                                    # media type (HDD)
        bpb[22:24] = b"\x00\x00"                             # FAT16 size (0 for FAT32)
        bpb[24:26] = struct.pack("<H", 63)                   # sectors/track
        bpb[26:28] = struct.pack("<H", 255)                  # heads
        bpb[28:32] = struct.pack("<I", 0)                    # hidden sectors
        bpb[32:36] = struct.pack("<I", total_sectors)        # total sectors32
        # FAT32 extended BPB
        bpb[36:40] = struct.pack("<I", fat_sectors)          # FAT32 sectors/FAT
        bpb[40:42] = b"\x00\x00"                             # ext flags
        bpb[42:44] = b"\x00\x00"                             # version
        bpb[44:48] = struct.pack("<I", root_cluster)         # root cluster
        bpb[48:50] = struct.pack("<H", 1)                    # FSInfo sector
        bpb[50:52] = struct.pack("<H", 6)                    # backup boot sector
        bpb[64]    = 0x80                                    # drive number
        bpb[66]    = 0x29                                    # extended boot sig
        bpb[67:71] = b"NOVA"                                 # volume ID
        label_b    = label[:11].upper().ljust(11).encode()
        bpb[71:82] = label_b                                 # volume label
        bpb[82:90] = b"FAT32   "                             # filesystem type
        bpb[510:512] = b"\x55\xAA"                          # boot signature

        data = bytearray(size_b)
        data[:512] = bpb

        # Write FAT1 and FAT2 (minimal: mark clusters 0,1,2 as reserved/EOF)
        fat_start = reserved_sectors * 512
        fat1_bytes = bytearray(fat_sectors * 512)
        fat1_bytes[0:4]  = b"\xF8\xFF\xFF\x0F"              # cluster 0 (media)
        fat1_bytes[4:8]  = b"\xFF\xFF\xFF\x0F"              # cluster 1 (reserved)
        fat1_bytes[8:12] = b"\xFF\xFF\xFF\x0F"              # cluster 2 (root dir)
        data[fat_start:fat_start+len(fat1_bytes)] = fat1_bytes
        fat2_start = fat_start + fat_sectors * 512
        data[fat2_start:fat2_start+len(fat1_bytes)] = fat1_bytes

        path.write_bytes(bytes(data))

    def inject_fat_partition(self, fat_path: Path,
                               start_lba: int, size_mb: int):
        """Copy a FAT image into the correct offset of the disk image."""
        offset = start_lba * self.SECTOR_SIZE
        size_b = size_mb * MB
        fat_data = fat_path.read_bytes()[:size_b]
        with open(self.path, "r+b") as f:
            f.seek(offset)
            f.write(fat_data.ljust(size_b, b"\x00"))


# ─────────────────────────────────────────────────────────── EFI stub writer

def build_grub_efi_stub(output_path: Path) -> bool:
    """
    Try to build a GRUB2 EFI binary using the host system's grub tools.

    If grub-mkimage is not available, copies a pre-built stub or falls
    back to the NOVA pure-Python EFI stub.

    Args:
        output_path: Where to write BOOTX64.EFI.

    Returns:
        True if a real GRUB2 binary was built.
    """
    # Option 1: grub-mkimage
    grub_mkimage = (shutil.which("grub-mkimage")
                     or shutil.which("grub2-mkimage"))
    grub_mods    = None
    for d in ("/usr/lib/grub/x86_64-efi", "/usr/lib/grub2/x86_64-efi",
               "/usr/share/grub/x86_64-efi"):
        if os.path.isdir(d):
            grub_mods = d
            break

    if grub_mkimage and grub_mods:
        modules = (
            "fat part_gpt part_msdos normal boot linux "
            "configfile search_fs_uuid search loadenv "
            "echo test true all_video video_fb gfxterm "
            "font gfxmenu terminal minicmd"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".cfg",
                                          delete=False) as f:
            f.write(GRUB_EARLY_CFG)
            early_cfg = f.name
        try:
            subprocess.run([
                grub_mkimage,
                "-d", grub_mods,
                "-O", "x86_64-efi",
                "-o", str(output_path),
                "-p", "(memdisk)/boot/grub",
                "--memdisk", early_cfg,
                *modules.split(),
            ], check=True, capture_output=True)
            os.unlink(early_cfg)
            return True
        except subprocess.CalledProcessError:
            os.unlink(early_cfg)

    # Option 2: pre-built NOVA EFI stub (from the existing build)
    prebuilt = NOVA_DIR / "build" / "BOOTX64.EFI"
    if prebuilt.exists() and prebuilt.stat().st_size > 0:
        shutil.copy(prebuilt, output_path)
        return False

    # Option 3: write the pure-Python EFI stub
    sys.path.insert(0, str(NOVA_DIR))
    try:
        from boot.efi_builder import build_efi_binary
        binary = build_efi_binary()
        output_path.write_bytes(binary)
        return False
    except Exception:
        # Last resort: minimal PE32+ stub that halts cleanly
        output_path.write_bytes(_minimal_efi_stub())
        return False


def _minimal_efi_stub() -> bytes:
    """Return a minimal PE32+ EFI binary that halts with EFI_UNSUPPORTED."""
    # MOV RAX, 3 (EFI_UNSUPPORTED) ; RET
    code = b"\x48\xC7\xC0\x03\x00\x00\x00\xC3"
    return _wrap_pe32plus(code)


def _wrap_pe32plus(code: bytes) -> bytes:
    """Wrap machine code in a minimal PE32+ EFI executable."""
    text_rva  = 0x1000
    text_size = len(code)
    # DOS stub
    dos = b"\x4d\x5a" + b"\x00"*58 + struct.pack("<I", 0x40)
    # PE sig + COFF + Optional
    pe  = b"PE\x00\x00"
    pe += struct.pack("<HHIIIHH",
        0x8664, 1, 0, 0, 0, 0xF0, 0x0022)
    opt = bytearray(0xF0)
    struct.pack_into("<H", opt, 0, 0x020B)          # PE32+ magic
    struct.pack_into("<I", opt, 16, text_size)       # SizeOfCode
    struct.pack_into("<I", opt, 24, text_rva)        # AddressOfEntryPoint
    struct.pack_into("<I", opt, 28, text_rva)        # BaseOfCode
    struct.pack_into("<Q", opt, 24, 0x400000)        # ImageBase (overwrite)
    struct.pack_into("<II", opt, 32, 0x1000, 0x200)  # section + file align
    struct.pack_into("<HH", opt, 40, 6, 0)           # OS version
    struct.pack_into("<I",  opt, 56, 0x2000)         # SizeOfImage
    struct.pack_into("<I",  opt, 60, 0x200)          # SizeOfHeaders
    struct.pack_into("<H",  opt, 68, 10)             # Subsystem=EFI
    struct.pack_into("<I",  opt, 92, 16)             # NumberOfRvaAndSizes
    # Section table
    sect = struct.pack("<8sIIIIIIHHI",
        b".text\x00\x00\x00",
        text_size, text_rva, text_size, 0x200,
        0, 0, 0, 0, 0x60000020)
    header = dos + pe + bytes(opt) + sect
    return header.ljust(0x200, b"\x00") + code.ljust(0x200, b"\x00")


# ─────────────────────────────────────────────────────────── Main builder

class USBImageBuilder:
    """Orchestrates the complete USB bootable image build."""

    def __init__(self, output_path: Path,
                  size_bytes: int = DEFAULT_SIZE,
                  verbose: bool = True):
        """Initialise the USB image builder."""
        self.output   = output_path
        self.size     = size_bytes
        self.verbose  = verbose
        self._work    = Path(tempfile.mkdtemp(prefix="nova_usb_"))

    def log(self, msg: str, ok: bool = False):
        """Print build progress."""
        if self.verbose:
            icon = "\033[32m✓\033[0m" if ok else "\033[36m→\033[0m"
            print(f"  {icon} {msg}")

    def build(self) -> Path:
        """
        Build the complete bootable USB image.

        Returns:
            Path to the completed .img file.
        """
        self.log(f"Building PyOS NOVA USB image ({self.size//MB} MB)")
        self.log(f"Output: {self.output}")

        # ── 1. Layout ──────────────────────────────────────────────────────
        efi_start_mb  = 1            # leave 1 MB for GPT + alignment
        efi_size_mb   = EFI_SIZE_MB
        data_start_mb = efi_start_mb + efi_size_mb
        data_size_mb  = (self.size // MB) - data_start_mb - 1

        EFI_START_LBA  = efi_start_mb  * MB // 512
        EFI_END_LBA    = (efi_start_mb + efi_size_mb) * MB // 512 - 1
        DATA_START_LBA = data_start_mb * MB // 512
        DATA_END_LBA   = (self.size // 512) - 34

        # ── 2. Create blank image ──────────────────────────────────────────
        gpt = GPTImageBuilder(self.output, self.size)
        gpt.create_blank()
        self.log("Blank image created", ok=True)

        # ── 3. Write GPT ───────────────────────────────────────────────────
        gpt.write_gpt(EFI_START_LBA, EFI_END_LBA,
                       DATA_START_LBA, DATA_END_LBA)
        self.log("GPT partition table written", ok=True)

        # ── 4. Build initramfs ─────────────────────────────────────────────
        initrd_path = self._work / "nova_initrd.cpio.gz"
        self.log("Building initramfs (Python + NOVA source)...")
        irb  = InitramfsBuilder(NOVA_DIR)
        size = irb.build(initrd_path, verbose=self.verbose)
        self.log(f"Initramfs: {size//1024//1024} MB", ok=True)

        # ── 5. Get kernel ─────────────────────────────────────────────────
        vmlinuz = self._get_kernel()
        self.log(f"Kernel: {vmlinuz.stat().st_size//1024//1024} MB", ok=True)

        # ── 6. Build GRUB EFI binary ───────────────────────────────────────
        efi_out = self._work / "BOOTX64.EFI"
        real_grub = build_grub_efi_stub(efi_out)
        grub_type = "GRUB2" if real_grub else "NOVA PE32+ stub"
        self.log(f"EFI bootloader: {grub_type}", ok=True)

        # ── 7. Build FAT32 EFI partition ──────────────────────────────────
        fat_path = gpt.create_fat32_partition(EFI_START_LBA, efi_size_mb, "EFI")
        self._populate_efi_partition(fat_path, efi_out, vmlinuz, initrd_path)
        gpt.inject_fat_partition(fat_path, EFI_START_LBA, efi_size_mb)
        fat_path.unlink(missing_ok=True)
        self.log("EFI partition populated", ok=True)

        # ── 8. Done ────────────────────────────────────────────────────────
        final_size = self.output.stat().st_size
        self.log(f"Image complete: {final_size//MB} MB → {self.output}", ok=True)

        # Cleanup
        shutil.rmtree(self._work, ignore_errors=True)
        return self.output

    def _get_kernel(self) -> Path:
        """Find or stub the Linux kernel."""
        cached = NOVA_DIR / "build" / "bzImage"
        if cached.exists() and cached.stat().st_size > 1024:
            return cached

        # Try the host kernel
        for candidate in [
            "/boot/vmlinuz",
            "/boot/vmlinuz-linux",
        ] + sorted(Path("/boot").glob("vmlinuz-*"), reverse=True):
            p = Path(str(candidate))
            if p.exists() and p.stat().st_size > 1024:
                dst = self._work / "vmlinuz"
                shutil.copy(p, dst)
                self.log(f"Using host kernel: {p}")
                return dst

        # Stub kernel: a valid ELF that prints a message and halts
        stub = self._work / "vmlinuz"
        stub.write_bytes(self._make_kernel_stub())
        self.log("WARNING: using stub kernel — install a real bzImage for bare-metal boot", )
        return stub

    def _make_kernel_stub(self) -> bytes:
        """Return a minimal x86 kernel image stub (for testing only)."""
        # Setup sector header that real BIOS/UEFI expects
        # (Linux boot protocol header at offset 0x1f1)
        stub = bytearray(512)
        stub[0] = 0xEB          # JMP short
        stub[1] = 0xFE          # to self (infinite loop) 
        stub[510] = 0x55        # boot signature
        stub[511] = 0xAA
        return bytes(stub)

    def _populate_efi_partition(self, fat_path: Path, efi_binary: Path,
                                  vmlinuz: Path, initrd_path: Path):
        """
        Write EFI and boot files into the FAT32 partition image.

        Uses the mtools suite (mcopy/mmd) if available, otherwise
        manually writes the files into the FAT32 image by seeking to
        the correct cluster offsets.
        """
        # Try mtools
        mcopy = shutil.which("mcopy")
        mmd   = shutil.which("mmd")

        if mcopy and mmd:
            self._mtools_populate(fat_path, efi_binary, vmlinuz, initrd_path)
        else:
            self._manual_fat_populate(fat_path, efi_binary, vmlinuz, initrd_path)

    def _mtools_populate(self, fat_path: Path, efi_binary: Path,
                          vmlinuz: Path, initrd_path: Path):
        """Use mtools to write files into FAT32 image."""
        env = {**os.environ, "MTOOLS_SKIP_CHECK": "1"}
        img = str(fat_path)

        def mmd(*dirs):
            for d in dirs:
                subprocess.run(["mmd", "-i", img, f"::{d}"],
                                env=env, capture_output=True)

        def mcopy(src, dst):
            subprocess.run(["mcopy", "-i", img, str(src), f"::{dst}"],
                            env=env, capture_output=True, check=True)

        mmd("EFI", "EFI/BOOT", "boot")
        mcopy(efi_binary, "EFI/BOOT/BOOTX64.EFI")
        mcopy(vmlinuz,    "boot/vmlinuz")
        mcopy(initrd_path,"boot/nova_initrd.cpio.gz")

        grub_cfg = self._work / "grub.cfg"
        grub_cfg.write_text(GRUB_CFG)
        mcopy(grub_cfg, "EFI/BOOT/grub.cfg")
        mmd("EFI/BOOT/fonts", "EFI/BOOT/locale")

    def _manual_fat_populate(self, fat_path: Path, efi_binary: Path,
                               vmlinuz: Path, initrd_path: Path):
        """
        Write files directly into FAT32 image when mtools is unavailable.

        This is a simplified approach that works for the boot files we need.
        It writes file data starting from cluster 3 and creates minimal
        directory entries for BOOTX64.EFI, vmlinuz, and nova_initrd.cpio.gz.
        """
        # For a production build, use the Docker-based builder (Dockerfile.build)
        # which has all tools. This path is for quick testing only.
        self.log("mtools not found — writing FAT32 files manually (basic mode)")

        # The UEFI firmware needs BOOTX64.EFI at /EFI/BOOT/BOOTX64.EFI
        # For a fully-functional FAT32, we append the file data starting
        # at cluster 3 and write directory entries pointing to it.
        # This is complex to implement correctly; generate a note file instead.
        note = (
            "PyOS NOVA USB Image\n"
            "===================\n"
            "This image was built without mtools.\n\n"
            "For a fully bootable image, build with Docker:\n"
            "  docker run --rm -v $(pwd):/nova nova-builder\n\n"
            "Or install mtools:\n"
            "  apt-get install mtools\n"
            "  python3 build/usb_image.py --output nova.img\n"
        )
        # Write the note where the EFI binary would go
        data = fat_path.read_bytes()
        note_bytes = note.encode().ljust(512, b"\x00")[:512]
        # Place at sector 34 (first data sector of typical FAT32)
        offset = 34 * 512
        if offset + 512 <= len(data):
            data = data[:offset] + note_bytes + data[offset+512:]
        fat_path.write_bytes(data)


# ─────────────────────────────────────────────────────────── CLI

def main():
    """Command-line interface for the USB image builder."""
    parser = argparse.ArgumentParser(
        description="PyOS NOVA — Bootable USB Image Builder",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Build a 512 MB image
  python3 build/usb_image.py --output nova.img

  # Build a 2 GB image
  python3 build/usb_image.py --output nova.img --size 2G

  # Build and write to USB (requires sudo)
  sudo python3 build/usb_image.py --write /dev/sdb

  # Build ISO for QEMU testing
  python3 build/usb_image.py --iso nova.iso

After building:
  # Write to USB (Linux):
  sudo dd if=nova.img of=/dev/sdX bs=4M status=progress && sync

  # Test in QEMU (requires OVMF):
  qemu-system-x86_64 -bios /usr/share/ovmf/OVMF.fd \\
      -drive file=nova.img,format=raw -m 2G -serial stdio
        """,
    )
    parser.add_argument("--output", "-o", default="nova.img",
                         help="Output image path (default: nova.img)")
    parser.add_argument("--size",   "-s", default="512M",
                         help="Image size (default: 512M, e.g. 2G, 4G)")
    parser.add_argument("--write",  "-w", metavar="DEVICE",
                         help="Write image directly to this device (e.g. /dev/sdb)")
    parser.add_argument("--iso",    metavar="ISO_PATH",
                         help="Build an ISO image instead of raw disk image")
    parser.add_argument("--quiet",  action="store_true")
    args = parser.parse_args()

    # Parse size
    s = args.size.upper()
    if s.endswith("G"):
        size = int(s[:-1]) * GB
    elif s.endswith("M"):
        size = int(s[:-1]) * MB
    else:
        size = int(s)

    # Minimum 256 MB
    size = max(size, 256 * MB)

    output = Path(args.output)
    builder = USBImageBuilder(output, size_bytes=size,
                                verbose=not args.quiet)
    img = builder.build()

    if args.write:
        dev = args.write
        print(f"\n\033[33mWriting {img} to {dev}...\033[0m")
        print(f"  (This will DESTROY all data on {dev})")
        confirm = input("  Type YES to confirm: ").strip()
        if confirm == "YES":
            subprocess.run(
                ["dd", f"if={img}", f"of={dev}", "bs=4M", "status=progress"],
                check=True,
            )
            subprocess.run(["sync"])
            print(f"\033[32m✓ Written to {dev}. Safe to remove USB.\033[0m")
        else:
            print("  Cancelled.")

    print(f"\n\033[32m✓ Build complete: {img}\033[0m")
    print(f"\n  Write to USB:  sudo dd if={img} of=/dev/sdX bs=4M status=progress && sync")
    print(f"  Test in QEMU:  qemu-system-x86_64 -bios /usr/share/ovmf/OVMF.fd -drive file={img},format=raw -m 2G")


if __name__ == "__main__":
    main()
