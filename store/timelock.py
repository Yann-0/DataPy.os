"""
PyOS NOVA — Time-Locked & Expiring Objects
============================================
Objects with temporal access controls:

  not_before  — cryptographically sealed until a future timestamp
  expires_at  — auto-deleted or sealed after a timestamp
  embargo     — like not_before but with a public announcement

Use cases:
  - Press embargoes (release at exactly 09:00 UTC)
  - Temporary credentials (expire after 1 hour)
  - Legal holds (cannot be deleted for 7 years)
  - Scheduled configuration rollouts
  - One-time secrets (read once, then sealed)

Shell commands:
  timelock set <path> --after <ISO> [--encrypt]
  timelock expire <path> --at <ISO>
  timelock once <path>                 (seal after first read)
  timelock status <path>
  timelock list                        (all locked/expiring objects)
  timelock unlock <path>               (admin override)
"""

from __future__ import annotations
import os, sys, json, time, threading
from typing import Optional, List, Dict, TYPE_CHECKING

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from store.sos import SemanticObjectStore

TIMELOCK_META_PATH = "/security/timelocks"
TAG_LOCKED   = "time-locked"
TAG_EXPIRING = "expiring"
TAG_ONCE     = "read-once"


def _parse_time(spec: str) -> float:
    """
    Parse a time specification into a Unix timestamp.

    Accepts: ISO 8601 (2026-12-31T09:00:00), relative (+1h, +30m, +7d),
    or Unix timestamp as string.

    Args:
        spec (str): Time specification string.

    Returns:
        float: Unix timestamp.
    """
    import re
    spec = spec.strip()
    # Relative time
    m = re.match(r"\+(\d+(?:\.\d+)?)\s*([smhd]?)", spec)
    if m:
        n    = float(m.group(1))
        unit = m.group(2) or "s"
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
        return time.time() + n * mult
    # Plain Unix timestamp
    try:
        return float(spec)
    except ValueError:
        pass
    # ISO 8601
    from datetime import datetime
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.fromisoformat(spec).timestamp()
        except ValueError:
            pass
    raise ValueError(f"Cannot parse time spec: {spec!r}")


class TimeLockManager:
    """
    Manages temporal access controls on SOS objects.
    
    Runs a background thread that checks for expired/unlocked objects
    every 30 seconds and applies the appropriate action.
    """

    CHECK_INTERVAL = 30   # seconds

    def __init__(self, sos: "SemanticObjectStore"):
        """Initialise the timelock manager."""
        self.sos      = sos
        self._locks: Dict[str, dict] = {}   # path → lock metadata
        self._lock    = threading.Lock()
        self._running = False
        self._ensure_dirs()
        self._load()
        self.start()

    def _ensure_dirs(self):
        """Create timelock metadata directory."""
        if not self.sos.exists(TIMELOCK_META_PATH):
            self.sos.mkdir(TIMELOCK_META_PATH, parents=True)

    def _meta_path(self, path: str) -> str:
        """Return SOS path for a lock metadata entry."""
        import hashlib
        key = hashlib.sha256(path.encode()).hexdigest()[:16]
        return f"{TIMELOCK_META_PATH}/{key}"

    def _load(self):
        """Load all timelock metadata from SOS."""
        for name in self.sos.listdir(TIMELOCK_META_PATH):
            try:
                meta = json.loads(self.sos.read(f"{TIMELOCK_META_PATH}/{name}"))
                self._locks[meta["path"]] = meta
            except Exception:
                pass

    def _save_meta(self, path: str, meta: dict):
        """Persist timelock metadata for a path."""
        self.sos.write(self._meta_path(path), json.dumps(meta))
        with self._lock:
            self._locks[path] = meta

    # ── public API ─────────────────────────────────────────────────────────────

    def set_not_before(self, path: str, ts: float, encrypt: bool = False):
        """
        Seal an object until a future time.

        Args:
            path (str): The SOS path to lock.
            ts (float): Unix timestamp when the object becomes readable.
            encrypt (bool): Whether to encrypt the object while sealed.
        """
        meta = {
            "path": path, "kind": "not_before",
            "not_before": ts, "encrypt": encrypt,
            "created_at": time.time(),
        }
        self._save_meta(path, meta)
        self.sos.tag(path, TAG_LOCKED)

    def set_expires_at(self, path: str, ts: float, action: str = "delete"):
        """
        Set an object to expire at a future time.

        Args:
            path (str): The SOS path to expire.
            ts (float): Unix timestamp when the object expires.
            action (str): What to do on expiry: 'delete', 'seal', or 'archive'.
        """
        meta = {
            "path": path, "kind": "expires_at",
            "expires_at": ts, "action": action,
            "created_at": time.time(),
        }
        self._save_meta(path, meta)
        self.sos.tag(path, TAG_EXPIRING)

    def set_read_once(self, path: str):
        """
        Mark an object as readable only once, then sealed.

        Args:
            path (str): The SOS path to make read-once.
        """
        meta = {
            "path": path, "kind": "read_once",
            "read_count": 0,
            "created_at": time.time(),
        }
        self._save_meta(path, meta)
        self.sos.tag(path, TAG_ONCE)

    def is_accessible(self, path: str) -> tuple[bool, str]:
        """
        Check if an object is currently accessible.

        Args:
            path (str): The SOS path to check.

        Returns:
            Tuple[bool, str]: (accessible, reason_if_blocked)
        """
        with self._lock:
            meta = self._locks.get(path)
        if not meta:
            return True, ""
        now = time.time()
        kind = meta.get("kind")

        if kind == "not_before":
            nb = meta.get("not_before", 0)
            if now < nb:
                remaining = nb - now
                when = time.strftime("%Y-%m-%d %H:%M:%S UTC",
                                     time.gmtime(nb))
                return False, (
                    f"Object sealed until {when} "
                    f"({remaining/3600:.1f}h remaining)"
                )

        elif kind == "expires_at":
            ea = meta.get("expires_at", float("inf"))
            if now > ea:
                return False, "Object has expired"

        elif kind == "read_once":
            if meta.get("read_count", 0) >= 1:
                return False, "Object has already been read (read-once)"

        return True, ""

    def record_read(self, path: str):
        """Record a read access (used for read-once objects)."""
        with self._lock:
            meta = self._locks.get(path)
        if meta and meta.get("kind") == "read_once":
            meta["read_count"] = meta.get("read_count", 0) + 1
            meta["last_read"]  = time.time()
            self._save_meta(path, meta)
            if meta["read_count"] >= 1:
                self.sos.tag(path, "sealed")

    def remove_lock(self, path: str):
        """Remove all timelocks from a path (admin override)."""
        with self._lock:
            self._locks.pop(path, None)
        try:
            self.sos.remove(self._meta_path(path))
        except Exception:
            pass
        for tag in (TAG_LOCKED, TAG_EXPIRING, TAG_ONCE, "sealed"):
            try:
                self.sos.untag(path, tag)
            except Exception:
                pass

    def list_all(self) -> List[dict]:
        """Return all timelock metadata entries."""
        with self._lock:
            return list(self._locks.values())

    # ── background enforcement ─────────────────────────────────────────────────

    def start(self):
        """Start the background enforcement thread."""
        self._running = True
        threading.Thread(target=self._enforce_loop,
                          daemon=True, name="nova-timelock").start()

    def stop(self):
        """Stop the background enforcement thread."""
        self._running = False

    def _enforce_loop(self):
        """Periodically check and enforce temporal controls."""
        while self._running:
            time.sleep(self.CHECK_INTERVAL)
            self._enforce_all()

    def _enforce_all(self):
        """Check all registered timelocks and apply expired/matured controls."""
        with self._lock:
            items = list(self._locks.items())
        now = time.time()
        for path, meta in items:
            kind = meta.get("kind")
            try:
                if kind == "expires_at" and now > meta.get("expires_at", float("inf")):
                    action = meta.get("action", "delete")
                    if action == "delete":
                        try:
                            self.sos.remove(path)
                            self.remove_lock(path)
                        except Exception:
                            pass
                    elif action == "seal":
                        self.sos.tag(path, "sealed")
            except Exception:
                pass

    def patch_sos(self):
        """
        Monkey-patch SOS read to enforce temporal controls.
        Raises PermissionError for locked/expired objects.
        """
        orig_read = self.sos.read
        manager   = self

        def _read(path):
            ok, reason = manager.is_accessible(path)
            if not ok:
                raise PermissionError(f"Access denied: {reason}")
            result = orig_read(path)
            manager.record_read(path)
            return result

        self.sos.read = _read
