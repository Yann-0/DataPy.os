#!/usr/bin/env python3
"""
PyOS NOVA — Python PID-1 (UEFI Edition)
=========================================
This is the first Python code that executes after UEFI.
There is NO Linux kernel. No initramfs. No OS.
Python is running directly on UEFI firmware.

When called with --efi flag (from python3.efi):
  - UEFI Boot Services are still active
  - No POSIX environment (no /proc, no fork, no signals)
  - UEFI services accessible via boot/uefi_services.py
  - Console via EFI Simple Text Output
  - Filesystem via EFI Simple File System

After boot/uefi_services.py patches the Python environment:
  - open() works (via EFI filesystem)
  - print() works (via EFI console)
  - The rest of NOVA runs identically to the Linux version
"""

import sys
import os

EFI_MODE = "--efi" in sys.argv

def _uefi_setup():
    """Configure Python to work under UEFI (no POSIX layer)."""
    if not EFI_MODE:
        return

    # Find NOVA source root
    # In UEFI mode: /nova/ is the root of the EFI partition
    nova_dirs = ["/nova", "\\nova", os.path.dirname(os.path.dirname(__file__))]
    for d in nova_dirs:
        if os.path.exists(os.path.join(d, "main.py")):
            sys.path.insert(0, d)
            os.environ["NOVA_SRC"]  = d
            os.environ["NOVA_DATA"] = os.path.join(d, ".nova_data")
            break

    # Apply UEFI service patches
    try:
        sys.path.insert(0, os.environ.get("NOVA_SRC", "/nova"))
        from boot.uefi_services import patch_python_for_uefi
        patch_python_for_uefi()
        print("\033[32m[  OK]\033[0m UEFI services patched")
    except ImportError:
        print("[WARN] uefi_services.py not found — running in degraded mode")

def _print_banner():
    """Print the NOVA boot banner."""
    print("\n\033[36m")
    print("  ██████╗ ██╗   ██╗ ██████╗ ███████╗    ███╗   ██╗ ██████╗ ██╗   ██╗ █████╗ ")
    print("  ██╔══██╗╚██╗ ██╔╝██╔═══██╗██╔════╝    ████╗  ██║██╔═══██╗██║   ██║██╔══██╗")
    print("  ██████╔╝ ╚████╔╝ ██║   ██║███████╗    ██╔██╗ ██║██║   ██║██║   ██║███████║")
    print("  ██╔═══╝   ╚██╔╝  ██║   ██║╚════██║    ██║╚██╗██║██║   ██║╚██╗ ██╔╝██╔══██║")
    print("  ██║        ██║   ╚██████╔╝███████║    ██║ ╚████║╚██████╔╝ ╚████╔╝ ██║  ██║")
    print("  ╚═╝        ╚═╝    ╚═════╝ ╚══════╝    ╚═╝  ╚═══╝ ╚═════╝   ╚═══╝  ╚═╝  ╚═╝")
    print("\033[0m")
    print(f"  Python {sys.version.split()[0]} — UEFI Direct Boot — v0.0008")
    if EFI_MODE:
        print("  \033[33m[ EFI ]\033[0m Running under UEFI firmware (no OS)")
    print()

def main():
    """Boot PyOS NOVA."""
    _print_banner()
    _uefi_setup()

    # Find and import the NOVA kernel
    nova_src = os.environ.get("NOVA_SRC")
    if not nova_src:
        for d in ["/nova", os.path.dirname(os.path.dirname(__file__))]:
            if os.path.exists(os.path.join(d, "main.py")):
                nova_src = d
                sys.path.insert(0, d)
                break

    if not nova_src:
        print("\033[31m[FAIL]\033[0m Cannot find NOVA source tree")
        print("  Expected main.py at /nova/main.py on the EFI partition")
        return 1

    print(f"  \033[32m[  OK]\033[0m NOVA source: {nova_src}")

    try:
        import main as nova_main
        return nova_main.main()
    except ImportError as e:
        print(f"\033[31m[FAIL]\033[0m Cannot import NOVA: {e}")
        return 1

if __name__ == "__main__":
    sys.exit(main() or 0)
