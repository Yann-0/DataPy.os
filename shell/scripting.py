"""
PyOS NOVA — Shell Scripting
=============================
Define shell functions, aliases, and scheduled tasks.

Function syntax (Python-inspired):
  def greet(name):
      echo Hello $name
      echo Welcome to NOVA

Alias syntax:
  alias ll = ls -la
  alias gs = git status

Cron syntax:
  cron every 5m: df
  cron every 1h: advisor
  cron daily at 09:00: echo Good morning

Stored in /home/root/.nova_rc and auto-loaded on shell start.
"""

import os, sys, re, time, threading, shlex
from typing import Dict, List, Callable, Optional, TYPE_CHECKING

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from store.sos import SemanticObjectStore

RC_PATH = "/home/root/.nova_rc"


class ShellFunction:
    """Shell function."""
    def __init__(self, name: str, params: List[str], body: List[str]):
        """Initialise the instance."""
        self.name   = name
        self.params = params
        self.body   = body

    def call(self, args: List[str], shell) -> bool:
        """Execute this function in the context of `shell`."""
        # Bind params to args
        env_backup = dict(shell.env)
        for i, param in enumerate(self.params):
            shell.env[param] = args[i] if i < len(args) else ""
        # Also set positional $1 $2 etc.
        for i, arg in enumerate(args, 1):
            shell.env[str(i)] = arg
        try:
            for line in self.body:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                shell._exec_line(line)
            return True
        finally:
            shell.env = env_backup


class CronTask:
    """Cron task."""
    def __init__(self, name: str, command: str,
                 interval_seconds: float, last_run: float = 0):
        """Initialise the instance."""
        self.name             = name
        self.command          = command
        self.interval_seconds = interval_seconds
        self.last_run         = last_run
        self.enabled          = True
        self.run_count        = 0

    def is_due(self) -> bool:
        """Return True if due.


            Returns:
                bool: Result.
            """
        return self.enabled and (time.time() - self.last_run) >= self.interval_seconds

    def mark_run(self):
        """Mark run."""
        self.last_run = time.time()
        self.run_count += 1


def _parse_interval(spec: str) -> float:
    """Parse '5m', '1h', '30s', '2d' → seconds."""
    spec = spec.strip().lower()
    m = re.match(r"(\d+(?:\.\d+)?)\s*([smhd]?)", spec)
    if not m:
        return 60.0
    n, unit = float(m.group(1)), m.group(2) or "s"
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


class ScriptingEngine:
    """
    Manages shell functions, aliases, and cron tasks.
    Loaded from .nova_rc at startup.
    """

    def __init__(self, sos: "SemanticObjectStore"):
        """Initialise the instance."""
        self.sos       = sos
        self.functions: Dict[str, ShellFunction] = {}
        self.aliases:   Dict[str, str]           = {}
        self.cron:      List[CronTask]           = []
        self._cron_thread: Optional[threading.Thread] = None
        self._running   = False

    # ─── loader ───────────────────────────────────────────────────────────────
    def load_rc(self, shell=None) -> int:
        """Load and execute the .nova_rc file. Returns lines parsed."""
        try:
            content = self.sos.read(RC_PATH)
        except Exception:
            return 0
        return self.parse(content, shell)

    def parse(self, content: str, shell=None) -> int:
        """Parse shell scripting syntax from a string."""
        lines   = content.splitlines()
        count   = 0
        i       = 0
        while i < len(lines):
            line = lines[i].strip()
            # Skip comments + empty
            if not line or line.startswith("#"):
                i += 1; continue

            # Function definition
            m = re.match(r"def\s+(\w+)\s*\(([^)]*)\)\s*:", line)
            if m:
                name   = m.group(1)
                params = [p.strip() for p in m.group(2).split(",") if p.strip()]
                body   = []
                i += 1
                while i < len(lines):
                    bl = lines[i]
                    if bl and not bl[0].isspace():
                        break
                    body.append(bl.rstrip())
                    i += 1
                self.functions[name] = ShellFunction(name, params, body)
                count += 1
                continue

            # Alias
            m = re.match(r"alias\s+(\w+)\s*=\s*(.+)", line)
            if m:
                self.aliases[m.group(1)] = m.group(2).strip().strip("'\"")
                count += 1; i += 1; continue

            # Cron
            m = re.match(r"cron\s+every\s+(\S+)\s*:\s*(.+)", line, re.I)
            if m:
                interval = _parse_interval(m.group(1))
                command  = m.group(2).strip()
                self.cron.append(CronTask(f"cron_{len(self.cron)}", command, interval))
                count += 1; i += 1; continue

            m = re.match(r"cron\s+daily\s+at\s+(\d+:\d+)\s*:\s*(.+)", line, re.I)
            if m:
                interval = 86400
                command  = m.group(2).strip()
                self.cron.append(CronTask(f"cron_daily_{len(self.cron)}", command, interval))
                count += 1; i += 1; continue

            # Plain command (run at load time if shell provided)
            if shell and not line.startswith("def ") and not line.startswith("alias "):
                try:
                    shell._exec_line(line)
                except Exception: pass
            i += 1

        return count

    # ─── runtime helpers ──────────────────────────────────────────────────────
    def expand_alias(self, cmd: str) -> Optional[str]:
        """Expand alias.

            Args:
            cmd (str): Cmd.


            Returns:
                Optional[str]: Result.
            """
        return self.aliases.get(cmd)

    def get_function(self, name: str) -> Optional[ShellFunction]:
        """Return the function.

            Args:
            name (str): Name.


            Returns:
                Optional[ShellFunction]: Result.
            """
        return self.functions.get(name)

    def has_callable(self, name: str) -> bool:
        """Return True if callable is present.

            Args:
            name (str): Name.


            Returns:
                bool: Result.
            """
        return name in self.functions or name in self.aliases

    # ─── cron scheduler ───────────────────────────────────────────────────────
    def start_cron(self, shell):
        """Start cron.

            Args:
            shell: Shell.
            """
        if self._running: return
        self._running = True
        def _loop():
            """Main event loop — runs until stopped."""
            while self._running:
                for task in self.cron:
                    if task.is_due():
                        try:
                            shell._exec_line(task.command)
                        except Exception: pass
                        task.mark_run()
                time.sleep(5)
        self._cron_thread = threading.Thread(target=_loop, daemon=True, name="nova-cron")
        self._cron_thread.start()

    def stop_cron(self):
        """Stop cron."""
        self._running = False

    # ─── rc file editor ───────────────────────────────────────────────────────
    def add_alias(self, name: str, command: str):
        """Add alias.

            Args:
            name (str): Name.
            command (str): Command.
            """
        self.aliases[name] = command
        self._append_rc(f"alias {name} = {command}")

    def add_function(self, func: ShellFunction):
        """Add function.

            Args:
            func (ShellFunction): Func.
            """
        self.functions[func.name] = func
        params = ", ".join(func.params)
        lines  = [f"def {func.name}({params}):"]
        lines += [f"    {line}" for line in func.body]
        self._append_rc("\n".join(lines))

    def add_cron(self, interval: str, command: str):
        """Add cron.

            Args:
            interval (str): Interval.
            command (str): Command.
            """
        task = CronTask(f"cron_{len(self.cron)}", command, _parse_interval(interval))
        self.cron.append(task)
        self._append_rc(f"cron every {interval}: {command}")

    def _append_rc(self, text: str):
        """Append rc.

            Args:
            text (str): Text.
            """
        try:
            existing = ""
            try: existing = self.sos.read(RC_PATH)
            except: pass
            self.sos.write(RC_PATH, existing + "\n" + text + "\n")
        except Exception: pass

    def status(self) -> dict:
        """Return the current status as a dict.


            Returns:
                dict: Result.
            """
        return {
            "functions": list(self.functions.keys()),
            "aliases":   list(self.aliases.keys()),
            "cron":      [{"name": t.name, "cmd": t.command,
                           "interval": t.interval_seconds,
                           "runs": t.run_count} for t in self.cron],
        }
