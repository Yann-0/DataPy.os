"""
PyOS NOVA — Event Sourcing
============================
Every SOS write becomes an immutable event in an append-only log.
Current state is a materialised projection of all events.
Rebuild the state of the entire SOS at any past moment by replaying.

This is the Event Sourcing pattern applied to an OS filesystem:
  - Every write, delete, and tag operation is an event
  - Events are stored in the immutable SOS partition
  - The alias table is a projection (materialised view of events)
  - Projection can be rewound to any past timestamp

Benefits:
  - Perfect audit trail (who changed what, when, why)
  - Time-travel: restore any file to any past state
  - Debugging: replay from just before a bug occurred
  - Analytics: understand change patterns over time

Shell commands:
  eventsource log [--from <ts>] [--path <p>]  — show event log
  eventsource rewind <ts>                       — rewind SOS to timestamp
  eventsource snapshot                          — take a projection snapshot
  eventsource stats                             — event log statistics
"""

from __future__ import annotations
import os, sys, time, json, threading
from typing import List, Dict, Optional, Iterator, TYPE_CHECKING
from dataclasses import dataclass, field

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from store.sos import SemanticObjectStore

EVENTSOURCE_BASE = "/eventsource"
ES_LOG_PATH      = "/eventsource/events.jsonl"
ES_SNAPSHOT_PATH = "/eventsource/snapshots"


@dataclass
class SourceEvent:
    """One immutable event in the source log."""
    seq:         int
    ts:          float
    event_type:  str       # write|delete|tag|mkdir|move
    path:        str
    oid:         str       # resulting OID (for write events)
    content_ref: str       # OID of the content (separate from alias)
    user:        str
    metadata:    dict = field(default_factory=dict)

    def to_line(self) -> str:
        """Serialise to JSON line."""
        return json.dumps({
            "seq":         self.seq,
            "ts":          self.ts,
            "event_type":  self.event_type,
            "path":        self.path,
            "oid":         self.oid,
            "content_ref": self.content_ref,
            "user":        self.user,
            "metadata":    self.metadata,
        })

    @staticmethod
    def from_line(line: str) -> "SourceEvent":
        """Deserialise from JSON line."""
        d = json.loads(line)
        return SourceEvent(**d)


class EventSourceLog:
    """
    Append-only event log backed by the SOS immutable partition.

    Events are written as newline-delimited JSON to a single file
    in the immutable partition — they can never be modified or deleted.
    """

    def __init__(self, sos: "SemanticObjectStore"):
        """Initialise the event source log."""
        self.sos    = sos
        self._seq   = 0
        self._buf:  List[SourceEvent] = []
        self._lock  = threading.Lock()
        self._running = True
        self._ensure_dirs()
        self._load_seq()
        threading.Thread(target=self._flush_loop,
                          daemon=True, name="nova-es").start()

    def _ensure_dirs(self):
        """Create event source directories."""
        if not self.sos.exists(EVENTSOURCE_BASE):
            self.sos.mkdir(EVENTSOURCE_BASE, parents=True)
        if not self.sos.exists(ES_SNAPSHOT_PATH):
            self.sos.mkdir(ES_SNAPSHOT_PATH, parents=True)

    def _load_seq(self):
        """Load the current sequence number from the log."""
        try:
            log = self.sos.read(ES_LOG_PATH)
            lines = [l for l in log.splitlines() if l.strip()]
            if lines:
                last     = SourceEvent.from_line(lines[-1])
                self._seq = last.seq
        except Exception:
            self._seq = 0

    def append(self, event_type: str, path: str,
                oid: str = "", user: str = "system",
                metadata: dict = None) -> SourceEvent:
        """
        Append an event to the log.

        Args:
            event_type (str): Type of event (write/delete/tag/mkdir).
            path (str): Affected SOS path.
            oid (str): The resulting OID.
            user (str): Acting user.
            metadata (dict): Optional additional metadata.

        Returns:
            SourceEvent: The appended event.
        """
        with self._lock:
            self._seq += 1
            event = SourceEvent(
                seq        = self._seq,
                ts         = time.time(),
                event_type = event_type,
                path       = path,
                oid        = oid,
                content_ref= oid,
                user       = user,
                metadata   = metadata or {},
            )
            self._buf.append(event)
        return event

    def _flush(self):
        """Flush buffered events to the log file."""
        with self._lock:
            if not self._buf:
                return
            new_lines = "\n".join(e.to_line() for e in self._buf)
            self._buf.clear()

        try:
            existing = ""
            try:
                existing = self.sos.read(ES_LOG_PATH)
            except Exception:
                pass
            self.sos.write(ES_LOG_PATH,
                            existing + ("\n" if existing else "") + new_lines,
                            tags=["event-source-log"])
        except Exception:
            pass

    def _flush_loop(self):
        """Flush every 500ms."""
        while self._running:
            time.sleep(0.5)
            self._flush()

    def stop(self):
        """Stop and do a final flush."""
        self._running = False
        self._flush()

    def events(self, from_ts: float = None,
                path_filter: str = None,
                n: int = 200) -> List[SourceEvent]:
        """
        Read events from the log.

        Args:
            from_ts (float): Only return events after this timestamp.
            path_filter (str): Only return events for this path.
            n (int): Maximum number of events.

        Returns:
            List[SourceEvent]: Matching events.
        """
        self._flush()
        try:
            log   = self.sos.read(ES_LOG_PATH)
            lines = [l for l in log.splitlines() if l.strip()]
        except Exception:
            lines = []

        result = []
        for line in reversed(lines):
            try:
                event = SourceEvent.from_line(line)
                if from_ts and event.ts < from_ts:
                    continue
                if path_filter and event.path != path_filter:
                    continue
                result.append(event)
                if len(result) >= n:
                    break
            except Exception:
                pass
        return result

    def replay_to(self, target_ts: float) -> Dict[str, str]:
        """
        Compute the state of all aliases at a past timestamp.

        Replays all events up to target_ts and returns
        the resulting path→OID mapping.

        Args:
            target_ts (float): Target timestamp to replay to.

        Returns:
            Dict[str, str]: path → OID mapping at target_ts.
        """
        self._flush()
        try:
            log   = self.sos.read(ES_LOG_PATH)
            lines = [l for l in log.splitlines() if l.strip()]
        except Exception:
            return {}

        state: Dict[str, str] = {}
        for line in lines:
            try:
                event = SourceEvent.from_line(line)
                if event.ts > target_ts:
                    break
                if event.event_type == "write":
                    state[event.path] = event.oid
                elif event.event_type == "delete":
                    state.pop(event.path, None)
                elif event.event_type == "mkdir":
                    state[event.path] = "__dir__"
            except Exception:
                pass
        return state

    def rewind(self, target_ts: float) -> int:
        """
        Restore the SOS aliases to their state at target_ts.

        Args:
            target_ts (float): Target timestamp to rewind to.

        Returns:
            int: Number of aliases restored.
        """
        past_state = self.replay_to(target_ts)
        conn       = self.sos._pool.get()
        restored   = 0
        with conn:
            for path, oid in past_state.items():
                if oid == "__dir__":
                    continue
                # Only restore if the OID still exists
                exists = conn.execute(
                    "SELECT 1 FROM objects WHERE oid=?", (oid,)
                ).fetchone()
                if exists:
                    conn.execute(
                        "INSERT OR REPLACE INTO aliases(path,oid,is_dir)"
                        " VALUES(?,?,0)", (path, oid)
                    )
                    self.sos._path_cache.set(path, oid)
                    self.sos._obj_cache.set(oid,
                        self.sos.get(oid))
                    restored += 1
        return restored

    def take_snapshot(self) -> str:
        """
        Snapshot the current projection state.

        Returns:
            str: SOS path of the snapshot.
        """
        conn  = self.sos._pool.get()
        rows  = conn.execute(
            "SELECT path, oid FROM aliases WHERE is_dir=0"
        ).fetchall()
        snap  = {row["path"]: row["oid"] for row in rows}
        ts    = int(time.time())
        path  = f"{ES_SNAPSHOT_PATH}/snap_{ts}"
        self.sos.write(path, json.dumps(snap),
                        kind="data", tags=["es-snapshot"])
        return path

    def stats(self) -> dict:
        """Return event source statistics."""
        self._flush()
        try:
            log   = self.sos.read(ES_LOG_PATH)
            count = len([l for l in log.splitlines() if l.strip()])
        except Exception:
            count = 0
        return {
            "total_events":  count,
            "current_seq":   self._seq,
            "log_path":      ES_LOG_PATH,
            "snapshots":     len(self.sos.listdir(ES_SNAPSHOT_PATH)),
        }


def patch_sos_with_event_sourcing(
        sos: "SemanticObjectStore",
        es_log: EventSourceLog,
        user_fn=None) -> EventSourceLog:
    """
    Patch SOS to record all mutations to the event source log.

    Args:
        sos: The SemanticObjectStore to patch.
        es_log (EventSourceLog): The event source log.
        user_fn: Callable returning the current username.

    Returns:
        EventSourceLog: The active log.
    """
    orig_write  = sos.write
    orig_remove = sos.remove
    orig_mkdir  = sos.mkdir

    def _u():
        return user_fn() if user_fn else "system"

    def _write(path: str, content, **kw):
        oid = orig_write(path, content, **kw)
        es_log.append("write", path, oid=oid, user=_u())
        return oid

    def _remove(path, **kw):
        es_log.append("delete", path, user=_u())
        return orig_remove(path, **kw)

    def _mkdir(path, **kw):
        result = orig_mkdir(path, **kw)
        es_log.append("mkdir", path, user=_u())
        return result

    sos.write  = _write
    sos.remove = _remove
    sos.mkdir  = _mkdir
    return es_log
