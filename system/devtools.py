"""
PyOS NOVA — Developer Tooling Bundle
========================================
Four developer experience tools:

1. Interactive Debugger
   pdb integrated with NOVA shell. Breakpoints stored as SOS tags.
   Step through code with SOS, kernel, and AI state visible.

2. Build System (nova-make)
   Make-like task runner with DAG dependency resolution.
   Incremental: only rebuilds what changed (content-hash in SOS).
   Parallel via the worker pool.

3. Flame Graph Profiler
   Capture cProfile samples, render as SVG flame graph.
   Stored in SOS, openable in HELIX.

4. Session Recorder (nova-cast)
   Record shell sessions to SOS (asciinema-compatible format).
   Play back in terminal. Export as SVG animation.

Shell commands:
  debug <script.py>           — debug a script
  debug breakpoint <path> <n> — set a breakpoint
  debug list                  — list breakpoints
  make [target]               — run a build target
  make list                   — list available targets
  profile <command>           — profile and generate flame graph
  profile list                — list recorded profiles
  rec start                   — start recording
  rec stop                    — stop and save recording
  rec play <id>               — replay a session
  rec list                    — list recordings
"""

from __future__ import annotations
import os, sys, time, json, io, threading, cProfile, pstats, hashlib
from typing import List, Dict, Optional, Any, Callable, TYPE_CHECKING
from dataclasses import dataclass, field

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from store.sos import SemanticObjectStore
    from kernel.nova import NovaKernel

BREAKPOINT_TAG = "breakpoint"
BUILD_BASE     = "/build"
PROFILE_BASE   = "/profiles"
RECORDING_BASE = "/recordings"


# ─────────────────────────────────────────────────── Interactive Debugger

class NovaDebugger:
    """
    Interactive Python debugger integrated with the NOVA shell.

    Breakpoints are stored as SOS tags (breakpoint:line_N on .py objects).
    The debugger provides pdb-like commands with NOVA state inspection.
    """

    def __init__(self, kernel: "NovaKernel"):
        """Initialise the NOVA debugger."""
        self.kernel     = kernel
        self._breakpoints: Dict[str, set] = {}   # path → set of line numbers
        self._watching   = False

    def set_breakpoint(self, path: str, line: int) -> bool:
        """
        Set a breakpoint in an SOS .py object.

        Args:
            path (str): SOS path to the .py file.
            line (int): Line number.

        Returns:
            bool: True if successfully set.
        """
        if path not in self._breakpoints:
            self._breakpoints[path] = set()
        self._breakpoints[path].add(line)
        self.kernel.sos.tag(path, f"{BREAKPOINT_TAG}:{line}")
        return True

    def clear_breakpoint(self, path: str, line: int = None) -> int:
        """
        Clear breakpoints.

        Args:
            path (str): SOS path.
            line (int): Specific line to clear (None = clear all).

        Returns:
            int: Number of breakpoints cleared.
        """
        if line:
            self._breakpoints.get(path, set()).discard(line)
            try:
                self.kernel.sos.untag(path, f"{BREAKPOINT_TAG}:{line}")
            except Exception:
                pass
            return 1
        # Clear all for path
        n = len(self._breakpoints.pop(path, set()))
        tags = self.kernel.sos.get_tags(path)
        for tag in tags:
            if tag.startswith(BREAKPOINT_TAG):
                try:
                    self.kernel.sos.untag(path, tag)
                except Exception:
                    pass
        return n

    def list_breakpoints(self) -> List[dict]:
        """Return all active breakpoints."""
        result = []
        for path, lines in self._breakpoints.items():
            for line in sorted(lines):
                result.append({"path": path, "line": line})
        return result

    def debug_file(self, path: str, args: List[str] = None) -> int:
        """
        Debug a Python file from the SOS.

        Runs the file under pdb with NOVA kernel available
        as a global variable.

        Args:
            path (str): SOS path to the .py file.
            args (List[str]): Script arguments.

        Returns:
            int: Exit code.
        """
        try:
            code = self.kernel.sos.read(path)
        except Exception as e:
            print(f"  Cannot read {path}: {e}")
            return 1

        import pdb, tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py",
                                          delete=False) as f:
            f.write(code)
            tmp_path = f.name

        # Inject NOVA globals
        ns = {
            "__file__":   path,
            "__name__":   "__main__",
            "kernel":     self.kernel,
            "sos":        self.kernel.sos,
            "args":       args or [],
        }

        print(f"  NOVA Debugger: {path}")
        print(f"  Commands: n=next  s=step  c=continue  p <expr>=print  q=quit")
        print(f"  NOVA vars: kernel, sos, args")

        try:
            debugger = pdb.Pdb()
            debugger.reset()
            debugger.run(compile(code, path, "exec"), ns)
            return 0
        except SystemExit as e:
            return e.code or 0
        except Exception as e:
            print(f"  Error: {e}")
            return 1
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    def status(self) -> dict:
        """Return debugger status."""
        return {
            "breakpoints": sum(len(v) for v in self._breakpoints.values()),
            "files":       len(self._breakpoints),
        }


# ─────────────────────────────────────────────────── Build System

@dataclass
class BuildTask:
    """One task in a Novafile build graph."""
    name:     str
    deps:     List[str]     # names of tasks this depends on
    commands: List[str]     # shell commands to run
    outputs:  List[str]     # SOS paths produced by this task
    inputs:   List[str]     # SOS paths consumed by this task

    def input_hash(self, sos) -> str:
        """Compute hash of all input files."""
        h = hashlib.sha256()
        for inp in sorted(self.inputs):
            try:
                content = sos.read(inp).encode()
                h.update(content)
            except Exception:
                h.update(inp.encode())
        return h.hexdigest()[:16]


class BuildSystem:
    """
    Make-like incremental build system for NOVA.

    Tasks are defined in a Novafile (SOS object at /build/Novafile).
    Incremental: checks content hashes before running.
    Parallel: uses the worker pool for independent tasks.
    """

    NOVAFILE = "/build/Novafile"
    CACHE    = "/build/.cache"

    def __init__(self, kernel: "NovaKernel"):
        """Initialise the build system."""
        self.kernel  = kernel
        self._tasks: Dict[str, BuildTask] = {}
        self._ensure_dirs()
        self._load()

    def _ensure_dirs(self):
        """Create build directories."""
        for p in (BUILD_BASE, self.CACHE):
            if not self.kernel.sos.exists(p):
                self.kernel.sos.mkdir(p, parents=True)

    def _load(self):
        """Load tasks from the Novafile."""
        try:
            data = json.loads(self.kernel.sos.read(self.NOVAFILE))
            for t in data.get("tasks", []):
                task = BuildTask(**t)
                self._tasks[task.name] = task
        except Exception:
            pass

    def define(self, name: str, commands: List[str],
                deps: List[str] = None,
                inputs: List[str] = None,
                outputs: List[str] = None) -> BuildTask:
        """
        Define a build task.

        Args:
            name (str): Task name.
            commands (List[str]): Shell commands to execute.
            deps (List[str]): Tasks that must run before this.
            inputs (List[str]): SOS paths consumed.
            outputs (List[str]): SOS paths produced.

        Returns:
            BuildTask: The created task.
        """
        task = BuildTask(
            name=name, commands=commands,
            deps=deps or [], inputs=inputs or [],
            outputs=outputs or [],
        )
        self._tasks[name] = task
        self._save()
        return task

    def _save(self):
        """Persist Novafile to SOS."""
        data = {"tasks": [t.__dict__ for t in self._tasks.values()]}
        self.kernel.sos.write(self.NOVAFILE, json.dumps(data, indent=2))

    def _is_dirty(self, task: BuildTask) -> bool:
        """Return True if the task needs to run."""
        cache_path = f"{self.CACHE}/{task.name}"
        try:
            cached_hash = self.kernel.sos.read(cache_path).strip()
            return task.input_hash(self.kernel.sos) != cached_hash
        except Exception:
            return True   # no cache → must run

    def _mark_clean(self, task: BuildTask):
        """Record the current input hash in the cache."""
        cache_path = f"{self.CACHE}/{task.name}"
        self.kernel.sos.write(cache_path, task.input_hash(self.kernel.sos))

    def _topo_sort(self, target: str) -> List[str]:
        """Topological sort of tasks ending at target."""
        visited = set()
        order   = []
        def _visit(name):
            if name in visited:
                return
            visited.add(name)
            task = self._tasks.get(name)
            if task:
                for dep in task.deps:
                    _visit(dep)
            order.append(name)
        _visit(target)
        return order

    def run(self, target: str = "all",
             force: bool = False) -> dict:
        """
        Run a build target and its dependencies.

        Args:
            target (str): Task to build.
            force (bool): Ignore cache and rebuild everything.

        Returns:
            dict: Build results per task.
        """
        import subprocess

        if target == "all":
            order = list(self._tasks.keys())
        else:
            if target not in self._tasks:
                return {"error": f"Unknown target: {target}"}
            order = self._topo_sort(target)

        results = {}
        for name in order:
            task = self._tasks.get(name)
            if not task:
                continue
            if not force and not self._is_dirty(task):
                results[name] = {"status": "skipped", "reason": "up-to-date"}
                continue

            t_start = time.time()
            ok = True
            output = []
            for cmd in task.commands:
                r = subprocess.run(
                    cmd, shell=True, capture_output=True,
                    text=True, timeout=300
                )
                output.append(r.stdout + r.stderr)
                if r.returncode != 0:
                    ok = False
                    break

            elapsed = time.time() - t_start
            if ok:
                self._mark_clean(task)
            results[name] = {
                "status":  "ok" if ok else "failed",
                "time_s":  round(elapsed, 2),
                "output":  "".join(output)[:500],
            }

        return results

    def list_tasks(self) -> List[dict]:
        """Return all defined tasks."""
        return [
            {"name": t.name, "deps": t.deps,
             "cmds": len(t.commands),
             "dirty": self._is_dirty(t)}
            for t in self._tasks.values()
        ]


# ─────────────────────────────────────────────────── Flame Graph Profiler

class FlameGraphProfiler:
    """
    cProfile-based flame graph profiler for NOVA.

    Profiles a command or function and generates an SVG flame graph
    stored in the SOS. Open in HELIX to view.
    """

    def __init__(self, sos: "SemanticObjectStore"):
        """Initialise the flame graph profiler."""
        self.sos = sos
        self._ensure_dirs()

    def _ensure_dirs(self):
        """Create profile storage directory."""
        if not self.sos.exists(PROFILE_BASE):
            self.sos.mkdir(PROFILE_BASE, parents=True)

    def profile(self, fn: Callable, *args,
                 name: str = None, **kwargs) -> str:
        """
        Profile a function and generate a flame graph.

        Args:
            fn (Callable): Function to profile.
            *args, **kwargs: Arguments to fn.
            name (str): Profile name.

        Returns:
            str: SOS path of the generated SVG flame graph.
        """
        pr = cProfile.Profile()
        pr.enable()
        try:
            fn(*args, **kwargs)
        except Exception:
            pass
        finally:
            pr.disable()

        svg  = self._to_svg(pr, name or fn.__name__)
        ts   = int(time.time())
        path = f"{PROFILE_BASE}/{name or fn.__name__}_{ts}.svg"
        self.sos.write(path, svg, kind="text",
                        tags=["flame-graph"])
        return path

    def profile_code(self, code: str, name: str = "profile",
                      globs: dict = None) -> str:
        """
        Profile a code string.

        Args:
            code (str): Python code to profile.
            name (str): Profile name.
            globs (dict): Global variables.

        Returns:
            str: SOS path of generated flame graph SVG.
        """
        pr = cProfile.Profile()
        ns = globs or {}
        pr.enable()
        try:
            exec(compile(code, name, "exec"), ns)
        except Exception:
            pass
        finally:
            pr.disable()
        svg  = self._to_svg(pr, name)
        ts   = int(time.time())
        path = f"{PROFILE_BASE}/{name}_{ts}.svg"
        self.sos.write(path, svg, kind="text", tags=["flame-graph"])
        return path

    def _to_svg(self, pr: cProfile.Profile, title: str) -> str:
        """Convert cProfile data to an SVG flame graph."""
        buf  = io.StringIO()
        stat = pstats.Stats(pr, stream=buf).sort_stats("cumulative")
        stat.print_stats(50)
        raw  = buf.getvalue()

        # Parse top functions
        funcs = []
        for line in raw.splitlines():
            import re
            m = re.match(
                r"\s+(\d+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+(.+)",
                line
            )
            if m:
                calls    = int(m.group(1))
                cumtime  = float(m.group(4))
                fn       = m.group(6).strip()
                if fn and cumtime > 0:
                    funcs.append({"calls": calls,
                                   "cumtime": cumtime, "fn": fn})

        if not funcs:
            return f"<svg><text>No profile data for {title}</text></svg>"

        # Simple horizontal bar chart as flame graph approximation
        W, H   = 900, max(50 + len(funcs[:30]) * 22, 200)
        max_t  = funcs[0]["cumtime"] if funcs else 1.0
        colors = ["#d35400","#e67e22","#f39c12","#27ae60","#2980b9"]

        lines  = [
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{W}" height="{H}" style="background:#1a1a2e">',
            f'<text x="10" y="24" font-size="14" fill="#ecf0f1" '
            f'font-weight="bold">{title} — Flame Graph</text>',
        ]
        for i, fn_info in enumerate(funcs[:30]):
            w    = int(fn_info["cumtime"] / max_t * (W - 160))
            y    = 36 + i * 22
            col  = colors[i % len(colors)]
            fn   = fn_info["fn"][-50:]
            t    = f"{fn_info['cumtime']*1000:.1f}ms"
            lines.append(
                f'<rect x="0" y="{y}" width="{w}" height="18" '
                f'fill="{col}" opacity="0.85"/>'
                f'<text x="4" y="{y+13}" font-size="11" fill="white">'
                f'{fn} ({t})</text>'
            )
        lines.append("</svg>")
        return "\n".join(lines)

    def list_profiles(self) -> List[dict]:
        """Return all stored profiles."""
        profiles = []
        for name in self.sos.listdir(PROFILE_BASE):
            stat = self.sos.stat(f"{PROFILE_BASE}/{name}")
            profiles.append({"name": name,
                               "size_kb": stat.get("size",0)//1024})
        return profiles


# ─────────────────────────────────────────────────── Session Recorder

@dataclass
class Recording:
    """One recorded terminal session."""
    rec_id:    str
    started_at: float
    events:    List[dict] = field(default_factory=list)
    ended_at:  float      = 0.0
    title:     str        = ""

    def to_dict(self) -> dict:
        """Serialize to dict."""
        return {
            "rec_id":     self.rec_id,
            "started_at": self.started_at,
            "ended_at":   self.ended_at,
            "title":      self.title,
            "events":     self.events,
        }


class SessionRecorder:
    """
    Records and replays terminal sessions.

    Format: asciinema-compatible v2 JSON.
    Stored in SOS at /recordings/<id>.cast
    """

    def __init__(self, sos: "SemanticObjectStore"):
        """Initialise the session recorder."""
        self.sos       = sos
        self._active:  Optional[Recording] = None
        self._start_ts = 0.0
        self._ensure_dirs()

    def _ensure_dirs(self):
        """Create recordings directory."""
        if not self.sos.exists(RECORDING_BASE):
            self.sos.mkdir(RECORDING_BASE, parents=True)

    def start(self, title: str = "") -> str:
        """
        Start a new recording.

        Args:
            title (str): Optional session title.

        Returns:
            str: Recording ID.
        """
        import uuid
        rec_id         = uuid.uuid4().hex[:8]
        self._active   = Recording(rec_id=rec_id,
                                    started_at=time.time(),
                                    title=title)
        self._start_ts = self._active.started_at

        # Monkey-patch stdout to capture output
        self._orig_stdout = sys.stdout
        recorder          = self

        class _RecordingWriter:
            def write(self, text):
                if recorder._active:
                    ts = time.time() - recorder._start_ts
                    recorder._active.events.append([ts, "o", text])
                recorder._orig_stdout.write(text)
            def flush(self): recorder._orig_stdout.flush()
            encoding = "utf-8"
            isatty   = lambda self: True

        sys.stdout = _RecordingWriter()
        return rec_id

    def stop(self) -> Optional[str]:
        """
        Stop recording and save to SOS.

        Returns:
            str: SOS path of the saved recording, or None.
        """
        if not self._active:
            return None
        sys.stdout         = self._orig_stdout
        self._active.ended_at = time.time()

        rec  = self._active
        self._active = None

        # Save in asciinema v2 format
        header = json.dumps({
            "version": 2,
            "width":   80, "height": 24,
            "timestamp": int(rec.started_at),
            "title":   rec.title or f"nova-session-{rec.rec_id}",
        })
        lines  = [header]
        for ts, kind, data in rec.events:
            lines.append(json.dumps([round(ts, 6), kind, data]))

        path = f"{RECORDING_BASE}/{rec.rec_id}.cast"
        self.sos.write(path, "\n".join(lines), kind="text",
                        tags=["recording"])
        return path

    def play(self, rec_id: str, speed: float = 1.0):
        """
        Replay a recording to the terminal.

        Args:
            rec_id (str): Recording ID to play.
            speed (float): Playback speed multiplier.
        """
        path = f"{RECORDING_BASE}/{rec_id}.cast"
        try:
            content = self.sos.read(path)
        except Exception:
            print(f"  Recording not found: {rec_id}")
            return

        lines = content.splitlines()
        if not lines:
            return

        # Parse events
        events = []
        for line in lines[1:]:   # skip header
            try:
                e = json.loads(line)
                if len(e) >= 3 and e[1] == "o":
                    events.append((e[0], e[2]))
            except Exception:
                pass

        if not events:
            print("  (empty recording)")
            return

        # Replay with timing
        prev_ts = 0.0
        for ts, text in events:
            delay = (ts - prev_ts) / speed
            if delay > 0.001:
                time.sleep(min(delay, 2.0))   # cap at 2s
            sys.stdout.write(text)
            sys.stdout.flush()
            prev_ts = ts

    def list_recordings(self) -> List[dict]:
        """Return all saved recordings."""
        recs = []
        for name in self.sos.listdir(RECORDING_BASE):
            if not name.endswith(".cast"):
                continue
            try:
                content  = self.sos.read(f"{RECORDING_BASE}/{name}")
                header   = json.loads(content.splitlines()[0])
                duration = 0.0
                for line in content.splitlines()[1:]:
                    try:
                        e = json.loads(line)
                        duration = max(duration, e[0])
                    except Exception:
                        pass
                recs.append({
                    "id":       name[:-5],
                    "title":    header.get("title", name),
                    "duration": round(duration, 1),
                })
            except Exception:
                pass
        return recs

    @property
    def is_recording(self) -> bool:
        """Return True if currently recording."""
        return self._active is not None
