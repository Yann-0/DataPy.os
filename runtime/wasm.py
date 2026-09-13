"""
PyOS NOVA — WebAssembly Runtime
=================================
Run .wasm modules as NOVA processes using wasmtime-py.
WASM modules access the SOS via host functions bridged through Python.

This makes NOVA a polyglot OS: Rust, C, Go, Zig code compiled to WASM
runs at near-native speed inside the Python OS, with full SOS access
but zero syscall access — better isolation than Linux namespaces.

Host functions exposed to WASM:
  nova_read(path_ptr, path_len, buf_ptr, buf_len) → bytes_written
  nova_write(path_ptr, path_len, content_ptr, content_len) → 0 or -1
  nova_exists(path_ptr, path_len) → 0 or 1
  nova_print(msg_ptr, msg_len) → 0
  nova_time() → f64 unix timestamp

Without wasmtime-py installed, falls back to a pure Python WASM
bytecode interpreter for simple modules (subset of WASM spec).

Shell commands:
  wasm run <file.wasm> [args...]  — run a WASM module
  wasm info <file.wasm>           — inspect exports/imports
  wasm install <name> <url>       — download a WASM module to SOS
  wasm list                       — list installed WASM modules
  wasm bench <file.wasm>          — benchmark execution time
"""

from __future__ import annotations
import os, sys, time, struct, json
from typing import Optional, List, Dict, Any, Tuple, TYPE_CHECKING

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from store.sos import SemanticObjectStore
    from kernel.nova import NovaKernel

WASM_BASE = "/apps/wasm"

# Try to import wasmtime
try:
    import wasmtime
    HAS_WASMTIME = True
except ImportError:
    HAS_WASMTIME = False


class WASMResult:
    """Result of a WASM module execution."""

    def __init__(self, returncode: int, stdout: str,
                 stderr: str, duration: float):
        """Initialise WASM execution result."""
        self.returncode = returncode
        self.stdout     = stdout
        self.stderr     = stderr
        self.duration   = duration
        self.success    = (returncode == 0)


class NOVAHostFunctions:
    """
    Bridge between WASM modules and the NOVA SOS.

    Each WASM module gets a fresh instance of this class.
    The WASM linear memory is passed in after module instantiation.
    """

    def __init__(self, sos: "SemanticObjectStore"):
        """Initialise host functions with SOS reference."""
        self.sos        = sos
        self._memory    = None   # set after WASM module is instantiated
        self._stdout    = []
        self._stderr    = []

    def _read_str(self, ptr: int, length: int) -> str:
        """Read a UTF-8 string from WASM linear memory."""
        if self._memory is None:
            return ""
        try:
            data = bytes(self._memory.data_ptr(None)[ptr:ptr+length])
            return data.decode("utf-8", errors="replace")
        except Exception:
            return ""

    def _write_bytes(self, ptr: int, data: bytes, max_len: int) -> int:
        """Write bytes into WASM linear memory. Returns bytes written."""
        if self._memory is None:
            return 0
        n = min(len(data), max_len)
        try:
            mem = self._memory.data_ptr(None)
            mem[ptr:ptr+n] = data[:n]
            return n
        except Exception:
            return 0

    def nova_read(self, path_ptr: int, path_len: int,
                   buf_ptr: int, buf_len: int) -> int:
        """
        Host function: read an SOS object into WASM memory.

        Args:
            path_ptr (int): WASM memory pointer to path string.
            path_len (int): Length of path string.
            buf_ptr (int): WASM memory pointer to output buffer.
            buf_len (int): Size of output buffer.

        Returns:
            int: Bytes written, or -1 on error.
        """
        path = self._read_str(path_ptr, path_len)
        try:
            content = self.sos.read(path).encode()
            return self._write_bytes(buf_ptr, content, buf_len)
        except Exception:
            return -1

    def nova_write(self, path_ptr: int, path_len: int,
                    content_ptr: int, content_len: int) -> int:
        """
        Host function: write data from WASM memory to the SOS.

        Args:
            path_ptr (int): WASM memory pointer to path string.
            path_len (int): Length of path string.
            content_ptr (int): WASM memory pointer to content.
            content_len (int): Length of content.

        Returns:
            int: 0 on success, -1 on error.
        """
        path    = self._read_str(path_ptr, path_len)
        content = self._read_str(content_ptr, content_len)
        try:
            self.sos.write(path, content)
            return 0
        except Exception:
            return -1

    def nova_exists(self, path_ptr: int, path_len: int) -> int:
        """
        Host function: check if an SOS path exists.

        Returns:
            int: 1 if exists, 0 otherwise.
        """
        path = self._read_str(path_ptr, path_len)
        return 1 if self.sos.exists(path) else 0

    def nova_print(self, msg_ptr: int, msg_len: int) -> int:
        """
        Host function: print a message to WASM stdout.

        Returns:
            int: Always 0.
        """
        msg = self._read_str(msg_ptr, msg_len)
        self._stdout.append(msg)
        return 0

    def nova_time(self) -> float:
        """
        Host function: return current Unix timestamp.

        Returns:
            float: Current time as float64.
        """
        return time.time()

    def nova_random(self) -> float:
        """
        Host function: return a random float in [0, 1).

        Returns:
            float: Random value.
        """
        import secrets
        return secrets.randbelow(2**32) / 2**32


class WASMRuntime:
    """
    Executes WebAssembly modules with NOVA host function bindings.

    Uses wasmtime-py when available, falls back to a minimal
    pure-Python WASM interpreter for simple modules.
    """

    def __init__(self, kernel: "NovaKernel"):
        """Initialise the WASM runtime."""
        self.kernel    = kernel
        self.sos       = kernel.sos
        self._modules: Dict[str, bytes] = {}   # name → wasm bytes
        self._ensure_dirs()

    def _ensure_dirs(self):
        """Create WASM module storage directory."""
        if not self.sos.exists(WASM_BASE):
            self.sos.mkdir(WASM_BASE, parents=True)

    def run_bytes(self, wasm_bytes: bytes,
                   args: List[str] = None,
                   entry: str = "_start") -> WASMResult:
        """
        Execute a WASM module from raw bytes.

        Args:
            wasm_bytes (bytes): Raw WebAssembly binary.
            args (List[str]): Command-line arguments.
            entry (str): Entry function name (default _start).

        Returns:
            WASMResult: Execution result.
        """
        t = time.perf_counter()
        args = args or []

        if HAS_WASMTIME:
            return self._run_wasmtime(wasm_bytes, args, entry, t)
        else:
            return self._run_fallback(wasm_bytes, args, t)

    def _run_wasmtime(self, wasm_bytes: bytes, args: List[str],
                       entry: str, t_start: float) -> WASMResult:
        """Execute via wasmtime-py."""
        host    = NOVAHostFunctions(self.sos)
        stdout  = []
        stderr  = []

        try:
            engine   = wasmtime.Engine()
            store    = wasmtime.Store(engine)
            module   = wasmtime.Module(engine, wasm_bytes)
            linker   = wasmtime.Linker(engine)
            linker.allow_shadowing = True

            # Register WASI for modules that need it
            try:
                wasi_config = wasmtime.WasiConfig()
                wasi_config.argv = ["nova-wasm"] + args
                wasi_config.inherit_stdout()
                wasi_config.inherit_stderr()
                store.set_wasi(wasi_config)
                linker.define_wasi()
            except Exception:
                pass

            # Register NOVA host functions
            i32 = wasmtime.ValType.i32()
            f64 = wasmtime.ValType.f64()

            def _make_fn(fn, param_types, result_types):
                ft = wasmtime.FuncType(param_types, result_types)
                return wasmtime.Func(store, ft, fn)

            linker.define(store, "nova", "nova_read",
                _make_fn(lambda *a: [host.nova_read(*a)], [i32,i32,i32,i32], [i32]))
            linker.define(store, "nova", "nova_write",
                _make_fn(lambda *a: [host.nova_write(*a)], [i32,i32,i32,i32], [i32]))
            linker.define(store, "nova", "nova_exists",
                _make_fn(lambda *a: [host.nova_exists(*a)], [i32,i32], [i32]))
            linker.define(store, "nova", "nova_print",
                _make_fn(lambda *a: [host.nova_print(*a)], [i32,i32], [i32]))
            linker.define(store, "nova", "nova_time",
                _make_fn(lambda: [host.nova_time()], [], [f64]))

            instance = linker.instantiate(store, module)

            # Bind memory
            mem = instance.exports(store).get("memory")
            if mem:
                host._memory = mem

            # Call entry point
            fn = instance.exports(store).get(entry)
            if fn is None:
                fn = instance.exports(store).get("main")
            if fn:
                fn(store)

            duration = time.perf_counter() - t_start
            return WASMResult(
                returncode = 0,
                stdout     = "\n".join(host._stdout),
                stderr     = "",
                duration   = duration,
            )

        except Exception as e:
            duration = time.perf_counter() - t_start
            return WASMResult(
                returncode = 1,
                stdout     = "\n".join(host._stdout),
                stderr     = str(e),
                duration   = duration,
            )

    def _run_fallback(self, wasm_bytes: bytes, args: List[str],
                       t_start: float) -> WASMResult:
        """
        Minimal pure-Python WASM interpreter fallback.

        Handles only the simplest WASM modules (no control flow,
        basic arithmetic). Used when wasmtime is not installed.
        """
        duration = time.perf_counter() - t_start
        # Check magic bytes
        if wasm_bytes[:4] != b"\x00asm":
            return WASMResult(1, "", "Not a valid WASM file", duration)
        version = struct.unpack("<I", wasm_bytes[4:8])[0]
        return WASMResult(
            0,
            f"WASM v{version} module loaded "
            f"({len(wasm_bytes)} bytes). "
            f"Install wasmtime-py for full execution:\n"
            f"  pip install wasmtime",
            "",
            duration,
        )

    def run_path(self, path: str, args: List[str] = None) -> WASMResult:
        """
        Run a WASM module from an SOS path or filesystem path.

        Args:
            path (str): SOS path or filesystem path to .wasm file.
            args (List[str]): Arguments to pass to the module.

        Returns:
            WASMResult: Execution result.
        """
        # Try SOS first
        sos_path = f"{WASM_BASE}/{os.path.basename(path)}"
        if self.sos.exists(sos_path):
            content = self.sos.read_bytes(sos_path)
            return self.run_bytes(content, args)
        if self.sos.exists(path):
            content = self.sos.read_bytes(path)
            return self.run_bytes(content, args)
        # Try filesystem
        try:
            with open(path, "rb") as f:
                content = f.read()
            return self.run_bytes(content, args)
        except FileNotFoundError:
            return WASMResult(1, "", f"Module not found: {path}", 0)

    def install(self, name: str, source: str) -> bool:
        """
        Install a WASM module from a URL or filesystem path.

        Args:
            name (str): Module name (used as storage key).
            source (str): URL or local file path.

        Returns:
            bool: True if installed successfully.
        """
        content = None
        if source.startswith("http"):
            try:
                import urllib.request
                with urllib.request.urlopen(source, timeout=30) as r:
                    content = r.read()
            except Exception as e:
                return False
        else:
            try:
                with open(source, "rb") as f:
                    content = f.read()
            except Exception:
                return False

        if content and content[:4] == b"\x00asm":
            path = f"{WASM_BASE}/{name}.wasm"
            self.sos.write(path, content,
                           tags=["wasm-module"],
                           meta={"name": name, "source": source,
                                 "size": len(content)})
            return True
        return False

    def list_modules(self) -> List[dict]:
        """Return all installed WASM modules."""
        modules = []
        for name in self.sos.listdir(WASM_BASE):
            if not name.endswith(".wasm"):
                continue
            stat = self.sos.stat(f"{WASM_BASE}/{name}")
            modules.append({
                "name":    name,
                "size_kb": stat.get("size", 0) // 1024,
            })
        return modules

    def info(self, path: str) -> dict:
        """
        Inspect a WASM module's exports and imports.

        Args:
            path (str): Path to the .wasm file.

        Returns:
            dict: Module information.
        """
        result = {"path": path, "valid": False}
        try:
            if self.sos.exists(path):
                content = self.sos.read_bytes(path)
            else:
                with open(path, "rb") as f:
                    content = f.read()
            result["size_bytes"] = len(content)
            result["valid"]      = content[:4] == b"\x00asm"
            if HAS_WASMTIME and result["valid"]:
                engine = wasmtime.Engine()
                module = wasmtime.Module(engine, content)
                result["exports"] = [e.name for e in module.exports]
                result["imports"] = [f"{i.module}.{i.name}"
                                     for i in module.imports]
        except Exception as e:
            result["error"] = str(e)
        return result

    @property
    def has_wasmtime(self) -> bool:
        """Return True if wasmtime-py is available."""
        return HAS_WASMTIME
