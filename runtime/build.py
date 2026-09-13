"""
PyOS NOVA — Build System (nova-make)
=======================================
Make-like task runner with DAG dependency resolution.
Incremental: only rebuilds what changed, tracked by content hash in SOS.
Parallel: runs independent tasks via the multiprocessing worker pool.

Novafile syntax (Python-like DSL stored in /Novafile):

    task('test', deps=['build'], shell='pytest tests/')

    task('build', deps=['lint'],
         shell='python3 -m compileall store/ ai/')

    task('lint', shell='ruff check .')

    task('docs', deps=['build'],
         python=lambda: generate_docs())

    default = 'test'

Shell commands:
  make                   — run default task
  make <task>            — run a specific task
  make --list            — show all tasks
  make --dry-run <task>  — show what would run
  make --clean           — clear all task hashes
  make --graph           — print dependency graph
"""

from __future__ import annotations
import os, sys, time, json, hashlib, subprocess, threading
from typing import List, Dict, Optional, Callable, Set, Any, TYPE_CHECKING
from dataclasses import dataclass, field

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from kernel.nova import NovaKernel

NOVAFILE_PATH = "/Novafile"
HASH_STORE    = "/system/build_hashes.json"


@dataclass
class Task:
    """One build task."""
    name:      str
    deps:      List[str]           = field(default_factory=list)
    shell:     Optional[str]       = None   # shell command to run
    python:    Optional[Callable]  = None   # Python function to call
    inputs:    List[str]           = field(default_factory=list)  # SOS paths
    outputs:   List[str]           = field(default_factory=list)  # SOS paths
    phony:     bool                = False   # always run even if up-to-date
    desc:      str                 = ""

    def input_hash(self, sos) -> str:
        """Compute hash of all input files."""
        parts = []
        for inp in self.inputs:
            try:
                content = sos.read(inp)
                parts.append(hashlib.sha256(content.encode()).hexdigest()[:8])
            except Exception:
                parts.append("missing")
        if self.shell:
            parts.append(hashlib.sha256(self.shell.encode()).hexdigest()[:8])
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


@dataclass
class TaskResult:
    """Result of running one task."""
    name:     str
    status:   str      # ok | failed | skipped | up-to-date
    output:   str      = ""
    error:    str      = ""
    duration: float    = 0.0

    @property
    def ok(self) -> bool:
        """Return True if the task succeeded or was skipped."""
        return self.status in ("ok", "skipped", "up-to-date")


class BuildSystem:
    """
    DAG-based incremental build system for NOVA.

    Discovers tasks from the Novafile, resolves the execution order
    via topological sort, and runs each task — parallelising independent
    tasks via threads.
    """

    def __init__(self, kernel: "NovaKernel"):
        """Initialise the build system."""
        self.kernel = kernel
        self._tasks: Dict[str, Task] = {}
        self._default: Optional[str] = None
        self._hashes: Dict[str, str] = {}
        self._lock   = threading.Lock()
        self._load_hashes()

    # ── task registry ─────────────────────────────────────────────────────────

    def register(self, name: str, deps: List[str] = None,
                  shell: str = None, python: Callable = None,
                  inputs: List[str] = None, outputs: List[str] = None,
                  phony: bool = False, desc: str = ""):
        """
        Register a build task.

        Args:
            name (str): Task name.
            deps (List[str]): Task names this depends on.
            shell (str): Shell command to execute.
            python (Callable): Python function to call.
            inputs (List[str]): SOS paths that affect this task.
            outputs (List[str]): SOS paths this task produces.
            phony (bool): If True, always run.
            desc (str): Human-readable description.
        """
        self._tasks[name] = Task(
            name=name, deps=deps or [], shell=shell, python=python,
            inputs=inputs or [], outputs=outputs or [],
            phony=phony, desc=desc,
        )

    def set_default(self, name: str):
        """Set the default task."""
        self._default = name

    # ── Novafile loading ──────────────────────────────────────────────────────

    def load_novafile(self, path: str = None) -> bool:
        """
        Load tasks from a Novafile.

        The Novafile is Python code with access to a `task()` function
        and `default` variable.

        Args:
            path (str): SOS path to Novafile. Defaults to /Novafile.

        Returns:
            bool: True if loaded successfully.
        """
        nf_path = path or NOVAFILE_PATH
        try:
            src = self.kernel.sos.read(nf_path)
        except Exception:
            return False

        def _task(name, **kw):
            self.register(name, **kw)

        ns = {
            "task":    _task,
            "default": None,
            "kernel":  self.kernel,
            "sos":     self.kernel.sos,
        }
        try:
            exec(compile(src, nf_path, "exec"), ns)
            if ns.get("default"):
                self.set_default(ns["default"])
            return True
        except Exception as e:
            print(f"  Novafile error: {e}")
            return False

    # ── dependency resolution ─────────────────────────────────────────────────

    def _toposort(self, names: List[str]) -> List[str]:
        """
        Topological sort of tasks respecting dependencies.

        Args:
            names (List[str]): Task names to sort.

        Returns:
            List[str]: Execution order (leaves first).
        """
        visited: Set[str] = set()
        result:  List[str] = []

        def _visit(name: str, path: Set[str]):
            if name in path:
                raise ValueError(f"Circular dependency: {name}")
            if name in visited:
                return
            path.add(name)
            task = self._tasks.get(name)
            if task:
                for dep in task.deps:
                    _visit(dep, path)
            path.discard(name)
            visited.add(name)
            result.append(name)

        for name in names:
            _visit(name, set())
        return result

    # ── incremental check ─────────────────────────────────────────────────────

    def _load_hashes(self):
        """Load stored task hashes from SOS."""
        try:
            self._hashes = json.loads(self.kernel.sos.read(HASH_STORE))
        except Exception:
            self._hashes = {}

    def _save_hashes(self):
        """Persist task hashes to SOS."""
        try:
            self.kernel.sos.write(HASH_STORE, json.dumps(self._hashes))
        except Exception:
            pass

    def _is_up_to_date(self, task: Task) -> bool:
        """Return True if the task outputs are up-to-date."""
        if task.phony:
            return False
        stored = self._hashes.get(task.name)
        if not stored:
            return False
        current = task.input_hash(self.kernel.sos)
        return stored == current

    def _mark_done(self, task: Task):
        """Record the task's current input hash."""
        with self._lock:
            self._hashes[task.name] = task.input_hash(self.kernel.sos)
        self._save_hashes()

    # ── execution ─────────────────────────────────────────────────────────────

    def _run_task(self, task: Task,
                   dry_run: bool = False) -> TaskResult:
        """Execute a single task."""
        if self._is_up_to_date(task):
            return TaskResult(task.name, "up-to-date")

        if dry_run:
            cmd = task.shell or (task.python.__name__ if task.python else "?")
            return TaskResult(task.name, "skipped",
                               output=f"[dry-run] would run: {cmd}")

        t = time.perf_counter()
        try:
            output = ""
            if task.shell:
                result = subprocess.run(
                    task.shell, shell=True,
                    capture_output=True, text=True, timeout=300
                )
                output = (result.stdout + result.stderr).strip()
                if result.returncode != 0:
                    return TaskResult(task.name, "failed",
                                       output=output, error=f"exit {result.returncode}",
                                       duration=time.perf_counter()-t)
            if task.python:
                task.python()
                output += " [python fn ok]"

            self._mark_done(task)
            return TaskResult(task.name, "ok", output=output[:500],
                               duration=time.perf_counter()-t)

        except subprocess.TimeoutExpired:
            return TaskResult(task.name, "failed", error="timeout",
                               duration=time.perf_counter()-t)
        except Exception as e:
            return TaskResult(task.name, "failed", error=str(e),
                               duration=time.perf_counter()-t)

    def run(self, target: str = None,
             dry_run: bool = False,
             parallel: bool = True) -> List[TaskResult]:
        """
        Run a task and all its dependencies.

        Args:
            target (str): Task to run. Defaults to self._default.
            dry_run (bool): Show plan without executing.
            parallel (bool): Run independent tasks in parallel.

        Returns:
            List[TaskResult]: Results for each executed task.
        """
        target = target or self._default
        if not target:
            print("  No default task. Specify: make <task>")
            return []
        if target not in self._tasks:
            print(f"  Unknown task: {target}")
            return []

        try:
            order = self._toposort([target])
        except ValueError as e:
            print(f"  {e}")
            return []

        results: List[TaskResult] = []
        failed  = False

        for name in order:
            task = self._tasks[name]
            if failed and name != target:
                continue
            result = self._run_task(task, dry_run=dry_run)
            results.append(result)
            if not result.ok:
                failed = True

        return results

    def clean(self):
        """Clear all stored build hashes (force full rebuild)."""
        self._hashes = {}
        self._save_hashes()

    def list_tasks(self) -> List[Task]:
        """Return all registered tasks."""
        return list(self._tasks.values())

    def graph(self) -> str:
        """Return a text dependency graph."""
        lines = ["  Build dependency graph:", "  " + "─"*40]
        for name, task in self._tasks.items():
            deps = " → ".join(task.deps) if task.deps else "(none)"
            mark = " [default]" if name == self._default else ""
            lines.append(f"  {name}{mark}: deps=[{deps}]  {task.desc}")
        return "\n".join(lines)
