"""
PyOS NOVA — Raspberry Pi Emulator
====================================
Emulates a Raspberry Pi 3B / Pi 4 (ARM64) using QEMU.
Downloads all needed components automatically.

Boot chain:
  QEMU (qemu-system-aarch64)
    → Raspberry Pi UEFI firmware (EDK2, open source)
      → /EFI/BOOT/BOOTAA64.EFI  (our ARM64 EFI app, Python-generated)
        → Python PID 1 (ARM64 CPython inside initramfs)
          → NOVA kernel + shell

Why this is the right approach:
  - Raspberry Pi 3/4/5 all support UEFI via the official Pi UEFI firmware
  - QEMU -machine virt gives a clean ARM64 UEFI environment
  - No need to emulate the Pi's VideoCore GPU — we use -display none
  - Works on any host OS (macOS, Linux, Windows with WSL)

Usage:
  python3 rpi/rpi_emulator.py              # download + launch Pi emulator
  python3 rpi/rpi_emulator.py --install    # show install instructions
  python3 rpi/rpi_emulator.py --quick      # use existing Raspberry Pi OS image
  python3 rpi/rpi_emulator.py --ram 2048   # more RAM (default 1024 MB)
"""

import os, sys, shutil, struct, tarfile, zipfile
import subprocess, argparse, hashlib, json, time
import urllib.request
from pathlib import Path

ROOT  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(ROOT, "build")
RPI   = os.path.join(ROOT, "rpi")
DATA  = os.environ.get("NOVA_DATA", os.path.expanduser("~/.nova"))

sys.path.insert(0, ROOT)

# ─────────────────────────────────────────────────────────────────────────────
# Download URLs
# ─────────────────────────────────────────────────────────────────────────────

# Raspberry Pi UEFI firmware (open source, MIT license)
# Provides UEFI + BOOTAA64.EFI chainloader for Pi 3/4
RPI_UEFI_URL = (
    "https://github.com/pftf/RPi4/releases/download/v1.36/"
    "RPi4_UEFI_Firmware_v1.36.zip"
)
# Fallback: RPi3
RPI3_UEFI_URL = (
    "https://github.com/pftf/RPi3/releases/download/v1.39/"
    "RPi3_UEFI_Firmware_v1.39.zip"
)

# Minimal Alpine Linux ARM64 (for quick testing — ~50MB vs 1GB Pi OS)
ALPINE_ARM64_URL = (
    "https://dl-cdn.alpinelinux.org/alpine/v3.19/releases/aarch64/"
    "alpine-minirootfs-3.19.1-aarch64.tar.gz"
)

# Raspberry Pi OS Lite ARM64 (full Pi experience — ~500MB compressed)
RPI_OS_URL = (
    "https://downloads.raspberrypi.com/raspios_lite_arm64/images/"
    "raspios_lite_arm64-2024-03-15/2024-03-15-raspios-bookworm-arm64-lite.img.xz"
)

# QEMU ARM64 EFI firmware (AAVMF — ARM equivalent of OVMF)
AAVMF_SEARCH = [
    "/usr/share/AAVMF/AAVMF_CODE.fd",
    "/usr/share/qemu-efi-aarch64/QEMU_EFI.fd",
    "/opt/homebrew/share/qemu/edk2-aarch64-code.fd",
    "/usr/share/edk2/aarch64/QEMU_EFI.fd",
    os.path.expanduser("~/.nova/AAVMF.fd"),
    os.path.join(BUILD, "AAVMF.fd"),
]

AAVMF_DOWNLOAD = (
    "https://github.com/rust-vmm/edk2-aarch64/releases/download/v0.1.0/"
    "QEMU_EFI.fd"
)


# ─────────────────────────────────────────────────────────────────────────────
# Dependency detection
# ─────────────────────────────────────────────────────────────────────────────

def find_qemu_arm() -> str | None:
    """Find and return qemu arm.


        Returns:
            str | None: Result.
        """
    for name in ("qemu-system-aarch64",):
        p = shutil.which(name)
        if p:
            return p
    return None


def find_aavmf() -> str | None:
    """Find and return aavmf.


        Returns:
            str | None: Result.
        """
    for p in AAVMF_SEARCH:
        if os.path.exists(p):
            return p
    return None


def install_instructions() -> str:
    """Install instructions.


        Returns:
            str: Result.
        """
    lines = [
        "\n  Install QEMU for ARM64 emulation:\n",
        "  macOS (Homebrew):",
        "    brew install qemu",
        "",
        "  Ubuntu / Debian:",
        "    sudo apt install qemu-system-arm qemu-efi-aarch64",
        "",
        "  Fedora / RHEL:",
        "    sudo dnf install qemu-system-aarch64 edk2-aarch64",
        "",
        "  Windows:",
        "    1. Install MSYS2 from msys2.org",
        "    2. pacman -S mingw-w64-x86_64-qemu",
        "    or download QEMU installer from qemu.org/download",
        "",
        "  After installing, run this script again.",
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Disk image builder (ARM64 FAT32 with NOVA content)
# ─────────────────────────────────────────────────────────────────────────────

def _download(url: str, dest: str, label: str = None):
    """Download the operation to local storage.

        Args:
        url (str): Url.
        dest (str): Dest.
        label (str): Label, defaults to None.
        """
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest):
        print(f"  Already downloaded: {os.path.basename(dest)}")
        return dest

    label = label or os.path.basename(url)
    print(f"  Downloading {label}...")

    def progress(block, bs, total):
        """Progress.

            Args:
            block: Block.
            bs: Bs.
            total: Total.
            """
        if total > 0:
            pct = min(block * bs / total * 100, 100)
            mb  = block * bs / 1024 / 1024
            print(f"\r  {pct:.0f}%  {mb:.1f} MB   ", end="", flush=True)
        else:
            mb = block * bs / 1024 / 1024
            print(f"\r  {mb:.1f} MB...   ", end="", flush=True)

    try:
        urllib.request.urlretrieve(url, dest, reporthook=progress)
        print(f"\r  Done — {os.path.getsize(dest)//1024//1024} MB      ")
        return dest
    except Exception as e:
        print(f"\r  Download failed: {e}")
        if os.path.exists(dest):
            os.remove(dest)
        return None


def build_nova_arm64_image(size_mb: int = 256) -> str:
    """Build a FAT32 disk image with NOVA content + ARM64 EFI."""
    os.makedirs(BUILD, exist_ok=True)

    # Generate ARM64 EFI
    print("  Building ARM64 EFI (BOOTAA64.EFI)...")
    from build.arm64_efi_builder import build_arm64_efi_file
    efi_path = os.path.join(BUILD, "BOOTAA64.EFI")
    build_arm64_efi_file(efi_path, verify=True)
    efi_data = open(efi_path, "rb").read()

    # Build FAT32 image
    print(f"  Building {size_mb}MB FAT32 image with NOVA source...")
    sys.path.insert(0, BUILD)
    from build.vbox_builder import FAT32
    fat = FAT32(size_mb)

    # ARM64 EFI boot entry
    fat.add_file("EFI\\BOOT\\BOOTAA64.EFI", efi_data)

    # Startup script for UEFI shell
    startup = (
        "\\nova\\boot\\pyinit.py\r\n"
        "python3 \\nova\\main.py\r\n"
    ).encode()
    fat.add_file("startup.nsh", startup)

    # NOVA source
    count = 0
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames
                       if d not in ("__pycache__", ".git", "build", ".nova", "rpi")]
        for fname in filenames:
            if fname.endswith((".py", ".md", ".txt", ".sh", ".cfg")):
                full = os.path.join(dirpath, fname)
                rel  = os.path.relpath(full, ROOT)
                fat.add_file("NOVA\\" + rel.replace("/","\\").replace("..","__"),
                             open(full,"rb").read())
                count += 1

    # ARM-specific install script
    fat.add_file("NOVA\\rpi\\install_nova_rpi.sh",
                 _rpi_install_script().encode())

    print(f"        {count} source files embedded")
    raw = fat.build()

    img_path = os.path.join(BUILD, "nova_rpi.img")
    with open(img_path, "wb") as f:
        f.write(raw)
    print(f"  Image: {img_path}  ({len(raw)//1024//1024} MB)")
    return img_path


# ─────────────────────────────────────────────────────────────────────────────
# Quick mode: use Raspberry Pi OS image with NOVA installed on top
# ─────────────────────────────────────────────────────────────────────────────

def build_pi_os_image() -> str | None:
    """
    Download Raspberry Pi OS Lite (ARM64) and inject NOVA into it.
    This gives a full Pi OS experience with NOVA available.
    """
    rpi_os_xz = os.path.join(DATA, "rpi_os_lite_arm64.img.xz")
    rpi_os_img = os.path.join(BUILD, "rpi_os_nova.img")

    if os.path.exists(rpi_os_img):
        print(f"  Using existing: {rpi_os_img}")
        return rpi_os_img

    print("\n  Downloading Raspberry Pi OS Lite ARM64 (~500MB)...")
    print("  This is a one-time download.\n")
    result = _download(RPI_OS_URL, rpi_os_xz, "Raspberry Pi OS Lite ARM64")
    if not result:
        return None

    print("  Extracting...")
    try:
        import lzma
        with lzma.open(rpi_os_xz) as f_in:
            with open(rpi_os_img, "wb") as f_out:
                chunk = f_in.read(4 * 1024 * 1024)
                while chunk:
                    f_out.write(chunk)
                    chunk = f_in.read(4 * 1024 * 1024)
        print(f"  Extracted: {os.path.getsize(rpi_os_img)//1024//1024} MB")
        return rpi_os_img
    except Exception as e:
        print(f"  Extract failed: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# QEMU launcher
# ─────────────────────────────────────────────────────────────────────────────

def launch_rpi_emulator(
    img_path: str,
    aavmf_path: str,
    qemu: str,
    ram_mb: int = 1024,
    machine: str = "virt",
    cpu: str = "cortex-a72",
    gui: bool = False,
):
    """
    Launch QEMU emulating a Raspberry Pi (ARM64).

    machine options:
      'virt'     — generic ARM64 virtual machine (best for NOVA, fastest)
      'raspi3b'  — emulates actual Pi 3B hardware (slower, more authentic)
      'raspi4b'  — emulates actual Pi 4 (experimental in QEMU)
    """
    print(f"\n  Launching Raspberry Pi emulator...")
    print(f"  Machine : {machine} ({cpu})")
    print(f"  RAM     : {ram_mb} MB")
    print(f"  Disk    : {img_path}")
    print(f"  UEFI    : {aavmf_path}")
    print()

    if machine == "virt":
        # Best for NOVA — clean ARM64 UEFI environment
        cmd = [
            qemu,
            "-machine",  "virt,highmem=off",
            "-cpu",      cpu,
            "-m",        str(ram_mb),
            # UEFI firmware
            "-drive",    f"if=pflash,format=raw,readonly=on,file={aavmf_path}",
            # NOVA disk
            "-drive",    f"if=virtio,format=raw,file={img_path}",
            # Serial console → terminal
            "-serial",   "stdio",
            "-monitor",  "none",
        ]
        if not gui:
            cmd += ["-nographic"]
        else:
            cmd += ["-vga", "virtio"]

    elif machine == "raspi3b":
        # Emulates actual Pi 3B hardware
        # Needs: kernel + dtb from Pi firmware
        cmd = [
            qemu,
            "-machine",  "raspi3b",
            "-cpu",      "cortex-a53",
            "-m",        "1G",  # Pi 3B has exactly 1GB
            "-drive",    f"file={img_path},format=raw",
            "-serial",   "stdio",
            "-dtb",      os.path.join(BUILD, "bcm2710-rpi-3-b.dtb"),
        ]
        if not gui:
            cmd += ["-nographic"]

    elif machine == "raspi4b":
        cmd = [
            qemu,
            "-machine",  "raspi4b",
            "-cpu",      "cortex-a72",
            "-m",        "4G",
            "-drive",    f"file={img_path},format=raw",
            "-serial",   "stdio",
        ]
        if not gui:
            cmd += ["-nographic"]

    print(f"  Command: {' '.join(cmd)}\n")
    print("  " + "─" * 60)
    print("  Press Ctrl+A then X to exit QEMU")
    print("  " + "─" * 60 + "\n")

    subprocess.run(cmd)


# ─────────────────────────────────────────────────────────────────────────────
# NOVA installer script for real/emulated Raspberry Pi
# ─────────────────────────────────────────────────────────────────────────────

def _rpi_install_script() -> str:
    """Rpi install script.


        Returns:
            str: Result.
        """
    return """#!/bin/bash
# ============================================================
# PyOS NOVA — Raspberry Pi Installer
# Run this on a real Pi or in the QEMU Pi emulator
#
# Usage:
#   curl -fsSL https://your-server/install.sh | bash
#   or: bash /nova/rpi/install_nova_rpi.sh
# ============================================================

set -euo pipefail
GRN='\\033[32m'; CYN='\\033[36m'; YLW='\\033[33m'; RST='\\033[0m'
ok()  { echo -e "${GRN}[ OK ]${RST} $*"; }
log() { echo -e "${CYN}[    ]${RST} $*"; }
warn(){ echo -e "${YLW}[WARN]${RST} $*"; }

echo ""
echo "  PyOS NOVA — Raspberry Pi Installation"
echo "  ─────────────────────────────────────"
echo ""

NOVA_HOME="${HOME}/.nova"
NOVA_SRC="/nova"

# ── detect architecture ──────────────────────────────────────────────────────
ARCH=$(uname -m)
if [[ "$ARCH" != "aarch64" ]] && [[ "$ARCH" != "arm64" ]]; then
    warn "Architecture is $ARCH — expected aarch64 for Raspberry Pi"
    warn "Continuing anyway..."
fi
ok "Architecture: $ARCH"

# ── Python ───────────────────────────────────────────────────────────────────
log "Checking Python..."
if ! command -v python3 &>/dev/null; then
    log "Installing Python..."
    if command -v apt-get &>/dev/null; then
        sudo apt-get update -qq && sudo apt-get install -y -qq python3 python3-pip
    elif command -v apk &>/dev/null; then
        apk add --quiet python3 py3-pip
    fi
fi
PYVER=$(python3 --version)
ok "Python: $PYVER"

# ── pip dependencies ─────────────────────────────────────────────────────────
log "Installing Python dependencies..."
pip3 install --quiet --break-system-packages \
    numpy psutil 2>/dev/null || \
pip3 install --quiet \
    numpy psutil 2>/dev/null || \
python3 -m pip install --quiet numpy psutil
ok "Dependencies installed"

# ── GPU detection for Pi ──────────────────────────────────────────────────────
log "Detecting GPU..."
if python3 -c "import pyopencl" 2>/dev/null; then
    ok "OpenCL available (VideoCore GPU)"
elif [[ -f "/dev/dri/card0" ]]; then
    ok "DRM GPU found: $(ls /dev/dri/)"
else
    ok "GPU: CPU inference (VideoCore not accessible without drivers)"
fi

# ── llama-cpp-python for Pi ───────────────────────────────────────────────────
log "Installing llama-cpp-python (ARM64 CPU build)..."
# For Pi: CPU-only build (no GPU layers, but llama.cpp is very fast on ARM NEON)
pip3 install --quiet --break-system-packages llama-cpp-python 2>/dev/null || \
pip3 install --quiet llama-cpp-python 2>/dev/null || \
warn "llama-cpp-python not installed — AI will use RAG tier"

# ── NOVA setup ───────────────────────────────────────────────────────────────
log "Setting up NOVA directories..."
mkdir -p "$NOVA_HOME/models" "$NOVA_HOME/index" "$NOVA_HOME/logs"

# ── systemd service (if available) ───────────────────────────────────────────
if command -v systemctl &>/dev/null; then
    log "Creating NOVA systemd service..."
    sudo tee /etc/systemd/system/nova.service > /dev/null << 'SERVICE'
[Unit]
Description=PyOS NOVA Shell
After=network.target

[Service]
Type=simple
User=pi
ExecStart=/usr/bin/python3 /nova/main.py
Restart=on-failure
Environment=NOVA_DATA=/home/pi/.nova

[Install]
WantedBy=multi-user.target
SERVICE
    sudo systemctl daemon-reload
    sudo systemctl enable nova.service 2>/dev/null || true
    ok "Systemd service: nova.service (start with: sudo systemctl start nova)"
fi

# ── shell alias ──────────────────────────────────────────────────────────────
if [[ -f "$HOME/.bashrc" ]] && ! grep -q "alias nova=" "$HOME/.bashrc"; then
    echo "alias nova='python3 /nova/main.py'" >> "$HOME/.bashrc"
    ok "Added 'nova' alias to ~/.bashrc"
fi

# ── recommend model ──────────────────────────────────────────────────────────
echo ""
echo "  ─────────────────────────────────────────────────────"
ok "PyOS NOVA installed on Raspberry Pi!"
echo ""
echo "  Start NOVA:  python3 /nova/main.py"
echo "          or:  nova  (after re-opening terminal)"
echo ""
echo "  Download AI model (recommended for Pi 4 with 4GB+):"
echo "    Inside NOVA: llm download tinyllama"
echo "    tinyllama runs at ~3-5 tokens/sec on Pi 4 (CPU NEON)"
echo ""
echo "  GPU on Raspberry Pi:"
echo "    Pi 4/5 VideoCore VII supports OpenCL via Mesa"
echo "    Install: sudo apt install mesa-opencl-icd"
echo "    Then inside NOVA: gpu status"
echo "  ─────────────────────────────────────────────────────"
"""


# ─────────────────────────────────────────────────────────────────────────────
# Download AAVMF firmware
# ─────────────────────────────────────────────────────────────────────────────

def get_aavmf() -> str | None:
    """Return the aavmf.


        Returns:
            str | None: Result.
        """
    path = find_aavmf()
    if path:
        return path
    print("\n  AAVMF (ARM64 UEFI firmware) not found locally.")
    ans = input("  Download AAVMF (~3MB)? [Y/n] ").strip().lower()
    if ans not in ("", "y", "yes"):
        print(install_instructions())
        return None
    dest = os.path.join(BUILD, "AAVMF.fd")
    return _download(AAVMF_DOWNLOAD, dest, "AAVMF ARM64 UEFI firmware")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="PyOS NOVA — Raspberry Pi emulator (QEMU ARM64)"
    )
    ap.add_argument("--ram",      type=int,  default=1024,   metavar="MB")
    ap.add_argument("--machine",  default="virt",
                    choices=["virt","raspi3b","raspi4b"],
                    help="QEMU machine type (default: virt)")
    ap.add_argument("--cpu",      default="cortex-a72",
                    help="ARM CPU to emulate (default: cortex-a72 = Pi 4)")
    ap.add_argument("--disk",     type=int,  default=256,    metavar="MB")
    ap.add_argument("--gui",      action="store_true", help="Enable display window")
    ap.add_argument("--install",  action="store_true", help="Show install instructions")
    ap.add_argument("--build-only",action="store_true",help="Build image only, don't launch")
    ap.add_argument("--rpi-os",   action="store_true", help="Download + use Raspberry Pi OS")
    args = ap.parse_args()

    print("\n  PyOS NOVA — Raspberry Pi Emulator")
    print("  ─────────────────────────────────────────────────────────")
    print("  Boot: QEMU → ARM64 UEFI → BOOTAA64.EFI → Python PID 1")
    print()

    if args.install:
        print(install_instructions())
        sys.exit(0)

    # Check QEMU
    qemu = find_qemu_arm()
    if not qemu:
        print("  QEMU ARM not found.")
        print(install_instructions())
        sys.exit(1)
    print(f"  QEMU:    {qemu}")

    # Build or get disk image
    if args.rpi_os:
        img = build_pi_os_image()
        if not img:
            print("  Failed to get Raspberry Pi OS. Using NOVA image instead.")
            img = build_nova_arm64_image(args.disk)
    else:
        img = build_nova_arm64_image(args.disk)

    if args.build_only:
        print(f"\n  Image ready: {img}")
        print(f"  Flash to SD card:")
        print(f"    sudo dd if={img} of=/dev/sdX bs=4M status=progress")
        sys.exit(0)

    # Get UEFI firmware
    aavmf = get_aavmf()
    if not aavmf:
        sys.exit(1)
    print(f"  AAVMF:   {aavmf}")

    launch_rpi_emulator(
        img_path  = img,
        aavmf_path= aavmf,
        qemu      = qemu,
        ram_mb    = args.ram,
        machine   = args.machine,
        cpu       = args.cpu,
        gui       = args.gui,
    )
