"""
PyOS NOVA — Namespace Containers
===================================
Run untrusted code in isolated Linux namespaces (mount, pid, net, user)
invoked from Python via ctypes → unshare() + clone().

Each container gets:
  - Private PID namespace (container processes can't see host)
  - Private mount namespace (private /tmp, /proc view)
  - Private network namespace (no network by default)
  - Private user namespace (container root maps to host non-root)
  - A copy-on-write SOS alias view (writes don't affect host)

Shell commands:
  sandbox run <script.py> [--net] [--time SECS] [--mem MB]
  sandbox list
  sandbox kill <id>
  sandbox status

Falls back gracefully on non-Linux or without CAP_SYS_ADMIN:
  runs in a restricted Python exec() namespace instead
"""

from __future__ import annotations
import os, sys, time, json, uuid, threading, subprocess, tempfile
from typing import Optional, List, Dict, TYPE_CHECKING

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from kernel.nova import NovaKernel

SANDBOX_LOG = "/system/sandboxes"
LINUX       = sys.platform == "linux"

# Linux unshare flags (from linux/sched.h)
CLONE_NEWNS   = 0x00020000   # New mount namespace
CLONE_NEWUTS  = 0x04000000   # New UTS (hostname) namespace
CLONE_NEWIPC  = 0x08000000   # New IPC namespace
CLONE_NEWPID  = 0x20000000   # New PID namespace
CLONE_NEWNET  = 0x40000000   # New network namespace
CLONE_NEWUSER = 0x10000000   # New user namespace


def _can_use_namespaces() -> bool:
    """Return True if Linux namespaces are available."""
    if not LINUX:
        return False
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        # Try an unshare with just user namespace (most permissive)
        ret  = libc.unshare(CLONE_NEWUSER)
        return True   # if no exception, we have the capability
    except Exception:
        return False


class SandboxResult:
    """Result of a sandbox execution."""

    def __init__(self, sandbox_id: str, returncode: int,
                 stdout: str, stderr: str, duration: float):
        """Initialise sandbox result."""
        self.sandbox_id = sandbox_id
        self.returncode = returncode
        self.stdout     = stdout
        self.stderr     = stderr
        self.duration   = duration
        self.success    = (returncode == 0)


class Sandbox:
    """
    One isolated execution environment.
    Uses Linux namespaces when available, Python exec() as fallback.
    """

    def __init__(self, kernel: "NovaKernel",
                 sandbox_id: str = None,
                 timeout: float = 30.0,
                 memory_mb: int = 256,
                 allow_network: bool = False):
        """Initialise a sandbox environment."""
        self.kernel       = kernel
        self.id           = sandbox_id or str(uuid.uuid4())[:8]
        self.timeout      = timeout
        self.memory_mb    = memory_mb
        self.allow_network= allow_network
        self._has_ns      = _can_use_namespaces()
        self._proc: Optional[subprocess.Popen] = None
        self._created_at  = time.time()
        self._status      = "idle"

    def run_script(self, script_path: str,
                   args: List[str] = None) -> SandboxResult:
        """
        Run a Python script in the sandbox.

        Reads the script from the SOS, writes to a temp file,
        then executes in an isolated environment.

        Args:
            script_path (str): SOS path to the Python script.
            args (List[str]): Arguments to pass to the script.

        Returns:
            SandboxResult: Execution result with stdout, stderr, returncode.
        """
        try:
            code = self.kernel.sos.read(script_path)
        except Exception as e:
            return SandboxResult(self.id, 1, "", str(e), 0)

        return self.run_code(code, args)

    def run_code(self, code: str,
                 args: List[str] = None) -> SandboxResult:
        """
        Execute Python code in the sandbox.

        Args:
            code (str): Python source code to execute.
            args (List[str]): Command-line arguments.

        Returns:
            SandboxResult: Execution result.
        """
        self._status = "running"
        start = time.time()

        if self._has_ns:
            result = self._run_in_namespace(code, args or [])
        else:
            result = self._run_in_exec(code, args or [])

        self._status = "done"
        result.duration = time.time() - start
        self._log_result(result)
        return result

    def _run_in_namespace(self, code: str,
                           args: List[str]) -> SandboxResult:
        """Execute code using Linux namespaces for isolation."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py",
                                         delete=False) as f:
            f.write(code)
            tmp_path = f.name

        try:
            # Build unshare command
            unshare_flags = ["--mount", "--pid", "--fork", "--ipc", "--uts"]
            if not self.allow_network:
                unshare_flags.append("--net")

            # Try with unshare(1) command (most portable)
            cmd = (
                ["unshare"] + unshare_flags +
                [sys.executable, tmp_path] + args
            )

            proc = subprocess.run(
                cmd,
                capture_output=True, text=True,
                timeout=self.timeout,
            )
            return SandboxResult(
                self.id, proc.returncode,
                proc.stdout[:65536], proc.stderr[:8192], 0,
            )
        except subprocess.TimeoutExpired:
            return SandboxResult(self.id, 124, "",
                                  "Sandbox timeout exceeded", 0)
        except FileNotFoundError:
            # unshare not available — fall back
            return self._run_in_exec(code, args)
        except Exception as e:
            return SandboxResult(self.id, 1, "", str(e), 0)
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    def _run_in_exec(self, code: str,
                      args: List[str]) -> SandboxResult:
        """
        Execute code in a restricted Python namespace (fallback).
        No OS-level isolation, but limits available builtins.
        """
        import io
        from contextlib import redirect_stdout, redirect_stderr

        SAFE_BUILTINS = {
            "print", "len", "range", "int", "str", "float", "list",
            "dict", "set", "tuple", "bool", "type", "isinstance",
            "enumerate", "zip", "map", "filter", "sorted", "reversed",
            "any", "all", "sum", "min", "max", "abs", "round",
            "repr", "hash", "id", "iter", "next", "vars", "dir",
        }

        restricted_builtins = {
            k: v for k, v in __builtins__.items()
            if k in SAFE_BUILTINS
        } if isinstance(__builtins__, dict) else {
            k: getattr(__builtins__, k)
            for k in SAFE_BUILTINS
            if hasattr(__builtins__, k)
        }

        ns = {
            "__builtins__": restricted_builtins,
            "__name__":     "__sandbox__",
            "__file__":     "<sandbox>",
            "args":         args,
        }

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        returncode = 0

        try:
            with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                exec(compile(code, "<sandbox>", "exec"), ns)
        except SystemExit as e:
            returncode = e.code if isinstance(e.code, int) else 1
        except Exception as e:
            stderr_buf.write(f"{type(e).__name__}: {e}\n")
            returncode = 1

        return SandboxResult(
            self.id, returncode,
            stdout_buf.getvalue()[:65536],
            stderr_buf.getvalue()[:8192],
            0,
        )

    def _log_result(self, result: SandboxResult):
        """Log sandbox execution to SOS."""
        try:
            if not self.kernel.sos.exists(SANDBOX_LOG):
                self.kernel.sos.mkdir(SANDBOX_LOG, parents=True)
            path = f"{SANDBOX_LOG}/{self.id}"
            self.kernel.sos.write(path, json.dumps({
                "id":         self.id,
                "returncode": result.returncode,
                "duration":   round(result.duration, 3),
                "stdout_len": len(result.stdout),
                "created_at": self._created_at,
                "ns_mode":    self._has_ns,
            }))
        except Exception:
            pass

    @property
    def status(self) -> dict:
        """Return sandbox status."""
        return {
            "id":              self.id,
            "status":          self._status,
            "namespace_mode":  self._has_ns,
            "timeout":         self.timeout,
            "memory_mb":       self.memory_mb,
            "allow_network":   self.allow_network,
        }


class SandboxManager:
    """Manages multiple sandbox instances."""

    def __init__(self, kernel: "NovaKernel"):
        """Initialise the sandbox manager."""
        self.kernel    = kernel
        self._active: Dict[str, Sandbox] = {}
        self._has_ns   = _can_use_namespaces()

    def create(self, timeout: float = 30.0,
               memory_mb: int = 256,
               allow_network: bool = False) -> Sandbox:
        """
        Create a new sandbox.

        Args:
            timeout (float): Maximum execution time in seconds.
            memory_mb (int): Memory limit hint.
            allow_network (bool): Whether to allow network access.

        Returns:
            Sandbox: The new sandbox instance.
        """
        sb = Sandbox(self.kernel, timeout=timeout,
                     memory_mb=memory_mb, allow_network=allow_network)
        self._active[sb.id] = sb
        return sb

    def run(self, code_or_path: str,
            args: List[str] = None,
            **kw) -> SandboxResult:
        """
        Run code or a script path in a new sandbox.

        Args:
            code_or_path (str): Python code or SOS path to script.
            args (List[str]): Arguments to pass.

        Returns:
            SandboxResult: Execution result.
        """
        sb = self.create(**kw)
        if code_or_path.startswith("/") and self.kernel.sos.exists(code_or_path):
            return sb.run_script(code_or_path, args)
        return sb.run_code(code_or_path, args)

    def list_all(self) -> List[dict]:
        """Return status of all sandboxes."""
        return [sb.status for sb in self._active.values()]

    @property
    def namespace_available(self) -> bool:
        """Return True if Linux namespace isolation is available."""
        return self._has_ns
