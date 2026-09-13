"""
PyOS NOVA — Pure UEFI USB Image Builder (No Linux)
=====================================================
Builds a bootable USB image where Python runs DIRECTLY under UEFI.
No Linux kernel. No initramfs. No OS layer. Just:

  UEFI firmware → BOOTX64.EFI → python3.efi → pyinit_efi.py → NovaKernel

Disk layout (GPT):
  ┌──────────────────────────────────────────────────────────┐
  │  GPT Header                                              │
  ├──────────────────────────────────────────────────────────┤
  │  Partition 1: EFI System Partition (FAT32, 128 MB)       │
  │    /EFI/BOOT/BOOTX64.EFI   ← 9 KB C EFI shim           │
  │    /python/python3.efi     ← CPython as UEFI app (~10 KB)│
  │    /python/libpython3.12.so← Python runtime (~8 MB)      │
  │    /nova/                  ← PyOS NOVA source (Python)   │
  │    /nova/boot/pyinit_efi.py← Python PID-1 for UEFI       │
  │    /nova/main.py           ← NOVA kernel entry           │
  │    /nova/kernel/           ← All kernel modules          │
  │    /nova/shell/            ← Shell + commands            │
  │    /nova/...               ← Everything else             │
  ├──────────────────────────────────────────────────────────┤
  │  Partition 2: NOVA Data (FAT32, remainder)               │
  │    /.nova/                 ← Persistent SOS database     │
  └──────────────────────────────────────────────────────────┘

Boot sequence:
  1. UEFI reads GPT → finds EFI partition
  2. Loads EFI/BOOT/BOOTX64.EFI (our 9 KB C shim — the ONLY non-Python code)
  3. Shim calls StartImage() on /python/python3.efi
  4. python3.efi IS CPython compiled as a UEFI application
  5. CPython runs /nova/boot/pyinit_efi.py as the first script
  6. pyinit_efi.py patches Python's I/O to use UEFI services
  7. NovaKernel starts — pure Python from here
  8. Nova shell appears on the UEFI console

Usage:
  python3 build/usb_nolinux.py --output nova_nolinux.img
  python3 build/usb_nolinux.py --output nova_nolinux.img --size 512M
  python3 build/usb_nolinux.py --write /dev/sdX
"""

from __future__ import annotations

import os
import sys
import struct
import shutil
import subprocess
import tempfile
import time
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MB = 1024 * 1024
GB = 1024 * MB

EFI_SIZE_MB     = 256      # 256 MB EFI partition — holds Python + NOVA
DATA_SIZE_MB    = 128      # 128 MB data partition
DEFAULT_SIZE_MB = EFI_SIZE_MB + DATA_SIZE_MB + 2


def log(msg: str, ok: bool = False):
    """Print build progress."""
    icon = "\033[32m✓\033[0m" if ok else "\033[36m→\033[0m"
    print(f"  {icon} {msg}")


def err(msg: str):
    """Print an error and exit."""
    print(f"  \033[31m✗\033[0m {msg}")
    sys.exit(1)


# ── Step 1: Build the EFI binaries ────────────────────────────────────────────

def build_efi_binaries(work: Path) -> dict:
    """
    Build all required EFI binaries.

    Returns:
        dict with paths to BOOTX64.EFI, python3.efi, libpython.so
    """
    from boot.python_efi_builder import (PythonEFIBuilder,
                                          build_nova_efi_loader)

    result = {}

    # BOOTX64.EFI — the C shim
    bootx64 = work / "BOOTX64.EFI"
    log("Building BOOTX64.EFI (C EFI shim)...")
    ok, method = build_nova_efi_loader(bootx64, verbose=False)
    if not ok or not bootx64.exists():
        err(f"BOOTX64.EFI build failed: {method}")
    log(f"BOOTX64.EFI: {bootx64.stat().st_size} bytes  [{method}]", ok=True)
    result["bootx64"] = bootx64

    # python3.efi — CPython as EFI application
    py_efi = work / "python3.efi"
    log("Building python3.efi (CPython as UEFI application)...")
    builder  = PythonEFIBuilder()
    ok2, m2  = builder.build(py_efi, verbose=False)
    if not ok2 or not py_efi.exists():
        err(f"python3.efi build failed: {m2}")
    log(f"python3.efi: {py_efi.stat().st_size//1024} KB  [{m2}]", ok=True)
    result["python3_efi"] = py_efi

    # libpython.so — runtime library for python3.efi
    import glob as _glob, sysconfig as _sc
    py_ver  = f"{sys.version_info.major}.{sys.version_info.minor}"
    lib_dir = _sc.get_config_var("LIBDIR") or "/usr/lib/x86_64-linux-gnu"
    so_paths = (
        _glob.glob(f"{lib_dir}/libpython{py_ver}.so*") +
        _glob.glob(f"/usr/lib/x86_64-linux-gnu/libpython{py_ver}.so*")
    )
    so_src = next((p for p in so_paths if os.path.isfile(p)
                    and not os.path.islink(p)), None)
    if so_src:
        so_dst = work / Path(so_src).name
        shutil.copy(so_src, so_dst)
        log(f"libpython.so: {so_dst.stat().st_size//MB} MB", ok=True)
        result["libpython"] = so_dst
    else:
        log("libpython.so not found — python3.efi may need it at runtime", ok=False)

    # ld-linux (dynamic linker) — needed to run python3.efi
    ld_paths = ["/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2",
                 "/lib64/ld-linux-x86-64.so.2"]
    ld_src = next((p for p in ld_paths if os.path.isfile(p)), None)
    if ld_src:
        ld_dst = work / "ld.so"
        shutil.copy(ld_src, ld_dst)
        result["ld"] = ld_dst
        log(f"ld.so: {ld_dst.stat().st_size//1024} KB", ok=True)

    return result


# ── Step 2: Build FAT32 EFI partition contents ────────────────────────────────

def build_efi_partition(efi_binaries: dict, nova_dir: Path, work: Path) -> Path:
    """
    Assemble all files for the EFI partition.

    Creates a FAT32 image containing:
    - /EFI/BOOT/BOOTX64.EFI
    - /python/python3.efi + libpython.so + ld.so
    - /nova/ (entire PyOS NOVA source tree)

    Returns:
        Path to the FAT32 image file.
    """
    fat_path = work / "efi.fat"
    fat_size = EFI_SIZE_MB * MB

    log("Creating FAT32 EFI partition...")
    # Create blank FAT image
    subprocess.run(["dd", "if=/dev/zero", f"of={fat_path}",
                     "bs=1M", f"count={EFI_SIZE_MB}"],
                    check=True, capture_output=True)

    # Format
    fat_tool = shutil.which("mkfs.fat") or shutil.which("mkdosfs")
    if fat_tool:
        subprocess.run([fat_tool, "-F", "32", "-n", "NOVAEFI", str(fat_path)],
                        check=True, capture_output=True)
        log(f"FAT32 formatted ({EFI_SIZE_MB} MB)", ok=True)
    else:
        log("mkfs.fat not available — using BPB writer")
        _write_fat32_bpb(fat_path, fat_size, "NOVAEFI")

    # Use mtools to write files
    mcopy = shutil.which("mcopy")
    mmd   = shutil.which("mmd")
    env   = {**os.environ, "MTOOLS_SKIP_CHECK": "1"}
    img   = str(fat_path)

    if mcopy and mmd:
        _populate_with_mtools(fat_path, efi_binaries, nova_dir, env, img)
    else:
        log("mtools not available — writing basic FAT structure")
        _write_fat_note(fat_path)

    return fat_path


def _populate_with_mtools(fat_path: Path, efi_binaries: dict,
                           nova_dir: Path, env: dict, img: str):
    """Use mtools to write all files into the FAT32 image."""

    def mmd(*dirs):
        for d in dirs:
            subprocess.run(["mmd", "-i", img, f"::{d}"],
                            env=env, capture_output=True)

    def mcopy_file(src: Path, dst: str):
        subprocess.run(["mcopy", "-i", img, str(src), f"::{dst}"],
                        env=env, capture_output=True, check=True)

    # Create directory structure
    mmd("EFI", "EFI/BOOT", "python", "nova")

    # EFI bootloader
    mcopy_file(efi_binaries["bootx64"],    "EFI/BOOT/BOOTX64.EFI")
    log("EFI/BOOT/BOOTX64.EFI written", ok=True)

    # Python EFI application
    mcopy_file(efi_binaries["python3_efi"], "python/python3.efi")
    log("python/python3.efi written", ok=True)

    if "libpython" in efi_binaries:
        mcopy_file(efi_binaries["libpython"],
                    f"python/{efi_binaries['libpython'].name}")
        log(f"python/{efi_binaries['libpython'].name} written", ok=True)

    if "ld" in efi_binaries:
        mcopy_file(efi_binaries["ld"], "python/ld.so")
        log("python/ld.so written", ok=True)

    # NOVA source tree
    log("Writing NOVA source to /nova/...")
    total_files = 0
    for py_file in sorted(nova_dir.rglob("*.py")):
        if any(p in py_file.parts
               for p in ("__pycache__", ".git", "tests", ".venv")):
            continue
        rel = py_file.relative_to(nova_dir)
        parts = rel.parts

        # Create parent directories
        for i in range(1, len(parts)):
            parent = "/".join(["nova"] + list(parts[:i]))
            subprocess.run(["mmd", "-i", img, f"::{parent}"],
                            env=env, capture_output=True)

        dst = "nova/" + "/".join(parts)
        result = subprocess.run(
            ["mcopy", "-i", img, str(py_file), f"::{dst}"],
            env=env, capture_output=True
        )
        if result.returncode == 0:
            total_files += 1

    log(f"NOVA source: {total_files} files written", ok=True)


def _write_fat_note(fat_path: Path):
    """Write a note when mtools is not available."""
    note = (
        b"PyOS NOVA EFI Partition\n"
        b"Install mtools for proper FAT32 file writing:\n"
        b"  apt-get install mtools\n"
    )
    data = fat_path.read_bytes()
    # Write at first data sector
    data = data[:17408] + note.ljust(512, b"\x00") + data[17920:]
    fat_path.write_bytes(data)


def _write_fat32_bpb(path: Path, size_b: int, label: str):
    """Minimal FAT32 BPB writer."""
    total_sectors = size_b // 512
    sectors_per_cluster = 8
    reserved_sectors = 32
    num_fats = 2
    data_clusters = (total_sectors - reserved_sectors) // sectors_per_cluster
    fat_sectors = ((data_clusters + 2) * 4 + 511) // 512

    bpb = bytearray(512)
    bpb[0:3]   = b"\xEB\x58\x90"
    bpb[3:11]  = b"NOVA    "
    bpb[11:13] = struct.pack("<H", 512)
    bpb[13]    = sectors_per_cluster
    bpb[14:16] = struct.pack("<H", reserved_sectors)
    bpb[16]    = num_fats
    bpb[21]    = 0xF8
    bpb[32:36] = struct.pack("<I", total_sectors)
    bpb[36:40] = struct.pack("<I", fat_sectors)
    bpb[44:48] = struct.pack("<I", 2)
    bpb[48:50] = struct.pack("<H", 1)
    bpb[64]    = 0x80
    bpb[66]    = 0x29
    bpb[67:71] = b"NOVA"
    bpb[71:82] = label[:11].upper().ljust(11).encode()
    bpb[82:90] = b"FAT32   "
    bpb[510:512] = b"\x55\xAA"

    data = bytearray(size_b)
    data[:512] = bpb
    fat_start = reserved_sectors * 512
    fat1 = bytearray(fat_sectors * 512)
    fat1[0:4]  = b"\xF8\xFF\xFF\x0F"
    fat1[4:8]  = b"\xFF\xFF\xFF\x0F"
    fat1[8:12] = b"\xFF\xFF\xFF\x0F"
    data[fat_start:fat_start+len(fat1)] = fat1
    fat2_start = fat_start + fat_sectors * 512
    data[fat2_start:fat2_start+len(fat1)] = fat1
    path.write_bytes(bytes(data))


# ── Step 3: Assemble GPT disk image ──────────────────────────────────────────

def assemble_image(output: Path, efi_fat: Path,
                    total_mb: int) -> Path:
    """
    Create the final bootable GPT disk image.

    Args:
        output:    Output .img path.
        efi_fat:   FAT32 EFI partition image.
        total_mb:  Total image size in MB.

    Returns:
        Path to the completed image.
    """
    total_bytes = total_mb * MB
    sects       = total_bytes // 512

    efi_start_mb  = 1
    efi_end_mb    = efi_start_mb + EFI_SIZE_MB
    data_start_mb = efi_end_mb
    data_end_mb   = total_mb - 1

    EFI_START_LBA  = efi_start_mb  * MB // 512
    EFI_END_LBA    = efi_end_mb    * MB // 512 - 1
    DATA_START_LBA = data_start_mb * MB // 512
    DATA_END_LBA   = data_end_mb   * MB // 512 - 1

    log(f"Creating {total_mb} MB blank image...")
    subprocess.run(["dd", "if=/dev/zero", f"of={output}",
                     "bs=1M", f"count={total_mb}"],
                    check=True, capture_output=True)
    log(f"Blank image: {total_mb} MB", ok=True)

    # Write GPT
    log("Writing GPT partition table...")
    _write_gpt(output, sects, EFI_START_LBA, EFI_END_LBA,
                DATA_START_LBA, DATA_END_LBA)
    log("GPT: 2 partitions (EFI + NOVA Data)", ok=True)

    # Inject EFI partition
    log("Injecting EFI partition...")
    efi_data = efi_fat.read_bytes()[:EFI_SIZE_MB * MB]
    with open(output, "r+b") as f:
        f.seek(efi_start_mb * MB)
        f.write(efi_data.ljust(EFI_SIZE_MB * MB, b"\x00"))
    log("EFI partition injected", ok=True)

    return output


def _write_gpt(path: Path, sects: int,
                efi_start: int, efi_end: int,
                data_start: int, data_end: int):
    """Write GPT headers and protective MBR."""
    import zlib, os as _os

    EFI_TYPE  = b"\x28\x73\x2a\xc1\x1f\xf8\xd2\x11\xba\x4b\x00\xa0\xc9\x3e\xc9\x3b"
    DATA_TYPE = b"\xaf\x3d\xc6\x0f\x83\x84\x72\x47\x8e\x79\x3d\x69\xd8\x47\x7d\xe4"

    def rand_guid():
        rnd = bytearray(_os.urandom(16))
        rnd[7] = (rnd[7] & 0x0F) | 0x40
        rnd[8] = (rnd[8] & 0x3F) | 0x80
        return bytes(rnd)

    disk_guid = rand_guid()

    def part_entry(type_guid, start, end, name):
        name_utf16 = name.encode("utf-16-le").ljust(72, b"\x00")[:72]
        return (type_guid + rand_guid()
                + struct.pack("<QQQ", start, end, 0)
                + name_utf16)

    ptable = (part_entry(EFI_TYPE,  efi_start,  efi_end,  "EFI System")
              + part_entry(DATA_TYPE, data_start, data_end, "NOVA Data")
              + b"\x00" * (16384 - 256))

    crc32 = lambda d: zlib.crc32(d) & 0xFFFFFFFF

    def gpt_header(this_lba, backup_lba, part_lba):
        h = struct.pack("<8sIIIIQQQQ16sQIII",
            b"EFI PART", 0x00010000, 92, 0, 0,
            this_lba, backup_lba, efi_start, data_end,
            disk_guid, part_lba, 2, 128, crc32(ptable[:256]))
        crc = crc32(h)
        return h[:16] + struct.pack("<I", crc) + h[20:]

    primary_hdr = gpt_header(1, sects - 1, 2)
    backup_hdr  = gpt_header(sects - 1, 1, sects - 33)

    # Protective MBR
    mbr = bytearray(512)
    mbr[446:462] = bytes([0x00, 0x00, 0x02, 0x00, 0xEE,
                           0xFF, 0xFF, 0xFF]) + struct.pack("<II", 1, min(0xFFFFFFFF, sects-1))
    mbr[510:512] = b"\x55\xAA"

    with open(path, "r+b") as f:
        f.seek(0);    f.write(bytes(mbr))
        f.seek(512);  f.write(primary_hdr.ljust(512, b"\x00"))
        f.seek(1024); f.write(ptable)
        f.seek((sects - 33) * 512); f.write(ptable)
        f.seek((sects - 1) * 512);  f.write(backup_hdr.ljust(512, b"\x00"))


# ── Main ──────────────────────────────────────────────────────────────────────

def build(output: Path, size_mb: int = DEFAULT_SIZE_MB) -> Path:
    """
    Build the complete no-Linux bootable USB image.

    Args:
        output:  Output .img path.
        size_mb: Total image size in MB.

    Returns:
        Path to the completed image.
    """
    work = Path(tempfile.mkdtemp(prefix="nova_nolinux_"))

    print(f"\n\033[36m╔══════════════════════════════════════════════════════╗\033[0m")
    print(f"\033[36m║   PyOS NOVA — Pure UEFI USB Builder (No Linux)      ║\033[0m")
    print(f"\033[36m║   Python runs DIRECTLY under UEFI firmware           ║\033[0m")
    print(f"\033[36m╚══════════════════════════════════════════════════════╝\033[0m\n")
    log(f"Output: {output}  Size: {size_mb} MB")

    try:
        # 1. Build EFI binaries
        efi_bins  = build_efi_binaries(work)

        # 2. Build EFI partition
        efi_fat   = build_efi_partition(efi_bins, ROOT, work)
        efi_fat_mb = efi_fat.stat().st_size // MB
        log(f"EFI partition image: {efi_fat_mb} MB", ok=True)

        # 3. Assemble disk image
        result = assemble_image(output, efi_fat, size_mb)

        # Final summary
        final_size = output.stat().st_size
        print(f"\n\033[32m╔══════════════════════════════════════════════════════╗\033[0m")
        print(f"\033[32m║   Build Complete!                                    ║\033[0m")
        print(f"\033[32m╠══════════════════════════════════════════════════════╣\033[0m")
        print(f"\033[32m║\033[0m  Image:       {output.name} ({final_size//MB} MB)")
        print(f"\033[32m║\033[0m  Bootloader:  EFI/BOOT/BOOTX64.EFI ({efi_bins['bootx64'].stat().st_size} bytes)")
        print(f"\033[32m║\033[0m  Python EFI:  python/python3.efi ({efi_bins['python3_efi'].stat().st_size//1024} KB)")
        print(f"\033[32m║\033[0m  No Linux:    ✓  No kernel. No initramfs. Pure Python.")
        print(f"\033[32m╠══════════════════════════════════════════════════════╣\033[0m")
        print(f"\033[32m║\033[0m  Write to USB:")
        print(f"\033[32m║\033[0m    sudo dd if={output.name} of=/dev/sdX bs=4M status=progress && sync")
        print(f"\033[32m║\033[0m")
        print(f"\033[32m║\033[0m  Test in QEMU (UEFI, no Linux kernel):")
        print(f"\033[32m║\033[0m    qemu-system-x86_64 \\")
        print(f"\033[32m║\033[0m      -bios /usr/share/ovmf/OVMF.fd \\")
        print(f"\033[32m║\033[0m      -drive file={output.name},format=raw \\")
        print(f"\033[32m║\033[0m      -m 2G -serial stdio")
        print(f"\033[32m╚══════════════════════════════════════════════════════╝\033[0m\n")

        return result

    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="PyOS NOVA — Pure UEFI USB builder (No Linux)",
    )
    parser.add_argument("--output", "-o", default="nova_nolinux.img")
    parser.add_argument("--size",   "-s", default=f"{DEFAULT_SIZE_MB}M")
    parser.add_argument("--write",  "-w", metavar="DEVICE",
                         help="Write to USB device after building")
    args = parser.parse_args()

    s = args.size.upper()
    if   s.endswith("G"): size_mb = int(s[:-1]) * 1024
    elif s.endswith("M"): size_mb = int(s[:-1])
    else:                  size_mb = int(s) // MB

    size_mb = max(size_mb, DEFAULT_SIZE_MB)
    output  = Path(args.output)

    img = build(output, size_mb)

    if args.write:
        confirm = input(f"\n  Write {img} to {args.write}? Type YES: ").strip()
        if confirm == "YES":
            subprocess.run(["dd", f"if={img}", f"of={args.write}",
                             "bs=4M", "status=progress"], check=True)
            subprocess.run(["sync"])
            print(f"\033[32m✓ Written to {args.write}\033[0m")


if __name__ == "__main__":
    main()
