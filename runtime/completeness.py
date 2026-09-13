"""
PyOS NOVA — Completeness & Resilience Bundle
===============================================
Six features completing NOVA for production use:

1. Git Bridge - mount git repos as SOS namespaces
2. Resilience Toolkit - circuit breaker, rate limiter, retry
3. Distributed Tracing - spans across all subsystems
4. Chaos Engineering - inject faults to verify resilience
5. Materialised Views - cached SQL projections auto-invalidated
6. MVCC Snapshot Isolation - concurrent reads without blocking writes

Shell commands:
  git mount <url> <path>     — mount a git repo
  git unmount <path>         — remove a git mount
  git mounts                 — list mounts
  resilience status          — show circuit breaker states
  trace list                 — list recent traces
  trace show <id>            — Gantt chart for a trace
  chaos inject <fault>       — inject a fault
  chaos stop                 — stop all faults
  view define <n> <sql>   — define a materialised view
  view refresh <n>        — refresh a view
  view list                  — list all views
  mvcc begin                 — start a snapshot transaction
  mvcc read <path>           — read in snapshot context
  mvcc commit                — commit writes
"""

from __future__ import annotations
import os, sys, time, json, threading, hashlib, functools, math, subprocess, shutil
from typing import Optional, List, Dict, Any, Callable, TYPE_CHECKING
from dataclasses import dataclass, field
from contextlib import contextmanager

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from store.sos import SemanticObjectStore
    from kernel.nova import NovaKernel

TRACES_BASE = "/system/traces"
GIT_MOUNTS  = "/system/git_mounts.json"
VIEWS_BASE  = "/store/views"


# ─────────────────────────────────────────────────── Git Bridge

class GitBridge:
    """Mounts git repositories as read-only SOS namespaces."""

    def __init__(self, kernel: "NovaKernel"):
        """Initialise the Git bridge."""
        self.kernel  = kernel
        self._mounts: Dict[str, str] = {}
        self._load_mounts()

    def _load_mounts(self):
        """Load persisted mounts."""
        try:
            self._mounts = json.loads(self.kernel.sos.read(GIT_MOUNTS))
        except Exception:
            self._mounts = {}

    def _save_mounts(self):
        """Persist mounts."""
        try:
            self.kernel.sos.write(GIT_MOUNTS, json.dumps(self._mounts))
        except Exception:
            pass

    def mount(self, git_url: str, sos_path: str,
               branch: str = "main") -> dict:
        """
        Clone and mount a git repository at a SOS path.

        Args:
            git_url (str): Git repository URL or local path.
            sos_path (str): Target SOS mount point.
            branch (str): Branch to import.

        Returns:
            dict: Mount result with file count.
        """
        import tempfile

        if not self.kernel.sos.exists(sos_path):
            self.kernel.sos.mkdir(sos_path, parents=True)

        td = tempfile.mkdtemp()
        try:
            # Try with specified branch, fall back to no --branch
            for cmd in [
                ["git", "clone", "--depth=1", "--branch", branch, git_url, td],
                ["git", "clone", "--depth=1", git_url, td + "_fb"],
            ]:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                if r.returncode == 0:
                    if td + "_fb" in " ".join(cmd):
                        shutil.rmtree(td, ignore_errors=True)
                        td = td + "_fb"
                    break
            else:
                return {"error": "git clone failed"}

            count = 0
            for dirpath, dirnames, filenames in os.walk(td):
                dirnames[:] = [d for d in dirnames if d != ".git"]
                for fname in filenames:
                    fp  = os.path.join(dirpath, fname)
                    rel = os.path.relpath(fp, td)
                    dst = f"{sos_path.rstrip('/')}/{rel}"
                    try:
                        with open(fp, "r", errors="replace") as f:
                            content = f.read()
                        parent = dst.rsplit("/", 1)[0]
                        if parent and not self.kernel.sos.exists(parent):
                            self.kernel.sos.mkdir(parent, parents=True)
                        self.kernel.sos.write(dst, content,
                                               tags=["git-mounted"])
                        count += 1
                    except Exception:
                        pass

            self._mounts[sos_path] = git_url
            self._save_mounts()
            return {"mounted": sos_path, "files": count, "url": git_url}

        except Exception as e:
            return {"error": str(e)}
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def unmount(self, sos_path: str) -> bool:
        """Remove a git mount."""
        self._mounts.pop(sos_path, None)
        self._save_mounts()
        return True

    def list_mounts(self) -> List[dict]:
        """Return all mounts."""
        return [{"path": k, "url": v} for k, v in self._mounts.items()]


# ─────────────────────────────────────────────────── Resilience Toolkit

class CircuitBreaker:
    """Circuit breaker: CLOSED → OPEN → HALF_OPEN → CLOSED."""

    CLOSED = "closed"; OPEN = "open"; HALF_OPEN = "half_open"

    def __init__(self, name: str, failure_threshold: int = 5,
                  window_s: float = 30, recovery_s: float = 60):
        """Initialise the circuit breaker."""
        self.name = name
        self._threshold = failure_threshold
        self._window    = window_s
        self._recovery  = recovery_s
        self._state     = self.CLOSED
        self._failures: List[float] = []
        self._opened    = 0.0
        self._lock      = threading.Lock()

    def call(self, fn: Callable, *args, **kwargs) -> Any:
        """Call fn through the circuit breaker. Raises if OPEN."""
        with self._lock:
            if self._state == self.OPEN:
                if time.time() - self._opened > self._recovery:
                    self._state = self.HALF_OPEN
                else:
                    raise RuntimeError(f"Circuit {self.name} OPEN")
        try:
            result = fn(*args, **kwargs)
            with self._lock:
                if self._state == self.HALF_OPEN:
                    self._state    = self.CLOSED
                    self._failures = []
            return result
        except Exception as e:
            with self._lock:
                now = time.time()
                self._failures = [t for t in self._failures if now-t < self._window]
                self._failures.append(now)
                if len(self._failures) >= self._threshold:
                    self._state  = self.OPEN
                    self._opened = now
            raise

    @property
    def state(self) -> str:
        """Return current state."""
        return self._state


class TokenBucketLimiter:
    """Token bucket rate limiter."""

    def __init__(self, capacity: float, rate: float):
        """Initialise the token bucket."""
        self.capacity = capacity
        self.rate     = rate
        self._tokens  = capacity
        self._last    = time.time()
        self._lock    = threading.Lock()
        self._rejected = 0

    def acquire(self, tokens: float = 1.0) -> bool:
        """Try to acquire tokens. Returns True if granted."""
        with self._lock:
            now = time.time()
            self._tokens = min(self.capacity,
                                self._tokens + (now - self._last) * self.rate)
            self._last = now
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            self._rejected += 1
            return False

    def status(self) -> dict:
        """Return limiter status."""
        return {"tokens": round(self._tokens, 1), "rejected": self._rejected}


def retry_with_backoff(max_attempts: int = 3, base_delay: float = 0.5,
                        exceptions=(Exception,)):
    """Decorator: retry with exponential backoff + jitter."""
    import random

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last = None
            for attempt in range(max_attempts):
                try:
                    return fn(*args, **kwargs)
                except exceptions as e:
                    last = e
                    if attempt < max_attempts - 1:
                        delay = min(base_delay * 2**attempt + random.uniform(0, 0.05), 10)
                        time.sleep(delay)
            raise last
        return wrapper
    return decorator


class ResilienceKit:
    """Factory for resilience primitives."""

    def __init__(self):
        """Initialise the resilience kit."""
        self._breakers: Dict[str, CircuitBreaker] = {}
        self._limiters: Dict[str, TokenBucketLimiter] = {}

    def circuit_breaker(self, name: str, **kw) -> CircuitBreaker:
        """Get or create a named circuit breaker."""
        if name not in self._breakers:
            self._breakers[name] = CircuitBreaker(name, **kw)
        return self._breakers[name]

    def rate_limiter(self, name: str, capacity: float = 100,
                      rate: float = 10) -> TokenBucketLimiter:
        """Get or create a named rate limiter."""
        if name not in self._limiters:
            self._limiters[name] = TokenBucketLimiter(capacity, rate)
        return self._limiters[name]

    def status(self) -> dict:
        """Return status of all primitives."""
        return {
            "breakers": {n: b.state for n, b in self._breakers.items()},
            "limiters":  {n: l.status() for n, l in self._limiters.items()},
        }


# ─────────────────────────────────────────────────── Distributed Tracing

@dataclass
class Span:
    """One trace span."""
    trace_id:   str
    span_id:    str
    parent_id:  Optional[str]
    name:       str
    service:    str    = "nova"
    started_at: float  = field(default_factory=time.time)
    ended_at:   float  = 0.0
    error:      str    = ""
    tags:       Dict[str, str] = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        """Return duration in milliseconds."""
        return ((self.ended_at or time.time()) - self.started_at) * 1000

    def finish(self, error: str = ""):
        """Mark the span as finished."""
        self.ended_at = time.time()
        self.error    = error

    def to_dict(self) -> dict:
        """Serialise to dict."""
        return {**self.__dict__, "duration_ms": round(self.duration_ms, 2)}


class Tracer:
    """Distributed tracer for NOVA subsystems."""

    _LOCAL = threading.local()

    def __init__(self, sos: "SemanticObjectStore", service: str = "nova"):
        """Initialise the tracer."""
        self.sos     = sos
        self.service = service
        self._spans: Dict[str, List[Span]] = {}
        self._lock  = threading.Lock()
        self._ensure_dirs()

    def _ensure_dirs(self):
        """Create traces directory."""
        if not self.sos.exists(TRACES_BASE):
            self.sos.mkdir(TRACES_BASE, parents=True)

    def start_span(self, name: str, trace_id: str = None) -> Span:
        """Start a new span."""
        parent = getattr(self._LOCAL, "span", None)
        if not trace_id:
            trace_id = (parent.trace_id if parent
                         else hashlib.sha256(f"{time.time_ns()}".encode()).hexdigest()[:16])
        span = Span(
            trace_id  = trace_id,
            span_id   = hashlib.sha256(f"{time.time_ns()}{name}".encode()).hexdigest()[:8],
            parent_id = parent.span_id if parent else None,
            name      = name,
            service   = self.service,
        )
        self._LOCAL.span = span
        with self._lock:
            self._spans.setdefault(trace_id, []).append(span)
        return span

    def finish_span(self, span: Span, error: str = ""):
        """Finish a span."""
        span.finish(error)
        self._LOCAL.span = None

    @contextmanager
    def trace(self, name: str, **tags):
        """Context manager for tracing a code block."""
        span = self.start_span(name)
        span.tags = {str(k): str(v) for k, v in tags.items()}
        try:
            yield span
        except Exception as e:
            span.finish(str(e)); raise
        else:
            span.finish()

    def gantt(self, trace_id: str) -> str:
        """Render trace as ASCII Gantt chart."""
        with self._lock:
            spans = list(self._spans.get(trace_id, []))
        if not spans:
            return "  No trace found"
        t0    = min(s.started_at for s in spans)
        total = max((s.ended_at or time.time()) - t0 for s in spans) or 0.001
        lines = [f"\n  Trace: {trace_id}", "  " + "─"*60]
        for s in sorted(spans, key=lambda x: x.started_at):
            off = int((s.started_at - t0) / total * 40)
            dur = max(1, int(s.duration_ms / (total*1000) * 40))
            bar = " "*off + "█"*dur
            err = " ✖" if s.error else ""
            lines.append(f"  {s.name[:24]:<25} {bar:<42} {s.duration_ms:.1f}ms{err}")
        return "\n".join(lines)

    def recent_traces(self, n: int = 20) -> List[dict]:
        """Return recent trace summaries."""
        with self._lock:
            return [
                {"id": tid, "spans": len(spans),
                 "root": spans[0].name if spans else "?",
                 "ms": round(sum(s.duration_ms for s in spans), 1)}
                for tid, spans in list(self._spans.items())[-n:]
            ]


# ─────────────────────────────────────────────────── Chaos Engineering

FAULTS = {
    "latency":   "Add 500ms latency to SOS reads",
    "error":     "Make 20% of SOS reads raise an error",
    "oom":       "Force small memory allocations to fail",
    "partition": "Block SOS writes (simulates disk full)",
    "slowdown":  "Make AI responses 5× slower",
}


class ChaosEngine:
    """
    Injects controlled faults into NOVA to verify resilience.

    Monkey-patches SOS and AI methods to introduce:
    latency, errors, OOM, network partitions, slowdowns.
    """

    def __init__(self, kernel: "NovaKernel"):
        """Initialise the chaos engine."""
        self.kernel  = kernel
        self._active: Dict[str, bool] = {}
        self._orig   = {}   # saved original methods

    def inject(self, fault: str) -> bool:
        """
        Inject a named fault.

        Args:
            fault (str): Fault type (latency/error/partition/slowdown).

        Returns:
            bool: True if fault was injected.
        """
        if fault not in FAULTS:
            return False
        if fault in self._active:
            return True

        sos = self.kernel.sos
        self._active[fault] = True

        if fault == "latency":
            orig = sos.read
            self._orig["latency_read"] = orig
            def _slow_read(path):
                time.sleep(0.5)
                return orig(path)
            sos.read = _slow_read

        elif fault == "error":
            import random
            orig = sos.read
            self._orig["error_read"] = orig
            def _flaky_read(path):
                if random.random() < 0.2:
                    raise IOError(f"[chaos] simulated read error on {path}")
                return orig(path)
            sos.read = _flaky_read

        elif fault == "partition":
            orig = sos.write
            self._orig["partition_write"] = orig
            def _blocked_write(path, content, **kw):
                raise IOError("[chaos] write blocked — simulated disk full")
            sos.write = _blocked_write

        elif fault == "slowdown":
            ai   = self.kernel.ai
            orig = ai.ask
            self._orig["slowdown_ask"] = orig
            def _slow_ask(prompt, **kw):
                time.sleep(2.0)
                return orig(prompt, **kw)
            ai.ask = _slow_ask

        return True

    def stop(self, fault: str = None) -> List[str]:
        """
        Stop one or all injected faults.

        Args:
            fault (str): Fault to stop, or None to stop all.

        Returns:
            List[str]: Faults that were stopped.
        """
        stopped = []
        targets = [fault] if fault else list(self._active.keys())

        for f in targets:
            if f not in self._active:
                continue
            # Restore original methods
            for key, method in self._orig.items():
                if key.startswith(f):
                    attr   = key.split("_", 1)[1]   # e.g. "latency_read" → "read"
                    target = self.kernel.sos
                    if "ask" in attr:
                        target = self.kernel.ai
                    setattr(target, attr, method)
            del self._active[f]
            stopped.append(f)

        return stopped

    def active_faults(self) -> List[str]:
        """Return list of currently active faults."""
        return list(self._active.keys())


# ─────────────────────────────────────────────────── Materialised Views

class MaterialisedView:
    """
    A cached SQL projection that auto-invalidates when relevant objects change.
    """

    def __init__(self, name: str, sql: str, invalidate_patterns: List[str]):
        """Initialise the materialised view."""
        self.name               = name
        self.sql                = sql
        self.invalidate_patterns = invalidate_patterns
        self.last_refreshed     = 0.0
        self.data: List[dict]   = []
        self.dirty              = True


class ViewManager:
    """
    Manages materialised views over SOS SQL data.
    Auto-refreshes views when relevant SOS paths change.
    """

    def __init__(self, sos: "SemanticObjectStore", bus=None):
        """Initialise the view manager."""
        self.sos    = sos
        self._views: Dict[str, MaterialisedView] = {}
        self._lock  = threading.Lock()
        if bus:
            self._subscribe(bus)

    def _subscribe(self, bus):
        """Subscribe to events to auto-invalidate views."""
        def _on_event(event):
            with self._lock:
                for view in self._views.values():
                    for pat in view.invalidate_patterns:
                        import fnmatch
                        if fnmatch.fnmatch(event.path, pat):
                            view.dirty = True
                            break
        bus.subscribe("/**", _on_event, owner="view-manager")

    def define(self, name: str, sql: str,
                invalidate_patterns: List[str] = None) -> MaterialisedView:
        """
        Define a materialised view.

        Args:
            name (str): View identifier.
            sql (str): SQL query to materialise.
            invalidate_patterns (List[str]): SOS path patterns that invalidate the view.

        Returns:
            MaterialisedView: The defined view.
        """
        view = MaterialisedView(name, sql, invalidate_patterns or ["/**"])
        with self._lock:
            self._views[name] = view
        return view

    def refresh(self, name: str) -> List[dict]:
        """
        Refresh a view by re-running its SQL.

        Args:
            name (str): View name.

        Returns:
            List[dict]: Query results.
        """
        with self._lock:
            view = self._views.get(name)
        if not view:
            return []
        try:
            conn = self.sos._pool.get()
            rows = conn.execute(view.sql).fetchall()
            data = [dict(r) for r in rows]
            with self._lock:
                view.data            = data
                view.last_refreshed  = time.time()
                view.dirty           = False
            return data
        except Exception as e:
            return [{"error": str(e)}]

    def query(self, name: str) -> List[dict]:
        """Return view data, refreshing if dirty."""
        with self._lock:
            view = self._views.get(name)
        if not view:
            return []
        if view.dirty or not view.data:
            return self.refresh(name)
        return view.data

    def list_views(self) -> List[dict]:
        """Return all view definitions."""
        with self._lock:
            return [{"name": v.name, "dirty": v.dirty,
                      "rows": len(v.data),
                      "sql":  v.sql[:60]}
                    for v in self._views.values()]


# ─────────────────────────────────────────────────── MVCC

class MVCCTransaction:
    """
    Multi-Version Concurrency Control snapshot transaction.

    Reads see a consistent snapshot taken at transaction start.
    Writes buffer in memory until commit.
    Conflicts detected at commit time (optimistic locking).
    """

    def __init__(self, sos: "SemanticObjectStore"):
        """Initialise an MVCC transaction."""
        self.sos       = sos
        self._snapshot: Dict[str, str] = {}   # path → content at start
        self._writes:   Dict[str, str] = {}   # path → new content
        self._start_ts = time.time()
        self._committed = False

        # Snapshot current state of all aliases
        try:
            conn = sos._pool.get()
            rows = conn.execute("SELECT path, oid FROM aliases WHERE is_dir=0").fetchall()
            for r in rows:
                self._snapshot[r["path"]] = r["oid"]
        except Exception:
            pass

    def read(self, path: str) -> str:
        """
        Read a value in the snapshot context.

        Returns the value as it was at transaction start,
        or a buffered write if this transaction wrote to path.

        Args:
            path (str): SOS path to read.

        Returns:
            str: Content at snapshot time.
        """
        if path in self._writes:
            return self._writes[path]
        # Read the snapshot version
        snap_oid = self._snapshot.get(path)
        if snap_oid:
            obj = self.sos.get(snap_oid)
            return obj.text if obj else ""
        return self.sos.read(path)

    def write(self, path: str, content: str):
        """
        Buffer a write for commit.

        Args:
            path (str): SOS path.
            content (str): New content.
        """
        self._writes[path] = content

    def commit(self) -> dict:
        """
        Commit all buffered writes.

        Detects conflicts: if any snapshot OID has changed since
        the transaction started, the commit is rejected.

        Returns:
            dict: {ok, writes, conflicts}
        """
        if self._committed:
            return {"ok": False, "error": "already committed"}

        conflicts = []
        for path in self._writes:
            snap_oid    = self._snapshot.get(path)
            current_oid = self.sos.resolve(path)
            if snap_oid and current_oid and snap_oid != current_oid:
                conflicts.append(path)

        if conflicts:
            return {"ok": False, "conflicts": conflicts}

        # Apply all writes
        for path, content in self._writes.items():
            self.sos.write(path, content)

        self._committed = True
        return {"ok": True, "writes": len(self._writes)}

    def rollback(self):
        """Discard all buffered writes."""
        self._writes.clear()
        self._committed = True
