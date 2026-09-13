"""
PyOS NOVA — GPU Detection & Management
========================================
Detects available GPU backends and configures llama-cpp-python
to use the best available hardware.

Supported backends (in priority order):
  1. CUDA   — NVIDIA GPUs (GeForce, RTX, Tesla, A100, H100...)
  2. ROCm   — AMD GPUs (RX 6000+, RX 7000+, Instinct...)
  3. Metal  — Apple Silicon (M1/M2/M3) and AMD on macOS
  4. Vulkan — Cross-vendor GPU compute (Intel, AMD, NVIDIA)
  5. OpenCL — Broad compatibility fallback
  6. CPU    — Always available, uses all cores

In VirtualBox:
  VirtualBox uses VMSVGA/VBoxSVGA display — no GPU compute passthrough
  by default. AI runs on CPU. You can enable GPU passthrough with
  the Extension Pack (see README).
"""

import os
import sys
import json
import shutil
import struct
import subprocess
import ctypes
import threading
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass, field


@dataclass
class GPUInfo:
    """G p u info."""
    backend:     str           # cuda | rocm | metal | vulkan | opencl | cpu
    name:        str           # e.g. "NVIDIA RTX 4090"
    vram_mb:     int           # available VRAM in MB (0 = unknown/CPU)
    driver:      str           # driver version string
    compute:     str           # compute capability / version
    available:   bool = True
    n_gpu_layers: int = 0      # layers to offload (computed from VRAM)

    @property
    def vram_gb(self) -> float:
        """Vram gb.


            Returns:
                float: Result.
            """
        return round(self.vram_mb / 1024, 1)

    def __str__(self):
        """Return a human-readable string representation."""
        if self.backend == "cpu":
            return f"CPU ({self.name})"
        return f"{self.backend.upper()} — {self.name} ({self.vram_gb}GB VRAM)"


# ─────────────────────────────────────────────────────────────────────────────
# Backend probes
# ─────────────────────────────────────────────────────────────────────────────

def _probe_cuda() -> Optional[GPUInfo]:
    """Probe NVIDIA CUDA via nvidia-smi and pynvml."""
    # Try nvidia-smi first (doesn't need Python bindings)
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        try:
            out = subprocess.run(
                [nvidia_smi, "--query-gpu=name,memory.total,driver_version,compute_cap",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            )
            if out.returncode == 0:
                lines = [l.strip() for l in out.stdout.strip().splitlines() if l.strip()]
                if lines:
                    parts  = [p.strip() for p in lines[0].split(",")]
                    name   = parts[0] if len(parts) > 0 else "Unknown NVIDIA GPU"
                    vram   = int(float(parts[1])) if len(parts) > 1 else 0
                    driver = parts[2] if len(parts) > 2 else "?"
                    cap    = parts[3] if len(parts) > 3 else "?"
                    return GPUInfo("cuda", name, vram, driver, cap,
                                   n_gpu_layers=_layers_for_vram(vram))
        except Exception:
            pass

    # Try pynvml
    try:
        import pynvml
        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        name   = pynvml.nvmlDeviceGetName(handle)
        mem    = pynvml.nvmlDeviceGetMemoryInfo(handle)
        driver = pynvml.nvmlSystemGetDriverVersion()
        vram   = mem.total // 1024 // 1024
        return GPUInfo("cuda", name if isinstance(name, str) else name.decode(),
                       vram, driver, "via pynvml",
                       n_gpu_layers=_layers_for_vram(vram))
    except Exception:
        pass

    # Try ctypes to load libcuda
    try:
        lib = ctypes.CDLL("libcuda.so.1")
        result = ctypes.c_int(0)
        if lib.cuInit(0) == 0:
            dev = ctypes.c_int(0)
            lib.cuDeviceGet(ctypes.byref(dev), 0)
            name_buf = ctypes.create_string_buffer(256)
            lib.cuDeviceGetName(name_buf, 256, dev)
            mem = ctypes.c_size_t(0)
            lib.cuDeviceTotalMem_v2(ctypes.byref(mem), dev)
            vram = mem.value // 1024 // 1024
            return GPUInfo("cuda", name_buf.value.decode("utf-8", errors="replace"),
                           vram, "libcuda", "ctypes",
                           n_gpu_layers=_layers_for_vram(vram))
    except Exception:
        pass

    return None


def _probe_rocm() -> Optional[GPUInfo]:
    """Probe AMD ROCm via rocminfo / amdgpu."""
    rocminfo = shutil.which("rocminfo")
    if rocminfo:
        try:
            out = subprocess.run([rocminfo], capture_output=True, text=True, timeout=8)
            if out.returncode == 0:
                name  = "AMD GPU"
                vram  = 0
                for line in out.stdout.splitlines():
                    if "Marketing Name" in line:
                        name = line.split(":", 1)[-1].strip()
                    if "Size:" in line and "MB" in line:
                        try:
                            vram = int(line.split(":")[1].strip().split()[0])
                        except Exception:
                            pass
                return GPUInfo("rocm", name, vram, "ROCm", rocminfo,
                               n_gpu_layers=_layers_for_vram(vram))
        except Exception:
            pass

    # Check for /dev/kfd (ROCm kernel fusion driver)
    if os.path.exists("/dev/kfd"):
        return GPUInfo("rocm", "AMD GPU (ROCm)", 0, "kfd", "detected",
                       n_gpu_layers=20)

    return None


def _probe_metal() -> Optional[GPUInfo]:
    """Probe Apple Metal on macOS."""
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(
            ["system_profiler", "SPDisplaysDataType", "-json"],
            capture_output=True, text=True, timeout=8,
        )
        data = json.loads(out.stdout)
        gpus = data.get("SPDisplaysDataType", [])
        if gpus:
            gpu   = gpus[0]
            name  = gpu.get("sppci_model", "Apple GPU")
            vram  = 0
            vram_str = gpu.get("sppci_vram", "0 MB")
            try:
                vram = int(vram_str.split()[0])
                if "GB" in vram_str.upper():
                    vram *= 1024
            except Exception:
                pass
            # Apple Silicon shares memory — estimate from system RAM
            if vram == 0:
                import resource
                vram = 8192  # conservative estimate for shared memory
            return GPUInfo("metal", name, vram, "Metal", "macOS",
                           n_gpu_layers=_layers_for_vram(vram))
    except Exception:
        pass
    return None


def _probe_vulkan() -> Optional[GPUInfo]:
    """Probe Vulkan via vulkaninfo."""
    vulkaninfo = shutil.which("vulkaninfo")
    if not vulkaninfo:
        return None
    try:
        out = subprocess.run(
            [vulkaninfo, "--summary"],
            capture_output=True, text=True, timeout=8,
            env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":0")},
        )
        if out.returncode == 0:
            name = "GPU (Vulkan)"
            for line in out.stdout.splitlines():
                if "deviceName" in line:
                    name = line.split("=", 1)[-1].strip()
                    break
            return GPUInfo("vulkan", name, 0, "Vulkan", "vulkaninfo",
                           n_gpu_layers=16)
    except Exception:
        pass
    return None


def _probe_opencl() -> Optional[GPUInfo]:
    """Probe OpenCL via pyopencl or clinfo."""
    try:
        import pyopencl as cl
        platforms = cl.get_platforms()
        if platforms:
            devices = platforms[0].get_devices(cl.device_type.GPU)
            if devices:
                dev  = devices[0]
                name = dev.name.strip()
                vram = dev.global_mem_size // 1024 // 1024
                return GPUInfo("opencl", name, vram, "OpenCL",
                               str(dev.version),
                               n_gpu_layers=_layers_for_vram(vram))
    except Exception:
        pass

    clinfo = shutil.which("clinfo")
    if clinfo:
        try:
            out = subprocess.run([clinfo, "--raw"], capture_output=True, text=True, timeout=5)
            if "GPU" in out.stdout:
                return GPUInfo("opencl", "GPU (OpenCL)", 0, "OpenCL", "clinfo",
                               n_gpu_layers=12)
        except Exception:
            pass
    return None


def _probe_cpu() -> GPUInfo:
    """Always available CPU fallback."""
    try:
        import psutil
        cores = psutil.cpu_count(logical=True)
        freq  = psutil.cpu_freq()
        name  = f"{cores}-core CPU"
        if freq:
            name += f" @ {freq.max/1000:.1f}GHz"
    except Exception:
        import os
        cores = os.cpu_count() or 1
        name  = f"{cores}-core CPU"

    # Try to get CPU name
    cpu_name = _get_cpu_name()
    if cpu_name:
        name = cpu_name

    return GPUInfo("cpu", name, 0, "software", f"{cores} threads", n_gpu_layers=0)


def _get_cpu_name() -> Optional[str]:
    """Return the cpu name.


        Returns:
            Optional[str]: Result.
        """
    try:
        if sys.platform == "linux":
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if "model name" in line:
                        return line.split(":", 1)[1].strip()
        elif sys.platform == "darwin":
            out = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True, text=True, timeout=3,
            )
            return out.stdout.strip()
        elif sys.platform == "win32":
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                  r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            return winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Layer calculator
# ─────────────────────────────────────────────────────────────────────────────

def _layers_for_vram(vram_mb: int) -> int:
    """
    Estimate how many transformer layers to offload to GPU.
    Based on model size heuristics for common GGUF Q4 models.
    """
    if vram_mb <= 0:
        return 0
    if vram_mb < 2048:    # < 2GB
        return 8
    if vram_mb < 4096:    # 2-4GB
        return 20
    if vram_mb < 6144:    # 4-6GB
        return 28
    if vram_mb < 8192:    # 6-8GB
        return 32
    if vram_mb < 12288:   # 8-12GB
        return 40
    if vram_mb < 24576:   # 12-24GB
        return 60
    return -1              # -1 = all layers on GPU


# ─────────────────────────────────────────────────────────────────────────────
# Main detector
# ─────────────────────────────────────────────────────────────────────────────

class GPUDetector:
    """
    Probes all GPU backends in priority order and returns the best available.
    Results are cached after first detection.
    """

    def __init__(self):
        """Initialise the instance."""
        self._detected: Optional[GPUInfo] = None
        self._all:      List[GPUInfo]     = []
        self._lock      = threading.Lock()

    def detect(self, force: bool = False) -> GPUInfo:
        """Detect the operation and return the result.

            Args:
            force (bool): Force, defaults to False.


            Returns:
                GPUInfo: Result.
            """
        with self._lock:
            if self._detected and not force:
                return self._detected
            self._all = []
            probes = [
                ("CUDA",   _probe_cuda),
                ("ROCm",   _probe_rocm),
                ("Metal",  _probe_metal),
                ("Vulkan", _probe_vulkan),
                ("OpenCL", _probe_opencl),
            ]
            for name, fn in probes:
                try:
                    result = fn()
                    if result:
                        self._all.append(result)
                except Exception:
                    pass
            cpu = _probe_cpu()
            self._all.append(cpu)
            self._detected = self._all[0]  # best = first found
            return self._detected

    def all_backends(self) -> List[GPUInfo]:
        """All backends.


            Returns:
                List[GPUInfo]: Result.
            """
        if not self._all:
            self.detect()
        return self._all

    def install_instructions(self, backend: str) -> str:
        """Return pip install instructions for a given backend."""
        instructions = {
            "cuda": (
                "# NVIDIA CUDA support for llama-cpp-python:\n"
                "CMAKE_ARGS=\"-DLLAMA_CUDA=on\" pip install llama-cpp-python --force-reinstall\n"
                "# Requires: CUDA toolkit 11.8+ installed"
            ),
            "rocm": (
                "# AMD ROCm support for llama-cpp-python:\n"
                "CMAKE_ARGS=\"-DLLAMA_HIPBLAS=on\" pip install llama-cpp-python --force-reinstall\n"
                "# Requires: ROCm 5.6+ installed"
            ),
            "metal": (
                "# Apple Metal support (auto-enabled on macOS):\n"
                "CMAKE_ARGS=\"-DLLAMA_METAL=on\" pip install llama-cpp-python --force-reinstall"
            ),
            "vulkan": (
                "# Vulkan support for llama-cpp-python:\n"
                "CMAKE_ARGS=\"-DLLAMA_VULKAN=on\" pip install llama-cpp-python --force-reinstall\n"
                "# Requires: Vulkan SDK installed"
            ),
            "cpu": (
                "# CPU-only (default, works everywhere):\n"
                "pip install llama-cpp-python"
            ),
        }
        return instructions.get(backend, "pip install llama-cpp-python")

    def report(self) -> str:
        """Human-readable GPU report."""
        lines = ["\n  GPU Detection Report", "  " + "─" * 40]
        best = self.detect()
        lines.append(f"  Best backend: {best}")
        lines.append(f"  GPU layers  : {best.n_gpu_layers} "
                     f"({'all' if best.n_gpu_layers == -1 else best.n_gpu_layers})")
        if len(self.all_backends()) > 1:
            lines.append("\n  All backends detected:")
            for g in self.all_backends():
                mark = " ← active" if g.backend == best.backend else ""
                lines.append(f"    {g.backend:<10} {g.name}{mark}")
        lines.append("")
        return "\n".join(lines)


# Singleton
_detector = GPUDetector()


def detect_gpu(force: bool = False) -> GPUInfo:
    """Detect the best available GPU. Cached after first call."""
    return _detector.detect(force=force)


def gpu_report() -> str:
    """Gpu report.


        Returns:
            str: Result.
        """
    return _detector.report()


def install_gpu_backend(backend: str = None):
    """
    Install llama-cpp-python with GPU support.
    If backend is None, auto-detect.
    """
    if backend is None:
        gpu = detect_gpu()
        backend = gpu.backend

    instructions = _detector.install_instructions(backend)
    print(f"\n  Installing llama-cpp-python with {backend.upper()} support...")
    print(f"\n  Run these commands:\n")
    for line in instructions.splitlines():
        print(f"    {line}")
    print()

    # Attempt auto-install
    import shlex
    env_vars = {}
    if backend == "cuda":
        env_vars["CMAKE_ARGS"] = "-DLLAMA_CUDA=on"
    elif backend == "rocm":
        env_vars["CMAKE_ARGS"] = "-DLLAMA_HIPBLAS=on"
    elif backend == "metal":
        env_vars["CMAKE_ARGS"] = "-DLLAMA_METAL=on"
    elif backend == "vulkan":
        env_vars["CMAKE_ARGS"] = "-DLLAMA_VULKAN=on"

    cmd = [sys.executable, "-m", "pip", "install", "llama-cpp-python",
           "--force-reinstall", "--quiet"]
    env = {**os.environ, **env_vars}

    try:
        result = subprocess.run(cmd, env=env, timeout=300)
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print("  Install timed out. Run manually.")
        return False
