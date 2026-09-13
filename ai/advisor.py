"""
PyOS NOVA — Proactive Health Advisor
======================================
Background daemon that monitors system health and queues advice for
the user to read with the ``advice`` shell command.

The advisor runs two check cycles:
    * Quick check  — every 5 minutes: disk, RAM, CPU, SOS size.
    * Deep check   — every 30 minutes: AI-powered analysis of metrics.

Advice items are stored as :class:`Advice` dataclass instances in a
thread-safe deque.  The shell command ``advice`` pops and displays them.

Key classes:
    Advice   — single advice item with severity and source.
    Advisor  — background thread; call start() after kernel boot.
"""
import json
import re
import sys
import time
import queue
import subprocess
import threading
import psutil
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kernel.nova import NovaKernel

C = {
    "reset": "\033[0m", "dim": "\033[2m", "red": "\033[31m",
    "green": "\033[32m", "yellow": "\033[33m", "cyan": "\033[36m", "purple": "\033[35m",
}
col = lambda t, c: f"{C.get(c,'')}{t}{C['reset']}"


class Advice:
    """Advice."""
    SEV_ICONS = {"info": "ℹ", "warn": "⚠", "critical": "✖"}
    SEV_COLS  = {"info": "cyan", "warn": "yellow", "critical": "red"}

    def __init__(self, title, body, severity="info", source="system"):
        """Initialise the instance."""
        self.title    = title
        self.body     = body
        self.severity = severity
        self.source   = source
        self.ts       = datetime.now().strftime("%H:%M")

    def __str__(self):
        """Return a human-readable string representation."""
        icon = self.SEV_ICONS.get(self.severity, "·")
        c    = self.SEV_COLS.get(self.severity, "dim")
        return (
            f"\n  {col(icon + '  ' + self.title, c)}  {col(self.ts, 'dim')}\n"
            f"  {self.body}\n"
        )


class Advisor:
    """Advisor."""
    CHECK_INTERVAL = 300
    LLM_INTERVAL   = 1800

    def __init__(self, kernel: "NovaKernel"):
        """Initialise the instance."""
        self.kernel    = kernel
        self._queue:   queue.Queue[Advice] = queue.Queue()
        self._running  = False
        self._last_llm = 0

    def start(self):
        """Start the operation."""
        if self._running:
            return
        self._running = True
        threading.Thread(target=self._loop, daemon=True, name="NOVA-Advisor").start()

    """Stop the operation."""
    def stop(self):  self._running = False
    """Pending.


        Returns:
            int: Result.
        """
    def pending(self) -> int: return self._queue.qsize()
    """Push.

        Args:
        a (Advice): A.
        """
    def push(self, a: Advice): self._queue.put(a)

    def drain(self):
        """Drain."""
        items = []
        while not self._queue.empty():
            try: items.append(self._queue.get_nowait())
            except queue.Empty: break
        return items

    def _loop(self):
        """Main event loop — runs until stopped."""
        time.sleep(15)
        while self._running:
            try: self._run_checks()
            except Exception: pass
            if time.time() - self._last_llm > self.LLM_INTERVAL:
                try:
                    self._llm_analysis()
                    self._last_llm = time.time()
                except Exception: pass
            time.sleep(self.CHECK_INTERVAL)

    def _run_checks(self):
        """Run checks."""
        try:
            disk = psutil.disk_usage("/")
            if disk.percent > 90:
                self.push(Advice("Disk critical", f"{disk.percent:.0f}% full — use `sos stats` and remove unused objects", "critical", "disk"))
            elif disk.percent > 75:
                self.push(Advice("Disk getting full", f"{disk.percent:.0f}% used — consider archiving old objects", "warn", "disk"))
        except Exception: pass
        try:
            vm = psutil.virtual_memory()
            if vm.percent > 88:
                self.push(Advice("Memory pressure", f"RAM at {vm.percent:.0f}% — run `top` to investigate", "warn", "memory"))
        except Exception: pass

    def _llm_analysis(self):
        """Llm analysis."""
        try:
            vm   = psutil.virtual_memory()
            disk = psutil.disk_usage("/")
            snap = {"cpu": psutil.cpu_percent(0.5), "ram_pct": vm.percent,
                    "disk_pct": disk.percent, "procs": self.kernel.procs.count()}
            text = self.kernel.ai.ask(
                f"System metrics: {json.dumps(snap)}\nGive 1-2 short actionable tips.",
                system_key="advisor",
            )
            if text and len(text) > 20:
                self.push(Advice("AI advisor", text.strip(), "info", "llm"))
        except Exception: pass


class Agent:
    """Agent."""
    RISKY = {"delete_file", "rm", "format", "reboot", "shutdown"}

    def __init__(self, kernel: "NovaKernel"):
        """Initialise the instance."""
        self.kernel  = kernel
        self.history = []
        self.shell   = None

    def ask(self, user_input: str) -> str:
        """Ask.

            Args:
            user_input (str): User input.


            Returns:
                str: Result.
            """
        self.history.append({"role": "user", "content": user_input})
        full = ""
        action_block = None
        print(col("\n  NOVA Agent: ", "cyan"), end="", flush=True)
        for chunk in self.kernel.ai.chat(self.history, system_key="agent"):
            print(chunk, end="", flush=True)
            full += chunk
            if action_block is None:
                m = re.search(r'\{[^{}]*"action"[^{}]*\}', full, re.S)
                if m:
                    try: action_block = json.loads(m.group())
                    except: pass
        print()
        self.history.append({"role": "assistant", "content": full})
        if action_block and action_block.get("action", "none") != "none":
            self._maybe_execute(action_block)
        return full

    def _maybe_execute(self, action: dict):
        """Maybe execute.

            Args:
            action (dict): Action.
            """
        act    = action.get("action", "none")
        reason = action.get("reason", "")
        print()
        print(col("  ┌─ Proposed action ─────────────────────────────", "yellow"))
        print(col(f"  │  {act}", "yellow"))
        for k, v in action.items():
            if k not in ("action", "reason"):
                print(col(f"  │  {k}: {v}", "yellow"))
        print(col(f"  │  {reason}", "dim"))
        print(col("  └────────────────────────────────────────────────", "yellow"))
        if act in self.RISKY:
            print(col("  ⚠  Potentially destructive action!", "red"))
        try:
            ans = input(col("  Execute? [y/N] ", "cyan")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(); return
        if ans not in ("y", "yes"):
            print(col("  Cancelled.", "dim")); return
        self._execute(act, action)

    def _execute(self, act: str, action: dict):
        """Execute the operation and return the result.

            Args:
            act (str): Act.
            action (dict): Action.
            """
        try:
            if act == "run_command":
                cmd = action.get("command", "")
                r   = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
                if r.stdout: print(r.stdout)
                if r.stderr: print(col(r.stderr, "red"))
                print(col("  Done." if r.returncode==0 else "  Failed.", "green" if r.returncode==0 else "red"))

            elif act == "install_package":
                pkg = action.get("package", "")
                r   = subprocess.run([sys.executable, "-m", "pip", "install", pkg, "-q"],
                                     capture_output=True, text=True, timeout=120)
                print(col(f"  {pkg} installed." if r.returncode==0 else r.stderr,
                           "green" if r.returncode==0 else "red"))

            elif act == "create_file":
                path    = action.get("path", "")
                content = action.get("content", "")
                rp      = self.kernel.sos.resolve_path(path, "/home/root")
                self.kernel.sys_write(rp, content)
                print(col(f"  Created: {rp}", "green"))

            elif act == "read_file":
                path = action.get("path", "")
                rp   = self.kernel.sos.resolve_path(path, "/home/root")
                print(self.kernel.sos.read(rp))

            elif act == "search":
                results = self.kernel.search.search(action.get("query", ""), n=5)
                for r in results:
                    print(f"  {col(r['path'],'cyan')}  {r['snippet'][:80]}")
        except Exception as e:
            print(col(f"  Error: {e}", "red"))

    """Reset the operation to its initial state."""
    def reset(self): self.history = []


class ScriptWriter:
    """Script writer."""
    def __init__(self, kernel: "NovaKernel"):
        """Initialise the instance."""
        self.kernel = kernel

    def write(self, description: str, save_path: str = None, run: bool = False) -> str:
        """Write the operation.

            Args:
            description (str): Description.
            save_path (str): Save path, defaults to None.
            run (bool): Run, defaults to False.


            Returns:
                str: Result.
            """
        print(col(f"\n  Writing: {description}", "cyan"))
        code = ""
        for chunk in self.kernel.ai.complete(description, system_key="writer"):
            code += chunk
        code = re.sub(r"^```(?:python)?\n?", "", code.strip())
        code = re.sub(r"\n?```$", "", code.strip())
        if not save_path:
            slug = re.sub(r"[^a-z0-9]+", "_", description.lower())[:30].strip("_")
            save_path = f"/home/root/projects/{slug}.py"
        rp = self.kernel.sos.resolve_path(save_path, "/home/root")
        self.kernel.sys_write(rp, code + "\n", kind="code")
        print(col(f"\n  Saved: {save_path} ({len(code.splitlines())} lines)", "green"))
        if run:
            try:
                exec(compile(code, save_path, "exec"), {"__name__": "__main__", "kernel": self.kernel})
            except Exception as e:
                print(col(f"  Run error: {e}", "red"))
        return code
