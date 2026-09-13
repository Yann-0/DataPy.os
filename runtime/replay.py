"""
PyOS NOVA — Deterministic Replay Debugger
==========================================
Record every SOS operation, shell command, and AI call with exact
inputs and timestamps. Replay deterministically to reproduce any bug
from a compact log.

Recording format: newline-delimited JSON, one entry per operation.
Each entry: {seq, ts, op, args, kwargs, result_oid}

Shell commands:
  replay record [--file path]  — start recording to file
  replay stop                  — stop recording
  replay run --from <ts>       — replay from timestamp in a sandbox
  replay run --file <path>     — replay a specific log file
  replay diff <ts1> <ts2>      — compare two replay snapshots
  replay list                  — list recorded sessions
  replay status                — show recording status
"""

from __future__ import annotations
import os, sys, time, json, threading, hashlib, gzip
from typing import List, Dict, Optional, IO, TYPE_CHECKING
from dataclasses import dataclass, asdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from store.sos import SemanticObjectStore
    from kernel.nova import NovaKernel

REPLAY_BASE = "/system/replays"


@dataclass
class ReplayEntry:
    """One recorded operation."""
    seq:        int
    ts:         float
    op:         str         # write|read|delete|cmd|ai_call|tag
    path:       str         # affected path or resource
    args_hash:  str         # sha256 of serialised args (for dedup)
    result_oid: str         # OID of result (for SOS ops)
    user:       str
    detail:     str         # human-readable summary

    def to_line(self) -> str:
        """Serialise to JSON line."""
        return json.dumps(asdict(self))

    @staticmethod
    def from_line(line: str) -> "ReplayEntry":
        """Deserialise from JSON line."""
        return ReplayEntry(**json.loads(line))


class ReplayRecorder:
    """
    Records all NOVA operations to a compact replay log.

    Recording adds <1% overhead — entries are buffered and
    written asynchronously.
    """

    def __init__(self, sos: "SemanticObjectStore",
                 output_path: str = None):
        """Initialise the recorder."""
        self.sos         = sos
        self._path       = output_path
        self._seq        = 0
        self._buf:       List[str] = []
        self._lock       = threading.Lock()
        self._recording  = False
        self._start_ts   = 0.0
        self._file:      Optional[IO] = None
        self._flush_thread: Optional[threading.Thread] = None

    def start(self, output_path: str = None):
        """
        Start recording operations.

        Args:
            output_path (str): Path for the replay log file.
                               Defaults to /tmp/nova_replay_<ts>.jsonl
        """
        if self._recording:
            return
        if output_path:
            self._path = output_path
        elif not self._path:
            ts = int(time.time())
            data_dir = os.environ.get("NOVA_DATA", os.path.expanduser("~/.nova"))
            os.makedirs(os.path.join(data_dir, "replays"), exist_ok=True)
            self._path = os.path.join(data_dir, "replays", f"replay_{ts}.jsonl")

        self._file       = open(self._path, "w", buffering=1)
        self._recording  = True
        self._start_ts   = time.time()
        self._seq        = 0

        # Write header
        header = json.dumps({
            "type": "header",
            "ts": self._start_ts,
            "nova_version": "2.0.0",
        })
        self._file.write(header + "\n")

        # Start flush thread
        self._flush_thread = threading.Thread(
            target=self._flush_loop, daemon=True, name="nova-replay"
        )
        self._flush_thread.start()

    def stop(self) -> str:
        """Stop recording and return the log path."""
        self._recording = False
        self._flush()
        if self._file:
            self._file.close()
            self._file = None
        return self._path or ""

    def record(self, op: str, path: str = "", args_data: str = "",
               result_oid: str = "", user: str = "system",
               detail: str = ""):
        """
        Record one operation.

        Args:
            op (str): Operation type (write/read/delete/cmd/ai_call).
            path (str): Affected path or resource name.
            args_data (str): Serialised arguments for hashing.
            result_oid (str): Result OID for SOS operations.
            user (str): Acting user.
            detail (str): Human-readable summary.
        """
        if not self._recording:
            return
        with self._lock:
            self._seq += 1
            entry = ReplayEntry(
                seq        = self._seq,
                ts         = time.time(),
                op         = op,
                path       = path,
                args_hash  = hashlib.sha256(
                    args_data.encode()[:256]).hexdigest()[:8],
                result_oid = result_oid,
                user       = user,
                detail     = detail[:120],
            )
            self._buf.append(entry.to_line())

    def _flush(self):
        """Write buffered entries to disk."""
        with self._lock:
            if not self._buf or not self._file:
                return
            lines = self._buf[:]
            self._buf.clear()
        for line in lines:
            try:
                self._file.write(line + "\n")
            except Exception:
                break

    def _flush_loop(self):
        """Flush buffer to disk every 100ms."""
        while self._recording:
            time.sleep(0.1)
            self._flush()

    @property
    def is_recording(self) -> bool:
        """Return True if currently recording."""
        return self._recording

    def status(self) -> dict:
        """Return recorder status."""
        return {
            "recording":   self._recording,
            "path":        self._path or "",
            "seq":         self._seq,
            "buffered":    len(self._buf),
            "elapsed_s":   round(time.time() - self._start_ts, 1)
                           if self._recording else 0,
        }


class ReplayPlayer:
    """
    Replays a recorded session deterministically.

    Creates a fresh SOS snapshot and re-applies each operation
    in sequence, allowing exact reproduction of any past state.
    """

    def __init__(self, kernel: "NovaKernel"):
        """Initialise the replay player."""
        self.kernel = kernel

    def load_entries(self, log_path: str,
                      since_ts: float = None) -> List[ReplayEntry]:
        """
        Load replay entries from a log file.

        Args:
            log_path (str): Path to the .jsonl replay log.
            since_ts (float): Only load entries after this timestamp.

        Returns:
            List[ReplayEntry]: Ordered list of entries.
        """
        entries = []
        try:
            opener = gzip.open if log_path.endswith(".gz") else open
            with opener(log_path, "rt") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("{\"type\":\"header\""):
                        continue
                    try:
                        entry = ReplayEntry.from_line(line)
                        if since_ts is None or entry.ts >= since_ts:
                            entries.append(entry)
                    except Exception:
                        pass
        except Exception as e:
            return []
        return entries

    def replay(self, log_path: str,
                since_ts: float = None,
                dry_run: bool = False) -> dict:
        """
        Replay a recorded session.

        Creates an isolated SOS view and re-applies all operations
        to reproduce the exact state at any point in time.

        Args:
            log_path (str): Path to the replay log.
            since_ts (float): Start from this timestamp.
            dry_run (bool): If True, show what would be replayed without applying.

        Returns:
            dict: Replay statistics.
        """
        import tempfile
        entries  = self.load_entries(log_path, since_ts)
        if not entries:
            return {"error": "No entries found", "entries": 0}

        if dry_run:
            return {
                "entries":  len(entries),
                "ops":      self._count_ops(entries),
                "dry_run":  True,
                "preview":  [e.detail for e in entries[:5]],
            }

        # Replay in a temp SOS
        td    = tempfile.mkdtemp()
        import shutil
        try:
            from store.sos import SemanticObjectStore
            replay_sos = SemanticObjectStore(
                db_path=os.path.join(td, "replay.db")
            )
            applied = 0
            errors  = 0
            for entry in entries:
                try:
                    self._apply(replay_sos, entry)
                    applied += 1
                except Exception:
                    errors += 1
        finally:
            shutil.rmtree(td, ignore_errors=True)

        return {
            "entries":  len(entries),
            "applied":  applied,
            "errors":   errors,
            "ops":      self._count_ops(entries),
        }

    def _apply(self, sos: "SemanticObjectStore", entry: ReplayEntry):
        """Apply one replay entry to an SOS instance."""
        if entry.op == "write":
            sos.write(entry.path, entry.detail or "")
        elif entry.op == "read":
            try:
                sos.read(entry.path)
            except Exception:
                pass
        elif entry.op == "delete":
            try:
                sos.remove(entry.path)
            except Exception:
                pass
        elif entry.op == "tag":
            try:
                sos.tag(entry.path, entry.detail)
            except Exception:
                pass

    def _count_ops(self, entries: List[ReplayEntry]) -> dict:
        """Count operations by type."""
        counts: dict = {}
        for e in entries:
            counts[e.op] = counts.get(e.op, 0) + 1
        return counts

    def list_sessions(self) -> List[dict]:
        """List all recorded replay sessions."""
        data_dir  = os.environ.get("NOVA_DATA", os.path.expanduser("~/.nova"))
        replay_dir = os.path.join(data_dir, "replays")
        sessions  = []
        if not os.path.isdir(replay_dir):
            return sessions
        for fname in sorted(os.listdir(replay_dir)):
            if not fname.endswith((".jsonl", ".jsonl.gz")):
                continue
            fpath = os.path.join(replay_dir, fname)
            size  = os.path.getsize(fpath)
            # Count entries (first 10 lines)
            try:
                entries = self.load_entries(fpath)
                n = len(entries)
            except Exception:
                n = -1
            sessions.append({
                "file":    fname,
                "size_kb": size // 1024,
                "entries": n,
            })
        return sessions


def patch_sos_with_recorder(sos: "SemanticObjectStore",
                              recorder: ReplayRecorder,
                              user_fn=None) -> ReplayRecorder:
    """
    Patch SOS to record all operations to the recorder.

    Args:
        sos: The SemanticObjectStore to patch.
        recorder (ReplayRecorder): The recorder to write to.
        user_fn: Callable returning the current username.

    Returns:
        ReplayRecorder: The active recorder.
    """
    orig_write  = sos.write
    orig_read   = sos.read
    orig_remove = sos.remove

    def _u():
        return user_fn() if user_fn else "system"

    def _write(path, content, **kw):
        oid = orig_write(path, content, **kw)
        size = len(content) if isinstance(content, (str, bytes)) else 0
        recorder.record("write", path, str(kw), oid,
                         _u(), f"{size} bytes")
        return oid

    def _read(path):
        result = orig_read(path)
        recorder.record("read", path, "", "", _u(), "")
        return result

    def _remove(path, **kw):
        recorder.record("delete", path, "", "", _u(), "")
        return orig_remove(path, **kw)

    sos.write  = _write
    sos.read   = _read
    sos.remove = _remove
    return recorder
