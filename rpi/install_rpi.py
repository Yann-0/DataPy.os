"""
PyOS NOVA — Raspberry Pi Installer
====================================
One-command installer for real Pi hardware (local or SSH).
"""

#!/usr/bin/env python3
"""
PyOS NOVA — Raspberry Pi Hardware Installer
=============================================
Installs NOVA on a real Raspberry Pi (any model with Python 3.10+).

Works on:
  Raspberry Pi 3B / 3B+  (ARM64, 1GB RAM — use RAG or tiny model)
  Raspberry Pi 4B         (ARM64, 2-8GB RAM — tinyllama runs well)
  Raspberry Pi 5          (ARM64, 4-8GB RAM — phi3 possible)
  Raspberry Pi Zero 2 W   (ARM64, 512MB — RAG only)

Run on the Pi itself:
  curl -fsSL https://raw.githubusercontent.com/.../install_rpi.py | python3
  or: python3 install_rpi.py

Or from your computer, SSH into the Pi:
  python3 install_rpi.py --ssh pi@raspberrypi.local
"""

import os, sys, subprocess, shutil, platform, argparse
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def detect_pi() -> dict:
    """Detect Raspberry Pi model and capabilities."""
    info = {
        "model":      "Unknown",
        "ram_mb":     0,
        "arch":       platform.machine(),
        "is_pi":      False,
        "gpu_opencl": False,
        "python_ver": f"{sys.version_info.major}.{sys.version_info.minor}",
    }

    # Check if we're on a Pi
    model_paths = ["/proc/device-tree/model", "/sys/firmware/devicetree/base/model"]
    for p in model_paths:
        if os.path.exists(p):
            try:
                info["model"]  = open(p, "rb").read().rstrip(b"\x00").decode("utf-8","replace")
                info["is_pi"]  = "Raspberry Pi" in info["model"]
                break
            except Exception:
                pass

    # RAM
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if "MemTotal" in line:
                    info["ram_mb"] = int(line.split()[1]) // 1024
                    break
    except Exception:
        pass

    # OpenCL GPU
    info["gpu_opencl"] = os.path.exists("/dev/dri/renderD128")

    return info


def recommend_model(ram_mb: int) -> str:
    """Recommend model.

        Args:
        ram_mb (int): Ram mb.


        Returns:
            str: Result.
        """
    if ram_mb >= 4096:
        return "phi3 (recommended for 4GB+ Pi)"
    if ram_mb >= 2048:
        return "tinyllama (recommended for 2-4GB Pi)"
    if ram_mb >= 1024:
        return "tinyllama (slow but works on 1GB Pi)"
    return "RAG tier (no model — 512MB RAM is too low for GGUF)"


def install_local():
    """Install NOVA on this machine (assumes we're on the Pi)."""
    info = detect_pi()

    print("\n  PyOS NOVA — Raspberry Pi Installer")
    print("  ─────────────────────────────────────────────────────────\n")
    print(f"  Model    : {info['model']}")
    print(f"  Arch     : {info['arch']}")
    print(f"  RAM      : {info['ram_mb']} MB")
    print(f"  Python   : {info['python_ver']}")
    print(f"  GPU      : {'OpenCL available' if info['gpu_opencl'] else 'CPU only'}")
    print(f"  Recommended model: {recommend_model(info['ram_mb'])}")
    print()

    if info["arch"] not in ("aarch64", "arm64", "armv7l", "armv8l"):
        print(f"  Warning: Expected ARM architecture, got {info['arch']}")

    if sys.version_info < (3, 10):
        print(f"  Error: Python 3.10+ required (you have {info['python_ver']})")
        print("  Update: sudo apt install python3.11")
        sys.exit(1)

    # ── Install Python dependencies ──────────────────────────────────────────
    print("  [1/4] Installing Python dependencies...")
    deps = ["numpy", "psutil"]
    _pip_install(*deps)
    print("        numpy, psutil installed\n")

    # ── Install llama-cpp-python ─────────────────────────────────────────────
    print("  [2/4] Installing llama-cpp-python (ARM64 CPU build)...")
    print("        This uses ARM NEON SIMD for fast inference on Pi...")
    success = _pip_install("llama-cpp-python")
    if success:
        print("        llama-cpp-python installed (ARM NEON enabled)")
    else:
        print("        Skipping — AI will use RAG tier (always works)")
    print()

    # ── Check VideoCore GPU (Pi-specific) ────────────────────────────────────
    print("  [3/4] Checking VideoCore GPU...")
    if info["gpu_opencl"]:
        vc_ok = _pip_install("pyopencl")
        if vc_ok:
            print("        OpenCL available — GPU inference possible!")
        else:
            print("        Install Mesa OpenCL: sudo apt install mesa-opencl-icd")
    else:
        print("        VideoCore GPU not accessible (normal for Pi OS Lite)")
        print("        For GPU: sudo apt install mesa-opencl-icd libclc-16-dev")
    print()

    # ── Copy NOVA source ─────────────────────────────────────────────────────
    print("  [4/4] Setting up NOVA...")
    nova_dst = "/opt/nova"
    data_dir = os.path.expanduser("~/.nova")
    os.makedirs(data_dir + "/models", exist_ok=True)
    os.makedirs(data_dir + "/index", exist_ok=True)

    if os.path.isdir(ROOT) and os.path.exists(os.path.join(ROOT, "main.py")):
        if not os.path.exists(nova_dst) or nova_dst == ROOT:
            print(f"        NOVA source already at: {ROOT}")
            nova_dst = ROOT
        else:
            print(f"        Copying NOVA to {nova_dst}...")
            try:
                subprocess.run(["sudo", "cp", "-r", ROOT, nova_dst], check=True)
            except Exception:
                nova_dst = ROOT  # use in-place
    else:
        nova_dst = os.path.expanduser("~/nova")
        print(f"        NOVA will run from: {nova_dst}")

    # Create launcher
    launcher = f"""#!/bin/bash
export NOVA_DATA="$HOME/.nova"
exec python3 {nova_dst}/main.py "$@"
"""
    try:
        lpath = "/usr/local/bin/nova"
        subprocess.run(["sudo", "tee", lpath], input=launcher.encode(),
                       stdout=subprocess.DEVNULL, check=True)
        subprocess.run(["sudo", "chmod", "+x", lpath], check=True)
        print("        Launcher: /usr/local/bin/nova")
    except Exception:
        # No sudo available — create in ~/bin
        bin_dir = os.path.expanduser("~/bin")
        os.makedirs(bin_dir, exist_ok=True)
        with open(f"{bin_dir}/nova", "w") as f:
            f.write(launcher)
        os.chmod(f"{bin_dir}/nova", 0o755)
        print(f"        Launcher: {bin_dir}/nova")

    # Add shell alias
    bashrc = os.path.expanduser("~/.bashrc")
    alias  = f'\nalias nova="python3 {nova_dst}/main.py"\n'
    if os.path.exists(bashrc):
        content = open(bashrc).read()
        if "alias nova=" not in content:
            with open(bashrc, "a") as f:
                f.write(alias)

    print()
    print("  ─────────────────────────────────────────────────────────")
    print("  PyOS NOVA installed on Raspberry Pi!\n")
    print(f"  Start NOVA:  nova")
    print(f"          or:  python3 {nova_dst}/main.py")
    print()
    print("  Inside NOVA — get an AI model:")
    if info["ram_mb"] >= 2048:
        print("    llm download tinyllama   (637MB, ~3 tok/s on Pi 4)")
    else:
        print("    AI runs in RAG mode (no model needed for 1GB Pi)")
    print()
    print("  Enable VideoCore GPU (Pi 4/5):")
    print("    sudo apt install mesa-opencl-icd")
    print("    Then in NOVA: gpu status")
    print("  ─────────────────────────────────────────────────────────\n")


def install_via_ssh(target: str):
    """Install NOVA on a remote Pi via SSH."""
    print(f"\n  Installing NOVA on {target} via SSH...")

    # Check SSH available
    if not shutil.which("ssh") or not shutil.which("scp"):
        print("  ssh and scp are required for remote install.")
        sys.exit(1)

    # Copy NOVA source
    print(f"  Copying NOVA source to {target}:~/nova/...")
    result = subprocess.run(
        ["scp", "-r", ROOT, f"{target}:~/nova"],
        capture_output=False,
    )
    if result.returncode != 0:
        print("  scp failed. Check SSH access.")
        sys.exit(1)

    # Run installer on Pi
    print(f"\n  Running installer on {target}...")
    subprocess.run([
        "ssh", target,
        f"python3 ~/nova/rpi/install_rpi.py"
    ])


def _pip_install(*packages) -> bool:
    """Pip install.


        Returns:
            bool: Result.
        """
    cmd = [sys.executable, "-m", "pip", "install", "--quiet",
           "--break-system-packages", *packages]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=300)
        if result.returncode != 0:
            # Try without --break-system-packages (older pip)
            cmd2 = [sys.executable, "-m", "pip", "install", "--quiet", *packages]
            result = subprocess.run(cmd2, capture_output=True, timeout=300)
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print("        Install timed out (slow internet?)")
        return False
    except Exception as e:
        print(f"        pip error: {e}")
        return False


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="PyOS NOVA Raspberry Pi installer")
    ap.add_argument("--ssh", metavar="user@host",
                    help="Install on remote Pi via SSH (e.g. pi@raspberrypi.local)")
    ap.add_argument("--info", action="store_true",
                    help="Show Pi hardware info only")
    args = ap.parse_args()

    if args.info:
        info = detect_pi()
        for k, v in info.items():
            print(f"  {k:<15} {v}")
        sys.exit(0)

    if args.ssh:
        install_via_ssh(args.ssh)
    else:
        install_local()
