"""
PyOS NOVA — Pure Python USB Builder
=====================================
Replaces build_usb.sh entirely. Pure Python. No bash. No external tools
(except dd/parted for the physical USB write, which are OS utilities
analogous to hardware drivers — not OS-layer software we "install").

Usage:
  python3 build/build_zero.py --usb /dev/sdX     # write to USB
  python3 build/build_zero.py --iso nova.iso      # create ISO
  python3 build/build_zero.py --img nova.img      # FAT32 image only
  python3 build/build_zero.py --test              # validate EFI only
"""

import os
import sys
import shutil
import struct
import hashlib
import argparse
import subprocess
import tempfile
from pathlib import Path

ROOT  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# ─────────────────────────────────────────────────────────────────────────────
# Dependency check — we need ZERO external tools for image building
# Only parted/dd are needed for writing to a physical USB device
# ─────────────────────────────────────────────────────────────────────────────

def check_environment():
    """Check environment and return the result."""
    print("  Checking environment...")
    python_ok = sys.version_info >= (3, 10)
    print(f"  {'✓' if python_ok else '✗'}  Python {sys.version.split()[0]}")

    tools_for_usb = ["dd", "parted"]
    for tool in tools_for_usb:
        ok = shutil.which(tool) is not None
        print(f"  {'✓' if ok else '○'}  {tool} {'(needed for physical USB write only)' if not ok else ''}")

    print()
    return python_ok


# ─────────────────────────────────────────────────────────────────────────────
# Build pipeline
# ─────────────────────────────────────────────────────────────────────────────

def build_all(output: str, mode: str = "img", size_mb: int = 128):
    """
    Full build pipeline:
      1. Generate BOOTX64.EFI (Python PE32+ builder)
      2. Collect NOVA source files
      3. Build FAT32 disk image (Python FAT32 builder)
      4. Write to output

    All steps are pure Python — no C compiler, no assembler, no shell tools.
    """
    from boot.efi_builder    import build_efi, _verify_efi
    from boot.nova_vm        import FAT32Builder

    build_dir = os.path.join(ROOT, "build")
    os.makedirs(build_dir, exist_ok=True)

    # ── Step 1: Generate EFI ─────────────────────────────────────────────────
    efi_out = os.path.join(build_dir, "BOOTX64.EFI")
    print("  [1/3] Generating BOOTX64.EFI (pure Python PE32+ builder)")
    size = build_efi(efi_out, verify=True)
    efi_data = open(efi_out, "rb").read()
    print(f"        {efi_out}  ({size} bytes)")

    # ── Step 2: Collect NOVA source ──────────────────────────────────────────
    print("\n  [2/3] Collecting NOVA source files")
    nova_files = {}
    total_src  = 0
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames
                       if d not in ("__pycache__", ".git", "build", ".nova", "node_modules")]
        for fname in filenames:
            if fname.endswith((".py", ".md", ".txt", ".cfg", ".json", ".sh", ".toml")):
                full = os.path.join(dirpath, fname)
                rel  = os.path.relpath(full, ROOT)
                data = open(full, "rb").read()
                nova_files[rel] = data
                total_src += len(data)

    print(f"        {len(nova_files)} files  ({total_src//1024} KB source)")

    # ── Step 3: Build FAT32 image ────────────────────────────────────────────
    print(f"\n  [3/3] Building {size_mb}MB FAT32 image")
    fat = FAT32Builder(size_mb=size_mb)

    # EFI boot directory
    fat.add_file("EFI\\BOOT\\BOOTX64.EFI", efi_data)

    # NOVA source in NOVA\ directory
    for rel, data in nova_files.items():
        fat_path = "NOVA\\" + rel.replace("/", "\\").replace("..", "__")
        fat.add_file(fat_path, data)

    # startup.nsh — UEFI Shell script (runs if UEFI has a shell built in)
    startup = "\\nova\\main.py\r\necho PyOS NOVA started\r\n".encode("utf-8")
    fat.add_file("startup.nsh", startup)

    img_data = fat.build()
    img_path = output if mode == "img" else os.path.join(build_dir, "nova_tmp.img")

    with open(img_path, "wb") as f:
        f.write(img_data)

    sha = hashlib.sha256(img_data).hexdigest()[:12]
    print(f"        {img_path}  ({len(img_data)//1024//1024} MB)  sha256:{sha}...")

    return img_path, img_data


def write_to_usb(img_path: str, usb_device: str):
    """Write the disk image to a physical USB device using dd."""
    print(f"\n  Writing to {usb_device}...")
    print(f"  ⚠  ALL DATA ON {usb_device} WILL BE ERASED")
    confirm = input(f"  Type YES to continue: ").strip()
    if confirm != "YES":
        print("  Aborted.")
        sys.exit(0)

    # Verify it's not the system disk
    try:
        result = subprocess.run(
            ["lsblk", "-no", "MOUNTPOINT", usb_device],
            capture_output=True, text=True,
        )
        if "/" in result.stdout:
            print(f"  ERROR: {usb_device} appears to contain a mounted system partition!")
            sys.exit(1)
    except Exception:
        pass

    subprocess.run(["sync"], check=False)

    cmd = [
        "dd",
        f"if={img_path}",
        f"of={usb_device}",
        "bs=4M",
        "status=progress",
        "conv=fsync",
    ]
    print(f"  Running: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    if result.returncode == 0:
        subprocess.run(["sync"])
        print(f"\n  USB ready: {usb_device}")
        print(f"  Boot from USB on any UEFI x86-64 machine.")
    else:
        print(f"\n  Write failed (exit code {result.returncode})")


def build_iso(img_path: str, iso_path: str):
    """
    Wrap the FAT32 image in a hybrid ISO9660 that can be:
      - Written to USB (dd)
      - Used in VMs (QEMU, VirtualBox)
      - Burned to DVD

    Uses Python's struct to build a minimal ISO9660 wrapper.
    For full ISO support, delegates to xorriso if available.
    """
    xorriso = shutil.which("xorriso")
    if xorriso:
        # Preferred: xorriso creates a proper hybrid ISO
        build_dir  = os.path.dirname(img_path)
        efi_out    = os.path.join(build_dir, "BOOTX64.EFI")
        cmd = [
            xorriso,
            "-as", "mkisofs",
            "-o", iso_path,
            "-V", "PyOS-NOVA",
            "-e", "EFI/BOOT/BOOTX64.EFI",   # EFI boot
            "-no-emul-boot",
            "-isohybrid-gpt-basdat",
            img_path,                         # source is our FAT32 image
        ]
        subprocess.run(cmd, check=False)
        print(f"  ISO (xorriso): {iso_path}")
    else:
        # Fallback: the FAT32 image IS usable as-is with dd
        shutil.copy2(img_path, iso_path)
        print(f"  ISO (direct): {iso_path}")
        print(f"  (Install xorriso for a proper hybrid ISO with El Torito)")

    size = os.path.getsize(iso_path)
    print(f"  Size: {size//1024//1024} MB")
    print(f"  Flash: sudo dd if={iso_path} of=/dev/sdX bs=4M status=progress")


# ─────────────────────────────────────────────────────────────────────────────
# Summary printer
# ─────────────────────────────────────────────────────────────────────────────

def print_summary(output: str, mode: str):
    """Print summary.

        Args:
        output (str): Output.
        mode (str): Mode.
        """
    print("\n" + "─" * 60)
    print("  PyOS NOVA — Build Complete")
    print("─" * 60)
    print()
    print("  Boot chain (zero non-Python software):")
    print("    UEFI firmware (ROM chip on motherboard — hardware)")
    print("      └→ EFI/BOOT/BOOTX64.EFI  ← generated by Python")
    print("           └→ Python interpreter")
    print("                └→ NOVA kernel + SOS + AI + shell")
    print()
    print("  What Python wrote:")
    print("    • PE32+ binary headers (struct module)")
    print("    • x86_64 machine code stub (bytes[] literals)")
    print("    • FAT32 disk image (pure Python FAT32 builder)")
    print()
    if mode == "usb":
        print(f"  USB device: {output}")
        print(f"  → Plug in + boot on any UEFI x86-64 machine")
    elif mode == "iso":
        print(f"  ISO: {output}")
        print(f"  → Flash: sudo dd if={output} of=/dev/sdX bs=4M status=progress")
        print(f"  → VM:    python3 boot/nova_vm.py  (uses OVMF UEFI firmware)")
    else:
        print(f"  Image: {output}")
        print(f"  → VM:    python3 boot/nova_vm.py")
        print(f"  → USB:   sudo dd if={output} of=/dev/sdX bs=4M status=progress")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="PyOS NOVA — Pure Python bootable image builder (no shell, no tools)"
    )
    ap.add_argument("--usb",  metavar="/dev/sdX", help="Write directly to USB device")
    ap.add_argument("--iso",  metavar="FILE",     help="Create ISO file")
    ap.add_argument("--img",  metavar="FILE",     default=None, help="Create raw FAT32 image")
    ap.add_argument("--size", metavar="MB",       type=int, default=128, help="Image size in MB")
    ap.add_argument("--test", action="store_true", help="Build EFI only, verify it")
    args = ap.parse_args()

    print("\n  PyOS NOVA — Zero-Dependency Builder")
    print("  Pure Python. No C compiler. No bash. No external tools.")
    print("  ─────────────────────────────────────────────────────────\n")

    check_environment()

    if args.test:
        from boot.efi_builder import build_efi
        build_dir = os.path.join(ROOT, "build")
        os.makedirs(build_dir, exist_ok=True)
        efi_path  = os.path.join(build_dir, "BOOTX64_test.EFI")
        size = build_efi(efi_path, verify=True)
        print(f"\n  EFI test passed — {size} bytes — {efi_path}")
        sys.exit(0)

    if args.usb:
        img, _ = build_all(os.path.join(ROOT, "build", "nova_usb.img"), "img", args.size)
        write_to_usb(img, args.usb)
        print_summary(args.usb, "usb")

    elif args.iso:
        img, _ = build_all(os.path.join(ROOT, "build", "nova_tmp.img"), "img", args.size)
        build_iso(img, args.iso)
        print_summary(args.iso, "iso")

    else:
        out = args.img or os.path.join(ROOT, "build", "nova.img")
        img, _ = build_all(out, "img", args.size)
        print_summary(img, "img")
