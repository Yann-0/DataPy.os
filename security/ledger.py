"""
PyOS NOVA — Tamper-Evident Audit Trail
========================================
Every SOS access (read, write, delete, crypto operation, login)
is appended to a Merkle-chained log.

Each entry hashes the previous entry's hash + its own content.
Any alteration of a past entry breaks the chain — `audit verify`
detects it instantly.

Structure:
  Entry N:  hash = sha256(Entry[N-1].hash + Entry[N].payload)

The chain root is published to /security/audit/root after every
batch flush so external systems can verify integrity.

Shell commands:
  audit log              — show recent audit entries
  audit verify           — verify chain integrity (detects tampering)
  audit since <time>     — show entries since ISO timestamp
  audit search <query>   — search audit log
  audit export <path>    — export log as JSON
"""

from __future__ import annotations
import os, sys, json, time, hashlib, threading
from dataclasses import dataclass, asdict
from typing import List, Optional, Iterator, TYPE_CHECKING

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from store.sos import SemanticObjectStore

AUDIT_BASE   = "/security/audit"
CHAIN_PATH   = "/security/audit/chain.jsonl"   # newline-delimited JSON
ROOT_PATH    = "/security/audit/root"           # current chain root hash
GENESIS_HASH = "0" * 64                         # sentinel for first entry

EVENTS = {
    "read":     "Object read",
    "write":    "Object written",
    "delete":   "Object deleted",
    "login":    "User login",
    "logout":   "User logout",
    "auth_ok":  "Authentication successful",
    "auth_fail":"Authentication failed",
    "cap_grant":"Capability granted",
    "cap_revoke":"Capability revoked",
    "crypto":   "Crypto operation",
    "fix":      "System fix applied",
    "rollback": "Fix rolled back",
    "agent":    "Agent action",
    "import":   "Object imported",
    "export":   "Object exported",
    "cmd":      "Command executed",
    "network":  "Network operation",
    "boot":     "System boot",
    "shutdown": "System shutdown",
}


@dataclass
class AuditEntry:
    """A single audit log entry in the Merkle chain."""

    seq:       int            # sequence number (monotonically increasing)
    timestamp: float          # unix timestamp
    event:     str            # event type key
    user:      str            # acting user
    path:      str            # affected path or resource
    detail:    str            # human-readable detail
    prev_hash: str            # hash of previous entry
    entry_hash: str = ""      # sha256(prev_hash + payload) — set after creation

    def _payload(self) -> str:
        """Return the canonical payload string for hashing."""
        return json.dumps({
            "seq":       self.seq,
            "timestamp": self.timestamp,
            "event":     self.event,
            "user":      self.user,
            "path":      self.path,
            "detail":    self.detail,
            "prev_hash": self.prev_hash,
        }, separators=(",", ":"), sort_keys=True)

    def compute_hash(self) -> str:
        """Compute and return this entry's hash."""
        data = (self.prev_hash + self._payload()).encode()
        return hashlib.sha256(data).hexdigest()

    def to_dict(self) -> dict:
        """Serialize to dict."""
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "AuditEntry":
        """Deserialize from dict."""
        return AuditEntry(**d)


class AuditTrail:
    """
    Merkle-chained append-only audit log.
    
    Entries are buffered in memory and flushed to SOS periodically
    or on explicit flush(). The chain hash is updated on every append.
    Thread-safe.
    """

    FLUSH_INTERVAL = 10   # seconds between auto-flushes

    def __init__(self, sos: "SemanticObjectStore"):
        """Initialise the audit trail with a reference to the SOS."""
        self.sos       = sos
        self._lock     = threading.Lock()
        self._buffer:  List[AuditEntry] = []
        self._seq      = 0
        self._tip_hash = GENESIS_HASH
        self._ensure_dirs()
        self._load_state()
        # Start auto-flush thread
        self._running  = True
        threading.Thread(target=self._flush_loop, daemon=True,
                          name="nova-audit").start()

    def _ensure_dirs(self):
        """Create audit directory."""
        if not self.sos.exists(AUDIT_BASE):
            self.sos.mkdir(AUDIT_BASE, parents=True)

    def _load_state(self):
        """Load the current chain tip from SOS."""
        try:
            root = self.sos.read(ROOT_PATH).strip()
            # Find last seq by counting lines in chain
            if self.sos.exists(CHAIN_PATH):
                chain = self.sos.read(CHAIN_PATH)
                lines = [l for l in chain.splitlines() if l.strip()]
                if lines:
                    last = AuditEntry.from_dict(json.loads(lines[-1]))
                    self._seq      = last.seq
                    self._tip_hash = last.entry_hash
        except Exception:
            self._seq      = 0
            self._tip_hash = GENESIS_HASH

    def append(self, event: str, user: str, path: str = "",
               detail: str = ""):
        """
        Append a new event to the audit trail.

        Args:
            event (str): Event type key (see EVENTS dict).
            user (str): The user performing the action.
            path (str): The affected SOS path or resource.
            detail (str): Human-readable description.
        """
        with self._lock:
            self._seq += 1
            entry = AuditEntry(
                seq        = self._seq,
                timestamp  = time.time(),
                event      = event,
                user       = user,
                path       = path,
                detail     = detail,
                prev_hash  = self._tip_hash,
            )
            entry.entry_hash = entry.compute_hash()
            self._tip_hash   = entry.entry_hash
            self._buffer.append(entry)

    def flush(self):
        """Flush buffered entries to the SOS."""
        with self._lock:
            if not self._buffer:
                return
            # Append to existing chain file
            existing = ""
            try:
                existing = self.sos.read(CHAIN_PATH)
            except Exception:
                pass
            new_lines = "\n".join(
                json.dumps(e.to_dict()) for e in self._buffer
            )
            self.sos.write(CHAIN_PATH,
                           existing + ("\n" if existing else "") + new_lines,
                           tags=["audit-chain"])
            # Update root
            self.sos.write(ROOT_PATH, self._tip_hash, tags=["audit-root"])
            self._buffer.clear()

    def _flush_loop(self):
        """Background thread that flushes the buffer periodically."""
        while self._running:
            time.sleep(self.FLUSH_INTERVAL)
            try:
                self.flush()
            except Exception:
                pass

    def verify(self) -> tuple[bool, str]:
        """
        Verify the entire chain integrity.

        Replays every entry and checks that each hash is correct
        and the prev_hash chain is unbroken.

        Returns:
            Tuple[bool, str]: (is_valid, message)
        """
        self.flush()   # ensure buffer is persisted
        try:
            chain   = self.sos.read(CHAIN_PATH)
        except Exception:
            return True, "Chain is empty — nothing to verify."

        lines     = [l for l in chain.splitlines() if l.strip()]
        prev_hash = GENESIS_HASH
        for i, line in enumerate(lines):
            try:
                entry = AuditEntry.from_dict(json.loads(line))
            except Exception as e:
                return False, f"Entry {i+1}: parse error — {e}"

            # Check prev_hash linkage
            if entry.prev_hash != prev_hash:
                return False, (
                    f"Chain broken at entry {entry.seq} "
                    f"(seq {entry.seq}): prev_hash mismatch. "
                    f"Expected {prev_hash[:16]}... got {entry.prev_hash[:16]}..."
                )
            # Verify this entry's hash
            expected = entry.compute_hash()
            if entry.entry_hash != expected:
                return False, (
                    f"Entry {entry.seq} hash invalid — "
                    f"entry has been tampered with."
                )
            prev_hash = entry.entry_hash

        return True, f"Chain verified — {len(lines)} entries, integrity intact."

    def recent(self, n: int = 50) -> List[AuditEntry]:
        """Return the N most recent audit entries."""
        self.flush()
        try:
            chain = self.sos.read(CHAIN_PATH)
        except Exception:
            return list(self._buffer[-n:])
        lines   = [l for l in chain.splitlines() if l.strip()]
        entries = []
        for line in lines[-n:]:
            try:
                entries.append(AuditEntry.from_dict(json.loads(line)))
            except Exception:
                pass
        return entries

    def search(self, query: str, n: int = 50) -> List[AuditEntry]:
        """Search audit entries by path, user, event, or detail."""
        query = query.lower()
        results = []
        for entry in self.recent(1000):
            if any(query in str(getattr(entry, f, "")).lower()
                   for f in ("event","user","path","detail")):
                results.append(entry)
                if len(results) >= n:
                    break
        return results

    def since(self, ts: float, n: int = 200) -> List[AuditEntry]:
        """Return entries since the given timestamp."""
        return [e for e in self.recent(n) if e.timestamp >= ts]

    def stop(self):
        """Stop the background flush thread and do a final flush."""
        self._running = False
        self.flush()


# ── Audit decorator ────────────────────────────────────────────────────────

def audited(event: str, path_arg: int = None, detail_fn=None):
    """
    Decorator that automatically logs a function call to the audit trail.

    Args:
        event (str): The event type to log.
        path_arg (int): Index of the argument that is the path (optional).
        detail_fn: Callable that takes (args, kwargs, result) and returns detail string.
    """
    import functools

    def decorator(fn):
        """Apply the audit logging decorator to a function."""
        @functools.wraps(fn)
        def wrapper(self_or_first, *args, **kwargs):
            result = fn(self_or_first, *args, **kwargs)
            trail  = getattr(self_or_first, "_audit", None)
            if trail:
                path   = args[path_arg] if path_arg is not None and path_arg < len(args) else ""
                detail = detail_fn(args, kwargs, result) if detail_fn else ""
                trail.append(event, user="system", path=str(path), detail=detail)
            return result
        return wrapper
    return decorator


def patch_sos_with_audit(sos, trail: AuditTrail, user_fn=None):
    """
    Monkey-patch SOS read/write/remove to log every access.

    Args:
        sos: The SemanticObjectStore instance to patch.
        trail (AuditTrail): The audit trail to log to.
        user_fn: Callable that returns the current username (optional).
    """
    orig_write  = sos.write
    orig_read   = sos.read
    orig_remove = sos.remove

    def _user():
        return user_fn() if user_fn else "system"

    def _write(path, content, **kw):
        result = orig_write(path, content, **kw)
        trail.append("write", _user(), path, f"{len(content)} bytes")
        return result

    def _read(path):
        result = orig_read(path)
        trail.append("read", _user(), path)
        return result

    def _remove(path, **kw):
        trail.append("delete", _user(), path, "before removal")
        return orig_remove(path, **kw)

    sos.write  = _write
    sos.read   = _read
    sos.remove = _remove
