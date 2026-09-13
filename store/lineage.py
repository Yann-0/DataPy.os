"""
PyOS NOVA — Data Lineage & Provenance
========================================
Track the full history of every object: who created it,
which process wrote it, what it was derived from, what it produced.

Every write records:
  - which process (pid, name) performed the write
  - which objects were read before the write (inputs)
  - the resulting object (output)

`lineage <path>` renders a DAG showing the full provenance chain.
This turns the SOS into a full data governance platform.

Shell commands:
  lineage <path>           — show full lineage DAG
  lineage sources <path>   — what objects was this derived from?
  lineage impacts <path>   — what objects did this produce?
  lineage who <path>       — which process/user last wrote this?
  lineage graph            — full system lineage graph
"""

from __future__ import annotations
import os, sys, json, time, threading
from typing import List, Dict, Optional, Set, TYPE_CHECKING
from dataclasses import dataclass, asdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from store.sos import SemanticObjectStore

LINEAGE_BASE = "/lineage"
_THREAD_READS = threading.local()   # per-thread read tracking


@dataclass
class LineageRecord:
    """Provenance record for one object write."""

    oid:         str            # OID of the written object
    path:        str            # alias path
    writer_pid:  int            # process ID
    writer_name: str            # process name
    writer_user: str            # username
    timestamp:   float
    input_oids:  List[str]      # OIDs that were read before this write
    input_paths: List[str]      # corresponding paths
    parent_oid:  Optional[str]  # previous version OID

    def to_dict(self) -> dict:
        """Serialize to dict."""
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "LineageRecord":
        """Deserialize from dict."""
        return LineageRecord(**d)


class LineageTracker:
    """
    Tracks data lineage across all SOS operations.
    
    Maintains a provenance graph: objects as nodes,
    derivation relationships as directed edges.
    """

    def __init__(self, sos: "SemanticObjectStore"):
        """Initialise the lineage tracker."""
        self.sos   = sos
        self._lock = threading.Lock()
        self._ensure_dirs()

    def _ensure_dirs(self):
        """Create lineage directory."""
        if not self.sos.exists(LINEAGE_BASE):
            self.sos.mkdir(LINEAGE_BASE, parents=True)

    def _record_path(self, oid: str) -> str:
        """Return SOS path for a lineage record."""
        return f"{LINEAGE_BASE}/{oid}"

    # ── read tracking ──────────────────────────────────────────────────────────

    def begin_session(self):
        """Start tracking reads for the current thread (call before a write)."""
        _THREAD_READS.reads = []

    def track_read(self, path: str, oid: str):
        """Record that the current thread read this object."""
        if not hasattr(_THREAD_READS, "reads"):
            _THREAD_READS.reads = []
        _THREAD_READS.reads.append((path, oid))

    def end_session(self) -> List[tuple]:
        """Return and clear the current thread's read list."""
        reads = getattr(_THREAD_READS, "reads", [])
        _THREAD_READS.reads = []
        return reads

    # ── record writes ─────────────────────────────────────────────────────────

    def record_write(self, path: str, oid: str,
                     parent_oid: str = None,
                     user: str = "system"):
        """
        Record provenance for a write operation.

        Args:
            path (str): The SOS path being written.
            oid (str): The new OID of the written object.
            parent_oid (str): Previous version OID, if any.
            user (str): The user performing the write.
        """
        reads = self.end_session()
        try:
            import os as _os
            pid  = _os.getpid()
            name = "python3"
        except Exception:
            pid  = 0
            name = "unknown"

        record = LineageRecord(
            oid          = oid,
            path         = path,
            writer_pid   = pid,
            writer_name  = name,
            writer_user  = user,
            timestamp    = time.time(),
            input_oids   = [r[1] for r in reads if r[1] != oid],
            input_paths  = [r[0] for r in reads if r[1] != oid],
            parent_oid   = parent_oid,
        )
        with self._lock:
            try:
                self.sos.write(
                    self._record_path(oid),
                    json.dumps(record.to_dict()),
                    tags=["lineage", user],
                )
                # Store reverse links: each input → this output
                for inp_oid in record.input_oids:
                    self._add_output_link(inp_oid, oid)
            except Exception:
                pass

    def _add_output_link(self, input_oid: str, output_oid: str):
        """Record that input_oid produced output_oid."""
        link_path = f"{LINEAGE_BASE}/_out_{input_oid}"
        try:
            existing = json.loads(self.sos.read(link_path))
        except Exception:
            existing = []
        if output_oid not in existing:
            existing.append(output_oid)
            self.sos.write(link_path, json.dumps(existing))

    # ── query API ─────────────────────────────────────────────────────────────

    def get_record(self, oid: str) -> Optional[LineageRecord]:
        """Return the lineage record for an OID."""
        try:
            data = json.loads(self.sos.read(self._record_path(oid)))
            return LineageRecord.from_dict(data)
        except Exception:
            return None

    def sources(self, path: str, depth: int = 5) -> List[LineageRecord]:
        """
        Return the chain of objects this path was derived from.

        Args:
            path (str): The SOS path to trace backwards.
            depth (int): Maximum depth of backward traversal.

        Returns:
            List[LineageRecord]: Chain of records from immediate parent to origin.
        """
        oid = self.sos.resolve(path)
        if not oid:
            return []
        result = []
        visited: Set[str] = set()
        queue = [oid]
        for _ in range(depth):
            if not queue:
                break
            cur = queue.pop(0)
            if cur in visited:
                continue
            visited.add(cur)
            rec = self.get_record(cur)
            if not rec:
                continue
            result.append(rec)
            queue.extend(rec.input_oids)
            if rec.parent_oid and rec.parent_oid not in visited:
                queue.append(rec.parent_oid)
        return result

    def impacts(self, path: str, depth: int = 5) -> List[LineageRecord]:
        """
        Return objects derived from this path (forward lineage).

        Args:
            path (str): The SOS path to trace forwards.
            depth (int): Maximum depth of forward traversal.

        Returns:
            List[LineageRecord]: Records of objects derived from this one.
        """
        oid = self.sos.resolve(path)
        if not oid:
            return []
        result   = []
        visited: Set[str] = set()
        queue    = [oid]
        for _ in range(depth):
            if not queue:
                break
            cur = queue.pop(0)
            if cur in visited:
                continue
            visited.add(cur)
            link_path = f"{LINEAGE_BASE}/_out_{cur}"
            try:
                output_oids = json.loads(self.sos.read(link_path))
            except Exception:
                output_oids = []
            for out_oid in output_oids:
                rec = self.get_record(out_oid)
                if rec:
                    result.append(rec)
                    queue.append(out_oid)
        return result

    def who_wrote(self, path: str) -> Optional[LineageRecord]:
        """Return the most recent lineage record for a path."""
        oid = self.sos.resolve(path)
        if not oid:
            return None
        return self.get_record(oid)

    def render_dag(self, path: str, depth: int = 3) -> str:
        """
        Render an ASCII lineage DAG for a path.

        Args:
            path (str): The path to render lineage for.
            depth (int): Depth of backward traversal.

        Returns:
            str: ASCII DAG representation.
        """
        lines = [f"\n  Lineage: {path}", "  " + "─"*50]
        rec = self.who_wrote(path)
        if rec:
            ts  = time.strftime("%Y-%m-%d %H:%M", time.localtime(rec.timestamp))
            lines.append(f"  Written by : {rec.writer_user} (pid {rec.writer_pid})")
            lines.append(f"  Time       : {ts}")
        chain = self.sources(path, depth)
        if chain:
            lines.append(f"\n  Sources ({len(chain)}):")
            for r in chain:
                lines.append(f"    ← {r.path}  [{r.writer_user}]")
        fwd = self.impacts(path, depth)
        if fwd:
            lines.append(f"\n  Derived objects ({len(fwd)}):")
            for r in fwd:
                lines.append(f"    → {r.path}  [{r.writer_user}]")
        if not chain and not fwd:
            lines.append("  No lineage recorded for this object.")
        return "\n".join(lines) + "\n"


def patch_sos_with_lineage(sos, tracker: LineageTracker,
                            user_fn=None):
    """
    Monkey-patch SOS to automatically track lineage.

    Args:
        sos: The SemanticObjectStore to patch.
        tracker (LineageTracker): The lineage tracker instance.
        user_fn: Callable returning current username.
    """
    orig_read  = sos.read
    orig_write = sos.write

    def _user():
        return user_fn() if user_fn else "system"

    def _read(path):
        result = orig_read(path)
        oid    = sos.resolve(path)
        if oid:
            tracker.track_read(path, oid)
        return result

    def _write(path, content, **kw):
        old_oid = sos.resolve(path)
        new_oid = orig_write(path, content, **kw)
        tracker.record_write(path, new_oid, parent_oid=old_oid, user=_user())
        return new_oid

    sos.read  = _read
    sos.write = _write
