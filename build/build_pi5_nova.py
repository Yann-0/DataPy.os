"""
DataPy.os — Raspberry Pi 5 image builder (Linux userspace / PID-1)
===================================================================
Builds an MBR disk image: FAT32 boot + labelled ext4 NOVA_DATA.

This is a host-side assembler. It does not certify a Pi 5 boot. Firmware-
native UEFI is a different, experimental path.

  python build/build_pi5_nova.py --output artifacts/nova_pi5.img

Do not flash without an explicitly confirmed target. Do not guess /dev/sdX.
Do not overwrite an existing NOVA_DATA partition when replacing boot files.
"""

from __future__ import annotations

import os
import sys
import struct
import gzip
import hashlib
import shutil
import tarfile
import tempfile
import urllib.request
import urllib.error
import time
import argparse
import json
from pathlib import Path


# ─── Configuration ────────────────────────────────────────────────────────────

VERSION = "0.0008"
SCRIPT_DIR = Path(__file__).resolve().parent

# Image layout
MB           = 1024 * 1024
BOOT_MB      = 256    # FAT32 boot partition: firmware + kernel + initramfs
DATA_MB      = 512    # NOVA data: persistent SOS database
IMAGE_MB     = BOOT_MB + DATA_MB + 2

# Download sources
FIRMWARE_BASE = (
    "https://raw.githubusercontent.com/raspberrypi/firmware/master/boot"
)

# Pi firmware files required for Pi 5 boot
PI_FIRMWARE_FILES = {
    "start4.elf":            "VideoCore firmware — required to boot",
    "fixup4.dat":            "Memory split config — required to boot",
    "bcm2712-rpi-5-b.dtb":  "Pi 5 hardware device tree",
    "kernel_2712.img":       "Pi 5 optimised Linux kernel (~31 MB)",
}

# Python 3.12.14 ARM64 musl (needs /lib/ld-musl-aarch64.so.1 in initramfs)
PYTHON_ARM64_URL = (
    "https://github.com/astral-sh/python-build-standalone/releases/download"
    "/20260901/cpython-3.12.14+20260901-aarch64-unknown-linux-musl"
    "-install_only_stripped.tar.gz"
)
PYTHON_ARM64_SHA256 = (
    "a0ad6f01b9204eba573a08927097b78143c564f98bea68a41ea1e172f041da3a"
)
MUSL_LOADER_URL = (
    "https://dl-cdn.alpinelinux.org/alpine/v3.20/main/aarch64/musl-1.2.5-r3.apk"
)
MUSL_LOADER_SHA256 = (
    "e455c49c6c3de1dfcd4b9867c35097f588de2fb01a939c77ddb149f8e6086a24"
)
MUSL_LOADER_NAME = "ld-musl-aarch64.so.1"

# ─── Colours ──────────────────────────────────────────────────────────────────

GREEN  = "\033[32m"
CYAN   = "\033[36m"
YELLOW = "\033[33m"
RED    = "\033[31m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

def ok(msg):  print(f"{GREEN}  [ok]{RESET} {msg}")
def log(msg): print(f"{CYAN}  ->{RESET} {msg}")
def warn(msg):print(f"{YELLOW}  [warn]{RESET} {msg}")
def err(msg): print(f"{RED}  [fail]{RESET} {msg}"); sys.exit(1)


# ─── Downloader ───────────────────────────────────────────────────────────────

def download(url: str, dest: Path, desc: str = "",
             sha256: str | None = None) -> Path:
    """
    Download a file with a progress bar.

    Args:
        url:  Full URL to download.
        dest: Destination file path.
        desc: Human-readable description for the progress display.

    Returns:
        Path to the downloaded file.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        if sha256:
            digest = hashlib.sha256(dest.read_bytes()).hexdigest()
            if digest.lower() != sha256.lower():
                dest.unlink()
            else:
                ok(f"Already downloaded: {dest.name}")
                return dest
        else:
            ok(f"Already downloaded: {dest.name}")
            return dest

    label = desc or dest.name
    print(f"  ↓ {label}", end="", flush=True)

    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "PyOS-NOVA-Builder/0.0008",
        })
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            done  = 0
            chunk = 65536

            tmp = dest.with_suffix(".part")
            with open(tmp, "wb") as f:
                while True:
                    data = resp.read(chunk)
                    if not data:
                        break
                    f.write(data)
                    done += len(data)
                    if total:
                        pct = done * 100 // total
                        mb  = done // MB
                        print(f"\r  ↓ {label}: {mb} MB  {pct}%  ", end="", flush=True)

            tmp.rename(dest)

        if sha256:
            digest = hashlib.sha256(dest.read_bytes()).hexdigest()
            if digest.lower() != sha256.lower():
                dest.unlink(missing_ok=True)
                err(f"SHA-256 mismatch for {dest.name}: {digest}")

        size_mb = dest.stat().st_size // MB
        print(f"\r  [ok] {label}: {size_mb} MB            ")
        return dest

    except urllib.error.HTTPError as e:
        print()
        err(f"HTTP {e.code} downloading {url}\n"
            f"    Check your internet connection and try again.")
    except Exception as e:
        print()
        err(f"Download failed: {e}")


def pack_current_source(dest: Path) -> tuple[Path, dict]:
    """Zip the current checkout and record commit / dirty state."""
    import zipfile
    import subprocess as sp

    root = SCRIPT_DIR.parent
    dest.parent.mkdir(parents=True, exist_ok=True)
    meta = {"commit": "unknown", "dirty": True, "tree": "unknown"}
    try:
        commit = sp.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip()
        tree = sp.check_output(
            ["git", "rev-parse", "HEAD^{tree}"], cwd=root, text=True
        ).strip()
        dirty = bool(sp.check_output(
            ["git", "status", "--porcelain"], cwd=root, text=True
        ).strip())
        meta = {"commit": commit, "dirty": dirty, "tree": tree}
    except Exception as exc:
        log(f"git identity unavailable: {exc}")
    skip = {".git", ".venv", "__pycache__", ".pytest_cache", "downloads",
            "dist", ".test-nova-data", "artifacts", ".pytest-tmp"}
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in root.rglob("*"):
            if any(part in skip for part in path.parts):
                continue
            if path.is_file() and path.suffix not in {".img", ".gguf"}:
                zf.write(path, "nova/" + str(path.relative_to(root)).replace("\\", "/"))
        zf.writestr("nova/SOURCE_IDENTITY.json",
                    __import__("json").dumps(meta, indent=2))
    meta["zip_sha256"] = hashlib.sha256(dest.read_bytes()).hexdigest()
    return dest, meta


def python_prefix(python_dir: Path) -> Path:
    """Normalize python-build-standalone layouts to the install prefix."""
    if (python_dir / "bin" / "python3").exists():
        return python_dir
    nested = python_dir / "python"
    if (nested / "bin" / "python3").exists() or (nested / "bin" / "python3.12").exists():
        return nested
    return python_dir


def extract_musl_loader(apk: Path, dest: Path) -> Path:
    """Extract ld-musl-aarch64.so.1 from an Alpine musl APK."""
    import gzip
    import io
    dest.parent.mkdir(parents=True, exist_ok=True)
    raw = gzip.GzipFile(fileobj=io.BytesIO(apk.read_bytes()))
    with tarfile.open(fileobj=raw) as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            if MUSL_LOADER_NAME not in member.name:
                continue
            extracted = tar.extractfile(member)
            if extracted is None:
                continue
            dest.write_bytes(extracted.read())
            return dest
    err(f"{MUSL_LOADER_NAME} not found in {apk.name}")
    return dest


# ─── Pi boot configuration ────────────────────────────────────────────────────

CONFIG_TXT = """\
# PyOS NOVA — Raspberry Pi 5 Boot Configuration
# ================================================
# This replaces the standard Pi OS config.txt.
# The Pi 5 bootloader reads this before anything else.

# ── Kernel selection ─────────────────────────────────────────────────────────
# We use the standard Pi 5 kernel (kernel_2712.img) but replace the
# entire init system with Python. The kernel boots, finds /sbin/init,
# and that IS Python.
kernel=kernel_2712.img

# ── Initramfs ─────────────────────────────────────────────────────────────────
# The initramfs contains Python 3.12 ARM64 and all PyOS NOVA source files.
# The kernel unpacks it into a RAM disk and runs /sbin/init from there.
initramfs initramfs_nova.gz followkernel

# ── Hardware settings ─────────────────────────────────────────────────────────
# 64-bit mode (required for Python 3.12 ARM64 binary)
arm_64bit=1

# Give GPU minimum RAM — NOVA is text-mode only.
# This leaves maximum RAM for Python, SOS database, and AI models.
gpu_mem=16

# UART serial console: NOVA prints all boot messages here.
# Connect USB-UART to GPIO 14 (TX) and 15 (RX) at 115200 baud.
enable_uart=1

# Force HDMI output even if monitor is off at boot time.
hdmi_force_hotplug=1
hdmi_drive=2

# Short USB enumeration delay (increase to 2 if USB boot fails).
boot_delay=1
"""

CMDLINE_TXT = (
    # serial console — NOVA outputs boot messages here
    "console=serial0,115200 "
    # HDMI console
    "console=tty1 "
    # Boot from RAM disk (our initramfs)
    "root=/dev/ram0 rw "
    # Python IS init — no systemd, no bash, no busybox
    "init=/sbin/init "
    # NOVA-specific flags (read by pyinit_rpi5.py)
    "nova_boot=1 nova_platform=rpi5 nova_version=" + VERSION + " "
    # Keep kernel messages quiet (NOVA handles its own logging)
    "loglevel=3 "
    # Wait for storage devices
    "rootwait"
)


# ─── Python PID-1 for Pi 5 ───────────────────────────────────────────────────

PYINIT_RPI5 = f'''\
#!/usr/bin/env python3
"""
PyOS NOVA — Python PID-1  (Raspberry Pi 5 Edition)
====================================================
Python replaces /sbin/init. The Linux kernel starts this script as the
first userspace process. No systemd. No bash. No Raspberry Pi OS shell.

What we do here:
  1. Mount virtual filesystems (/proc /sys /dev /tmp)
  2. Silence kernel noise on console
  3. Find and mount the NOVA persistent data partition (USB partition 2)
  4. Start NovaKernel (the PyOS NOVA OS)
  5. On exit: sync filesystem, reboot or halt

Hardware available from Python (no extra drivers):
  GPIO   → import gpiod (installed in initramfs)
  I2C    → /dev/i2c-* via smbus2
  SPI    → /dev/spidev* via spidev
  UART   → /dev/ttyAMA0 (115200 baud)
  Camera → picamera2
  Temp   → /sys/class/thermal/thermal_zone0/temp
"""

import os, sys, time, signal, subprocess, traceback

NOVA_VERSION = "{VERSION}"


def log(msg: str, level: str = "INFO") -> None:
    """Print a coloured log message to the console."""
    col = {{"OK": "\\033[32m", "WARN": "\\033[33m", "ERR": "\\033[31m"}}.get(level, "\\033[36m")
    ts  = time.strftime("%H:%M:%S")
    print(f"{{col}}[{{ts}} NOVA]\\033[0m {{msg}}", flush=True)


def mount(src: str, dst: str, fstype: str, opts: str = "") -> None:
    """Mount via Linux syscall, else the mount utility with inherited streams."""
    os.makedirs(dst, exist_ok=True)
    try:
        sys.path.insert(0, "/nova")
        from boot.linux import linux_mount
        linux_mount(src, dst, fstype, opts)
        return
    except Exception:
        pass
    cmd = ["mount", "-t", fstype]
    if opts:
        cmd += ["-o", opts]
    cmd += [src, dst]
    # Inherit the console: /dev/null may not exist until devtmpfs is mounted.
    # Essential mount failures must stop boot instead of reporting success.
    subprocess.run(cmd, check=True)


def setup_filesystems() -> None:
    """Mount the virtual filesystems the OS needs."""
    log("Mounting virtual filesystems")
    mount("proc",     "/proc",    "proc")
    mount("sysfs",    "/sys",     "sysfs")
    mount("devtmpfs", "/dev",     "devtmpfs", "mode=0755")
    mount("devpts",   "/dev/pts", "devpts",   "gid=5,mode=620")
    mount("tmpfs",    "/tmp",     "tmpfs",    "mode=1777,size=256m")
    mount("tmpfs",    "/run",     "tmpfs",    "mode=755,size=64m")
    log("Virtual filesystems ready", "OK")


def mount_data_partition() -> str:
    """Mount the labelled NOVA_DATA filesystem or fail if persistence is required."""
    sys.path.insert(0, "/nova")
    from boot.linux import BootstrapError, mount_persistent_data, reap_zombies

    require = os.environ.get("NOVA_REQUIRE_PERSISTENCE", "1") != "0"
    volatile_ok = os.environ.get("NOVA_VOLATILE_OK", "0") == "1"
    try:
        mnt = mount_persistent_data(require=require, volatile_ok=volatile_ok)
    except BootstrapError as exc:
        log(str(exc), "ERR")
        raise
    if mnt.endswith("volatile"):
        log("VOLATILE MODE: writes will not survive reboot", "ERR")
        os.environ["NOVA_PERSISTENCE"] = "volatile"
    else:
        os.environ["NOVA_PERSISTENCE"] = "durable"
        log(f"Persistent data at {{mnt}}", "OK")
    return mnt


def print_banner() -> None:
    """Print the NOVA boot banner."""
    print("\\n\\033[36m")
    print("  ██████╗ ██╗   ██╗ ██████╗ ███████╗    ███╗   ██╗ ██████╗ ██╗   ██╗ █████╗ ")
    print("  ██╔══██╗╚██╗ ██╔╝██╔═══██╗██╔════╝    ████╗  ██║██╔═══██╗██║   ██║██╔══██╗")
    print("  ██████╔╝ ╚████╔╝ ██║   ██║███████╗    ██╔██╗ ██║██║   ██║██║   ██║███████║")
    print("  ██╔═══╝   ╚██╔╝  ██║   ██║╚════██║    ██║╚██╗██║██║   ██║╚██╗ ██╔╝██╔══██║")
    print("  ██║        ██║   ╚██████╔╝███████║    ██║ ╚████║╚██████╔╝ ╚████╔╝ ██║  ██║")
    print("  ╚═╝        ╚═╝    ╚═════╝ ╚══════╝    ╚═╝  ╚═══╝ ╚═════╝   ╚═══╝  ╚═╝  ╚═╝")
    print("\\033[0m")
    print(f"  Raspberry Pi 5 Native Python OS  ·  v{{NOVA_VERSION}}  ·  Python {{sys.version.split()[0]}}")
    print(f"  No Raspberry Pi OS. No systemd. No bash. Python is the OS.\\n")


def emergency_shell() -> None:
    """Drop to a Python REPL for debugging when boot fails."""
    print("\\n\\033[31m━━━ NOVA EMERGENCY SHELL ━━━\\033[0m")
    print("  Something went wrong. Python REPL for debugging.")
    print(f"  NOVA_SRC  = {{os.environ.get('NOVA_SRC','not set')}}")
    print(f"  NOVA_DATA = {{os.environ.get('NOVA_DATA','not set')}}")
    print(f"  sys.path  = {{sys.path[:3]}}")
    print("  Type exit() to halt.\\n")
    try:
        import code
        code.interact(banner="")
    except Exception:
        pass


def boot() -> int:
    """Main boot sequence."""
    # 1. Virtual filesystems
    setup_filesystems()

    # 2. Silence kernel console noise (NOVA handles its own output)
    try:
        with open("/proc/sys/kernel/printk", "w") as f:
            f.write("3 4 1 3")
    except Exception:
        pass

    # 3. Boot banner
    print_banner()

    # 4. Mount persistent data partition
    data_mnt = mount_data_partition()
    nova_data = data_mnt
    os.makedirs(nova_data, exist_ok=True)
    os.environ["NOVA_DATA"] = nova_data

    # 5. Locate NOVA source tree (packed into the initramfs at /nova/)
    for candidate in ["/nova", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]:
        if os.path.exists(os.path.join(candidate, "main.py")):
            sys.path.insert(0, candidate)
            os.environ["NOVA_SRC"] = candidate
            log(f"NOVA source: {{candidate}}", "OK")
            break
    else:
        log("NOVA source not found in initramfs", "ERR")
        emergency_shell()
        return 1

    # 6. Start the NOVA kernel
    log(f"Starting NovaKernel v{{NOVA_VERSION}}...")
    try:
        import main as nova_main
        return nova_main.main() or 0
    except ImportError as exc:
        log(f"Cannot import NOVA: {{exc}}", "ERR")
        emergency_shell()
        return 1
    except Exception as exc:
        log(f"NOVA kernel crashed: {{exc}}", "ERR")
        traceback.print_exc()
        emergency_shell()
        return 1


def _reap(_signum=None, frame=None):
    try:
        from boot.linux import reap_zombies
        reap_zombies()
    except Exception:
        pass

# PID-1 must not die — the kernel panics if init exits.
signal.signal(signal.SIGTERM, lambda *_: None)
signal.signal(signal.SIGINT,  lambda *_: None)
signal.signal(signal.SIGCHLD, _reap)

if __name__ == "__main__":
    try:
        exit_code = boot()
    except SystemExit as e:
        exit_code = e.code if isinstance(e.code, int) else 0
    except Exception as exc:
        print(f"\\033[31mFATAL: PID-1 crashed: {{exc}}\\033[0m", file=sys.stderr)
        traceback.print_exc()
        exit_code = 1

    # Sync all filesystems before halt/reboot
    try:
        os.sync()
    except Exception:
        pass
    time.sleep(0.5)

    # Reboot on clean exit (code 0), halt on error
    try:
        if exit_code == 0:
            subprocess.run(["reboot", "-f"], timeout=5)
        else:
            subprocess.run(["halt", "-f"], timeout=5)
    except Exception:
        pass

    # If reboot/halt commands fail, spin forever (PID-1 must not exit)
    while True:
        time.sleep(60)
'''


# ─── CPIO initramfs writer ────────────────────────────────────────────────────

class CPIOWriter:
    """
    Writes a Linux initramfs in newc (SVR4) CPIO format.

    The resulting archive is gzip-compressed to form initramfs_nova.gz.
    The Linux kernel unpacks this into a RAM disk at boot.
    """

    MAGIC = b"070701"

    def __init__(self):
        self._entries = []
        self._ino = 1

    def add_dir(self, path: str, mode: int = 0o040755):
        self._entries.append((path.lstrip("/"), b"", mode, 0, 0))

    def add_file(self, path: str, data: bytes, mode: int = 0o100644):
        self._entries.append((path.lstrip("/"), data, mode, 0, 0))

    def add_symlink(self, path: str, target: str):
        self._entries.append((path.lstrip("/"), target.encode(), 0o120777, 0, 0))

    def add_device(self, path: str, major: int, minor: int,
                   permissions: int = 0o600) -> None:
        """Encode a character device without requiring host mknod privileges."""
        self._entries.append((path.lstrip("/"), b"", 0o020000 | permissions, major, minor))

    def add_file_from_disk(self, cpio_path: str, disk_path: Path, mode: int = 0o100644):
        self.add_file(cpio_path, disk_path.read_bytes(), mode)

    def _header(self, name: str, data: bytes, mode: int,
                rdevmajor: int = 0, rdevminor: int = 0) -> bytes:
        nb = name.encode() + b"\x00"
        nl, dl = len(nb), len(data)
        self._ino += 1
        h8 = lambda n: f"{n:08x}".encode()
        hdr = (self.MAGIC + h8(self._ino) + h8(mode) + h8(0) + h8(0)
               + h8(1) + h8(int(time.time())) + h8(dl)
               + h8(0) + h8(0) + h8(rdevmajor) + h8(rdevminor) + h8(nl) + h8(0))
        np = (4 - (110 + nl) % 4) % 4
        dp = (4 - dl % 4) % 4
        return hdr + nb + b"\x00" * np + data + b"\x00" * dp

    def build(self) -> bytes:
        parts = [self._header(*entry) for entry in self._entries]
        parts.append(self._header("TRAILER!!!", b"", 0))
        return b"".join(parts)


# ─── Initramfs builder ────────────────────────────────────────────────────────

def build_initramfs(python_dir: Path, nova_source_zip: Path,
                     output: Path, musl_loader: Path | None = None) -> int:
    """
    Build the initramfs containing Python 3.12 ARM64 + PyOS NOVA.

    Args:
        python_dir:      Extracted Python ARM64 directory.
        nova_source_zip: Zip containing NOVA Python source.
        output:          Output path for initramfs_nova.gz.
        musl_loader:     Optional ld-musl-aarch64.so.1 for musl Python.

    Returns:
        Size of the compressed initramfs in bytes.
    """
    log("Building initramfs (Python ARM64 + NOVA source)...")
    python_dir = python_prefix(python_dir)
    cpio = CPIOWriter()

    # Standard directory hierarchy
    for d in ("dev", "dev/pts", "proc", "sys", "tmp", "run",
              "mnt", "mnt/nova_data", "sbin", "bin",
              "usr", "usr/bin", "usr/local", "usr/local/bin", "usr/local/lib",
              "etc", "lib", "lib64",
              "nova", "nova/boot"):
        cpio.add_dir(d)

    # ── Python interpreter ─────────────────────────────────────────────────────
    log("  Embedding Python 3.12 ARM64...")
    python_bin = python_dir / "bin" / "python3"
    if not python_bin.exists():
        # python-build-standalone layout
        for candidate in python_dir.rglob("python3.12"):
            if candidate.is_file() and not candidate.suffix:
                python_bin = candidate
                break

    if python_bin.exists():
        cpio.add_file("usr/local/bin/python3", python_bin.read_bytes(), 0o100755)
        cpio.add_symlink("bin/python3",         "/usr/local/bin/python3")
        cpio.add_symlink("usr/bin/python3",     "/usr/local/bin/python3")
        cpio.add_symlink("usr/local/bin/python","/usr/local/bin/python3")
        ok(f"  Python binary: {python_bin.stat().st_size//MB} MB")
    else:
        warn("  Python binary not found in extracted archive")

    # ── Python standard library ────────────────────────────────────────────────
    lib_dir = python_dir / "lib"
    if not lib_dir.exists():
        # Try alternate layout
        for d in python_dir.rglob("python3.12"):
            if d.is_dir():
                lib_dir = d.parent
                break

    py_lib_count = 0
    for lib_path in lib_dir.rglob("*"):
        if lib_path.is_dir():
            continue
        # Skip test files and cache (save ~30% space)
        parts = lib_path.parts
        if any(p in ("test", "tests", "__pycache__", "idle_test") for p in parts):
            continue
        rel = lib_path.relative_to(python_dir)
        rel_s = str(rel).replace("\\", "/")
        if rel_s.startswith("python/"):
            rel_s = rel_s[len("python/"):]
        dest_dir = "/".join(["usr/local"] + rel_s.split("/")[:-1])
        cpio.add_dir(dest_dir)
        cpio.add_file(f"usr/local/{rel_s}", lib_path.read_bytes())
        py_lib_count += 1

    ok(f"  Python stdlib: {py_lib_count} files")

    if musl_loader and Path(musl_loader).is_file():
        data = Path(musl_loader).read_bytes()
        cpio.add_file(f"lib/{MUSL_LOADER_NAME}", data, 0o100755)
        cpio.add_symlink(f"lib64/{MUSL_LOADER_NAME}", f"/lib/{MUSL_LOADER_NAME}")
        ok(f"  musl loader: {MUSL_LOADER_NAME}")

    # ── Shared libraries Python needs ──────────────────────────────────────────
    # python-build-standalone bundles libpython statically, but we may need
    # minimal system libs for modules that use ctypes/cffi
    for so_name in ("libz.so.1", "libffi.so.8", "libssl.so.3", "libcrypto.so.3"):
        for so_dir in (Path("/usr/lib/aarch64-linux-gnu"), Path("/usr/lib")):
            so_path = so_dir / so_name
            if so_path.exists():
                cpio.add_file(f"usr/local/lib/{so_name}", so_path.read_bytes())
                break

    # ── /sbin/init — the Python launcher ──────────────────────────────────────
    init_script = (
        "#!/usr/local/bin/python3\n"
        "# /sbin/init — PyOS NOVA PID-1\n"
        "# The Linux kernel executes this as the first userspace process.\n"
        "import os, sys\n"
        "# Add NOVA source to Python path\n"
        "sys.path.insert(0, '/nova')\n"
        "# Run the NOVA boot sequence\n"
        "exec(open('/nova/boot/pyinit_rpi5.py').read())\n"
    )
    cpio.add_file("sbin/init", init_script.encode(), 0o100755)
    cpio.add_symlink("init", "/sbin/init")

    # ── pyinit_rpi5.py ────────────────────────────────────────────────────────
    cpio.add_file("nova/boot/pyinit_rpi5.py", PYINIT_RPI5.encode())

    # ── PyOS NOVA source ───────────────────────────────────────────────────────
    log("  Embedding NOVA source...")
    import zipfile
    nova_count = 0
    seen_dirs  = set()

    with zipfile.ZipFile(nova_source_zip) as z:
        for name in z.namelist():
            if not name.endswith(".py"):
                continue
            if any(p in name for p in ("__pycache__", "tests", ".git")):
                continue
            # Strip the leading "nova/" prefix from the zip path
            rel = name[5:] if name.startswith("nova/") else name
            # Ensure parent directories exist
            parts = rel.split("/")
            for i in range(1, len(parts)):
                parent = "nova/" + "/".join(parts[:i])
                if parent not in seen_dirs:
                    cpio.add_dir(parent)
                    seen_dirs.add(parent)
            cpio.add_file(f"nova/{rel}", z.read(name))
            nova_count += 1

    ok(f"  NOVA source: {nova_count} files")

    # ── System config ──────────────────────────────────────────────────────────
    cpio.add_file("etc/hostname",   b"nova-pi5\n")
    cpio.add_file("etc/hosts",      b"127.0.0.1 localhost nova-pi5\n")
    cpio.add_file("etc/os-release",
        f'NAME="PyOS NOVA"\nVERSION="{VERSION}"\nID=nova\n'
        f'PRETTY_NAME="PyOS NOVA {VERSION} (Pi 5)"\n'.encode())
    cpio.add_file("etc/fstab",
        b"# PyOS NOVA fstab\ntmpfs /tmp tmpfs defaults 0 0\n")
    # /dev/console must exist before devtmpfs is mounted
    cpio.add_device("dev/console", 5, 1, permissions=0o600)
    cpio.add_device("dev/null", 1, 3, permissions=0o666)
    # ld cache
    cpio.add_file("etc/ld.so.conf",
        b"/usr/local/lib\n/usr/local/lib/python3.12/lib-dynload\n")

    # ── Build the compressed archive ───────────────────────────────────────────
    log("  Compressing initramfs...")
    raw = cpio.build()
    compressed = gzip.compress(raw, compresslevel=6)
    output.write_bytes(compressed)

    size_mb = len(compressed) // MB
    ok(f"  initramfs_nova.gz: {size_mb} MB ({len(raw)//MB} MB uncompressed)")
    return len(compressed)


# ─── Disk image assembly ──────────────────────────────────────────────────────

def build_fat32_image(size_mb: int, label: str = "NOVABOOT") -> bytearray:
    """
    Create a minimal FAT32 filesystem image.

    Args:
        size_mb: Size in MB.
        label:   Volume label (max 11 chars).

    Returns:
        Bytearray containing the FAT32 image.
    """
    size_b         = size_mb * MB
    total_sectors  = size_b // 512
    spc            = 8          # sectors per cluster
    reserved       = 32         # reserved sectors (FAT32 standard)
    num_fats       = 2
    data_clusters  = (total_sectors - reserved) // spc
    fat_sectors    = ((data_clusters + 2) * 4 + 511) // 512

    bpb = bytearray(512)
    bpb[0:3]    = b"\xEB\x58\x90"
    bpb[3:11]   = b"MSDOS5.0"
    bpb[11:13]  = struct.pack("<H", 512)
    bpb[13]     = spc
    bpb[14:16]  = struct.pack("<H", reserved)
    bpb[16]     = num_fats
    bpb[19:21]  = b"\x00\x00"
    bpb[21]     = 0xF8
    bpb[22:24]  = b"\x00\x00"
    bpb[24:26]  = struct.pack("<H", 63)
    bpb[26:28]  = struct.pack("<H", 255)
    bpb[28:32]  = struct.pack("<I", 0)
    bpb[32:36]  = struct.pack("<I", total_sectors)
    bpb[36:40]  = struct.pack("<I", fat_sectors)
    bpb[40:42]  = struct.pack("<H", 0)
    bpb[42:44]  = struct.pack("<H", 0)
    bpb[44:48]  = struct.pack("<I", 2)   # root cluster = 2
    bpb[48:50]  = struct.pack("<H", 1)   # FSInfo at sector 1
    bpb[50:52]  = struct.pack("<H", 6)   # backup boot at sector 6
    bpb[64]     = 0x80                   # drive number
    bpb[66]     = 0x29                   # extended boot signature
    bpb[67:71]  = b"\x00\x00\x00\x00"   # volume serial
    bpb[71:82]  = label[:11].upper().ljust(11).encode()
    bpb[82:90]  = b"FAT32   "
    bpb[510:512]= b"\x55\xAA"

    data = bytearray(size_b)
    data[:512] = bpb

    # Write FAT1 and FAT2
    fat = bytearray(fat_sectors * 512)
    fat[0:4]   = b"\xF8\xFF\xFF\x0F"    # cluster 0: media type
    fat[4:8]   = b"\xFF\xFF\xFF\x0F"    # cluster 1: reserved
    fat[8:12]  = b"\xFF\xFF\xFF\x0F"    # cluster 2: root dir (EOF)

    fat_offset = reserved * 512
    data[fat_offset:fat_offset + len(fat)]            = fat
    data[fat_offset + fat_sectors*512:
         fat_offset + fat_sectors*512 * 2]            = fat

    return data


def write_file_to_fat32(fat_img: bytearray, filename: str, content: bytes) -> bool:
    """
    Write a file into a FAT32 image using raw cluster allocation.

    This is a simplified FAT32 writer sufficient for our boot files.
    In production use mtools — this covers the case when mtools is absent.

    Args:
        fat_img:  FAT32 image bytearray (modified in place).
        filename: 8.3 format filename (uppercase).
        content:  File content.

    Returns:
        True if successful.
    """
    # For simplicity, we mark the file as existing in the root directory
    # and write data starting at cluster 3.
    # A full FAT32 implementation is complex; use mtools when available.
    # This stub writes the content at a known offset and creates a dir entry.
    # WARNING: Only works for files that fit in one cluster group.

    # Parse BPB
    spc          = fat_img[13]
    reserved     = struct.unpack("<H", fat_img[14:16])[0]
    num_fats     = fat_img[16]
    fat_sectors  = struct.unpack("<I", fat_img[36:40])[0]
    root_cluster = struct.unpack("<I", fat_img[44:48])[0]

    bytes_per_cluster = spc * 512
    # Root directory is at cluster 2, which starts at:
    data_start = (reserved + num_fats * fat_sectors) * 512
    root_dir_offset = data_start  # cluster 2

    # Find free cluster for file data (start at cluster 3)
    file_cluster = root_cluster + 1   # cluster 3

    # Write file content at file cluster
    file_offset = data_start + (file_cluster - root_cluster) * bytes_per_cluster
    if file_offset + len(content) <= len(fat_img):
        fat_img[file_offset:file_offset + len(content)] = content

    # Write FAT chain (mark file cluster as EOF)
    fat_offset = reserved * 512
    cluster_entry_offset = fat_offset + file_cluster * 4
    struct.pack_into("<I", fat_img, cluster_entry_offset, 0x0FFFFFFF)
    # Second FAT
    fat_offset2 = fat_offset + fat_sectors * 512
    struct.pack_into("<I", fat_img, fat_offset2 + file_cluster * 4, 0x0FFFFFFF)

    # Write root directory entry (32 bytes)
    name_padded = filename[:8].ljust(8).upper().encode()
    ext_padded  = filename[9:12].ljust(3).upper().encode() if "." in filename else b"   "
    if "." in filename:
        parts = filename.upper().split(".")
        name_padded = parts[0][:8].ljust(8).encode()
        ext_padded  = parts[-1][:3].ljust(3).encode()

    file_size_bytes = len(content)
    import time as _t
    now = _t.localtime()
    fat_time = (now.tm_hour << 11) | (now.tm_min << 5) | (now.tm_sec // 2)
    fat_date = ((now.tm_year - 1980) << 9) | (now.tm_mon << 5) | now.tm_mday

    dir_entry = bytearray(32)
    dir_entry[0:8]   = name_padded
    dir_entry[8:11]  = ext_padded
    dir_entry[11]    = 0x20      # archive attribute
    dir_entry[14:16] = struct.pack("<H", fat_time)
    dir_entry[16:18] = struct.pack("<H", fat_date)
    dir_entry[20:22] = struct.pack("<H", (file_cluster >> 16) & 0xFFFF)
    dir_entry[22:24] = struct.pack("<H", fat_time)
    dir_entry[24:26] = struct.pack("<H", fat_date)
    dir_entry[26:28] = struct.pack("<H", file_cluster & 0xFFFF)
    dir_entry[28:32] = struct.pack("<I", file_size_bytes)

    # Find first free slot in root directory
    for i in range(0, 512, 32):
        if fat_img[root_dir_offset + i] in (0x00, 0xE5):
            fat_img[root_dir_offset + i:root_dir_offset + i + 32] = dir_entry
            return True

    return False


def populate_fat32_with_mtools(fat_path: Path, files: dict) -> bool:
    """
    Use mtools to write files into FAT32 image (accurate, full FAT32 support).

    Args:
        fat_path: Path to FAT32 image file.
        files:    Dict of {fat_path_str: content_bytes_or_Path}.

    Returns:
        True if mtools was available and succeeded.
    """
    mcopy = shutil.which("mcopy")
    mmd   = shutil.which("mmd")
    if not (mcopy and mmd):
        return False

    env = {**os.environ, "MTOOLS_SKIP_CHECK": "1"}
    img = str(fat_path)

    for dst_path, src in files.items():
        parts = dst_path.lstrip("/").split("/")
        # Create parent directories
        for i in range(1, len(parts)):
            parent = "/".join(parts[:i])
            subprocess.run(["mmd", "-i", img, f"::{parent}"],
                            env=env, capture_output=True)
        # Write file
        if isinstance(src, Path):
            subprocess.run(["mcopy", "-i", img, str(src), f"::{dst_path}"],
                            env=env, capture_output=True, check=True)
        else:
            tmp = Path(tempfile.mktemp())
            tmp.write_bytes(src if isinstance(src, bytes) else src.encode())
            subprocess.run(["mcopy", "-i", img, str(tmp), f"::{dst_path}"],
                            env=env, capture_output=True, check=True)
            tmp.unlink()

    return True


def build_disk_image(output: Path, boot_files: dict, total_mb: int) -> Path:
    """
    Assemble the final bootable USB disk image (MBR + FAT32 + data partition).

    The Pi 5 bootloader works best with an MBR-partitioned disk (not GPT)
    for USB boot. We use partition type 0x0B (FAT32 with CHS addressing).

    Args:
        output:     Output .img path.
        boot_files: Dict of filename → bytes/Path for the boot partition.
        total_mb:   Total image size in MB.

    Returns:
        Path to the finished image.
    """
    log(f"Creating {total_mb} MB disk image...")

    boot_start_mb  = 1
    data_start_mb  = boot_start_mb + BOOT_MB

    boot_start_lba = boot_start_mb  * MB // 512    # 2048
    boot_size_lba  = BOOT_MB        * MB // 512    # 524288
    data_start_lba = data_start_mb  * MB // 512
    data_size_lba  = DATA_MB        * MB // 512

    # ── MBR ───────────────────────────────────────────────────────────────────
    mbr = bytearray(512)
    # Partition 1: FAT32, bootable (0x80), type 0x0B
    mbr[446:462] = _mbr_entry(True,  0x0B, boot_start_lba, boot_size_lba)
    # Partition 2: Linux data, type 0x83 (NOVA data partition)
    mbr[462:478] = _mbr_entry(False, 0x83, data_start_lba, data_size_lba)
    mbr[510:512] = b"\x55\xAA"

    # ── FAT32 boot partition ──────────────────────────────────────────────────
    log("Building FAT32 boot partition...")
    from build.fat32 import Fat32Error, build_fat32_files

    payload: dict[str, bytes] = {}
    for fname, content in boot_files.items():
        if isinstance(content, Path):
            content = content.read_bytes()
        elif isinstance(content, str):
            content = content.encode()
        payload["/" + str(fname).lstrip("/")] = content
    try:
        fat_bytes = build_fat32_files(BOOT_MB * MB, payload, label="NOVABOOT")
    except Fat32Error as exc:
        err(f"FAT32 write/readback failed: {exc}")
        raise

    # ── Assemble disk image ───────────────────────────────────────────────────
    log(f"Assembling {total_mb} MB disk image...")
    # Create with truncate
    with open(output, "wb") as f:
        f.seek(total_mb * MB - 1)
        f.write(b"\x00")

    with open(output, "r+b") as f:
        # MBR at sector 0
        f.seek(0)
        f.write(bytes(mbr))
        # Boot partition at sector 2048 (1 MB)
        f.seek(boot_start_lba * 512)
        f.write(fat_bytes[:BOOT_MB * MB])

    # Format the persistent ext4 filesystem inside the image file (not a device).
    from build.persist_fs import format_ext4_file, write_partition_slice
    fs_tmp = output.with_suffix(".ext4.tmp")
    format_ext4_file(fs_tmp, DATA_MB * MB, label="NOVA_DATA")
    write_partition_slice(output, data_start_lba * 512, DATA_MB * MB, fs_tmp)
    fs_tmp.unlink(missing_ok=True)
    ok("NOVA_DATA ext4 filesystem written into partition 2")

    ok(f"Disk image: {output.stat().st_size // MB} MB")
    return output


def write_build_manifest(output: Path, source_meta: dict,
                          cache_dir: Path, extra: dict | None = None) -> Path:
    """Write a source/checksum manifest next to the image (not in git)."""
    manifest = {
        "target": "raspberry-pi5-linux-userspace",
        "certified_hardware_boot": False,
        "source": source_meta,
        "python": {
            "url": PYTHON_ARM64_URL,
            "sha256": PYTHON_ARM64_SHA256,
        },
        "musl_loader": {
            "url": MUSL_LOADER_URL,
            "sha256": MUSL_LOADER_SHA256,
        },
        "partitions": {
            "1": {"type": "0x0B", "label": "NOVABOOT", "fs": "fat32"},
            "2": {"type": "0x83", "label": "NOVA_DATA", "fs": "ext4"},
        },
        "output": {
            "path": str(output),
            "size": output.stat().st_size,
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        },
        "cache": str(cache_dir),
    }
    if extra:
        manifest.update(extra)
    dest = output.with_suffix(output.suffix + ".manifest.json")
    dest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return dest


def _mbr_entry(bootable: bool, part_type: int,
                lba_start: int, lba_size: int) -> bytes:
    """Build a 16-byte MBR partition entry."""
    return struct.pack(
        "<BBBBBBBBII",
        0x80 if bootable else 0x00,  # status
        0xFE, 0xFF, 0xFF,             # CHS first (maxed, LBA is authoritative)
        part_type,
        0xFE, 0xFF, 0xFF,             # CHS last
        lba_start,
        lba_size,
    )


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    """Build the complete PyOS NOVA Raspberry Pi 5 USB image."""
    parser = argparse.ArgumentParser(
        description="Build a bootable PyOS NOVA USB image for Raspberry Pi 5",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
After building, keep the image out of git. Hardware flashing requires an
explicitly confirmed target. Do not guess /dev/sdX. Preserve an existing
NOVA_DATA partition when replacing boot artifacts.
        """,
    )
    parser.add_argument("--output", "-o", default="nova_pi5.img",
                         help="Output image path (default: nova_pi5.img)")
    parser.add_argument("--size",   "-s", default="770M",
                         help="Image size (default: 770M = 256MB boot + 512MB data)")
    parser.add_argument("--cache",  "-c", default="downloads",
                         help="Download cache directory (default: downloads/)")
    parser.add_argument("--nova-zip", default=None,
                         help="Path to NOVA source zip (auto-detected if not given)")
    args = parser.parse_args()

    # Parse size
    s = args.size.upper()
    if s.endswith("G"):   total_mb = int(s[:-1]) * 1024
    elif s.endswith("M"): total_mb = int(s[:-1])
    else:                  total_mb = int(s) // MB
    total_mb = max(total_mb, BOOT_MB + DATA_MB + 2)

    output     = Path(args.output).resolve()
    cache_dir  = Path(args.cache).resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{BOLD}{CYAN}PyOS NOVA — Raspberry Pi 5 Image Builder{RESET}")
    print(f"{CYAN}=========================================={RESET}")
    print(f"  Output:   {output}")
    print(f"  Size:     {total_mb} MB  ({BOOT_MB} MB boot + {DATA_MB} MB data)")
    print(f"  Cache:    {cache_dir}\n")

    # ── Step 1: Pack current source ──────────────────────────────────────────────
    nova_zip = None
    source_meta = {}
    if args.nova_zip:
        nova_zip = Path(args.nova_zip)
        if not nova_zip.exists():
            err(f"--nova-zip not found: {nova_zip}")
        source_meta["zip_sha256"] = hashlib.sha256(nova_zip.read_bytes()).hexdigest()
        source_meta["note"] = "explicit zip input; hash recorded"
    else:
        nova_zip, source_meta = pack_current_source(cache_dir / "datapy_source.zip")
    ok(f"NOVA source: {nova_zip.name} commit={source_meta.get('commit','?')}")

    # ── Step 2: Download Pi firmware ──────────────────────────────────────────
    print(f"\n{BOLD}Step 1/4: Downloading Raspberry Pi 5 firmware{RESET}")
    pi_files = {}
    for fname, desc in PI_FIRMWARE_FILES.items():
        url  = f"{FIRMWARE_BASE}/{fname}"
        dest = cache_dir / fname
        f    = download(url, dest, desc)
        pi_files[fname] = f

    # ── Step 3: Download Python ARM64 ─────────────────────────────────────────
    print(f"\n{BOLD}Step 2/4: Downloading Python 3.12 ARM64 (static){RESET}")
    py_archive = cache_dir / "python_arm64.tar.gz"
    download(PYTHON_ARM64_URL, py_archive, "Python 3.12.14 ARM64 musl",
             sha256=PYTHON_ARM64_SHA256)
    musl_apk = cache_dir / "musl-1.2.5-r3.apk"
    download(MUSL_LOADER_URL, musl_apk, "Alpine musl aarch64",
             sha256=MUSL_LOADER_SHA256)

    # Extract Python
    py_dir = cache_dir / "python_arm64"
    if not py_dir.exists():
        log("Extracting Python ARM64...")
        with tarfile.open(py_archive, "r:gz") as t:
            # Find the top-level directory in the archive
            members = t.getmembers()
            t.extractall(py_dir)
        ok("Python ARM64 extracted")
    else:
        ok("Python ARM64 already extracted")

    # ── Step 4: Build initramfs ────────────────────────────────────────────────
    print(f"\n{BOLD}Step 3/4: Building initramfs (Python + NOVA){RESET}")
    initrd_path = cache_dir / "initramfs_nova.gz"
    if initrd_path.exists():
        initrd_path.unlink()  # always rebuild fresh
    musl_loader = extract_musl_loader(musl_apk, cache_dir / "musl_aarch64" / MUSL_LOADER_NAME)
    build_initramfs(py_dir, nova_zip, initrd_path, musl_loader=musl_loader)

    # ── Step 5: Assemble disk image ────────────────────────────────────────────
    print(f"\n{BOLD}Step 4/4: Assembling bootable disk image{RESET}")

    # All files for the boot partition
    boot_files = {
        "config.txt":        CONFIG_TXT,
        "cmdline.txt":       CMDLINE_TXT,
        "initramfs_nova.gz": initrd_path,
        **{k: v for k, v in pi_files.items()},
    }

    build_disk_image(output, boot_files, total_mb)
    manifest = write_build_manifest(output, source_meta, cache_dir)
    ok(f"Manifest: {manifest}")

    # ── Done ───────────────────────────────────────────────────────────────────
    final_mb = output.stat().st_size // MB
    print(f"\n{CYAN}{BOLD}Image assembled (host build). Pi 5 hardware boot is NOT certified.{RESET}")
    print(f"  Image: {output} ({final_mb} MB)")
    print(f"  Persistence: ext4 labelled NOVA_DATA in partition 2")
    print(f"  Do not flash without an explicitly confirmed target device.")
    print(f"  Do not guess /dev/sdX. Preserve an existing data partition.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
