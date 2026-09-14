"""
Update only the FAT boot partition of a DataPy USB stick.

Does NOT format or touch the NOVA_DATA partition. Requires an explicit
Windows drive letter that already looks like a DataPy boot volume.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REQUIRED = (
    "config.txt",
    "cmdline.txt",
    "kernel_2712.img",
    "initramfs_nova.gz",
)


def looks_like_datapy_boot(root: Path) -> bool:
    return all((root / name).is_file() for name in ("config.txt", "kernel_2712.img"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--drive",
        required=True,
        help="Windows drive letter of the FAT boot partition (e.g. D:)",
    )
    parser.add_argument(
        "--from-cache",
        default="downloads",
        help="Directory with rebuilt firmware/initramfs (default: downloads)",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Optional config.txt path (default: generate from builder)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive confirmation",
    )
    args = parser.parse_args()

    drive = args.drive.rstrip("\\/")
    if len(drive) == 1:
        drive = drive + ":"
    root = Path(drive + "\\")
    if not root.exists():
        print(f"[fail] drive not found: {root}", file=sys.stderr)
        return 1
    if not looks_like_datapy_boot(root):
        print(
            f"[fail] {root} does not look like a DataPy boot volume "
            "(need config.txt + kernel_2712.img). Refusing.",
            file=sys.stderr,
        )
        return 1

    cache = Path(args.from_cache).resolve()
    sources = {
        "kernel_2712.img": cache / "kernel_2712.img",
        "initramfs_nova.gz": cache / "initramfs_nova.gz",
        "start4.elf": cache / "start4.elf",
        "fixup4.dat": cache / "fixup4.dat",
        "bcm2712-rpi-5-b.dtb": cache / "bcm2712-rpi-5-b.dtb",
    }
    missing = [n for n, p in sources.items() if not p.is_file()]
    if missing:
        print(f"[fail] missing in cache: {missing}", file=sys.stderr)
        return 1

    # Prefer builder constants for config/cmdline so they match the image.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from build.build_pi5_nova import CMDLINE_TXT, CONFIG_TXT

    config_txt = (
        Path(args.config).read_text(encoding="utf-8")
        if args.config
        else CONFIG_TXT
    )
    cmdline_txt = CMDLINE_TXT

    print(f"Will update FAT boot only on {root}")
    print("Will NOT touch partition 2 (NOVA_DATA).")
    for name in sources:
        print(f"  {name}: {sources[name].stat().st_size} bytes")
    if not args.yes:
        ans = input("Type YES to copy boot files: ").strip()
        if ans != "YES":
            print("aborted")
            return 2

    (root / "config.txt").write_text(config_txt, encoding="utf-8")
    (root / "cmdline.txt").write_text(cmdline_txt, encoding="utf-8")
    for name, src in sources.items():
        dest = root / name
        print(f"copy {name} -> {dest}")
        shutil.copy2(src, dest)

    print("[ok] boot partition updated; NOVA_DATA left alone")
    return 0


if __name__ == "__main__":
    sys.exit(main())
