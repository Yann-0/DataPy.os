"""
PyOS NOVA — CRDT Distributed SOS + Process Reputation
========================================================

Part 1: CRDT Distributed SOS
------------------------------
Multiple NOVA instances write concurrently; objects merge automatically.
Uses Last-Write-Wins (LWW) Register per OID — the SOS is already
content-addressed, so the only conflict is "which alias points to what OID".

LWW-Register: each alias has a (timestamp, oid, node_id) triple.
On sync: take the triple with the highest timestamp.
Result: eventual consistency with no locks, no coordinator.

Part 2: Process Reputation & Trust Scoring
-------------------------------------------
Each process accumulates a trust score based on observed behaviour:
  - syscall patterns (reading secrets → suspicious)
  - objects accessed (crossing namespace boundaries → suspicious)
  - network calls (unexpected outbound → suspicious)
  - CPU/memory usage patterns

Low-trust processes are restricted to their own SOS namespace.

Shell commands:
  crdt sync <peer_url>       — sync with a peer NOVA instance
  crdt status                — show sync state
  crdt peers                 — list known peers
  trust status               — show process trust scores
  trust restrict <pid>       — force-restrict a process
  trust whitelist <pid>      — permanently trust a process
"""

from __future__ import annotations
import os, sys, time, json, threading, hashlib, collections
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from store.sos import SemanticObjectStore
    from kernel.nova import NovaKernel


# ─────────────────────────────────────────────────────────────────── CRDT

CRDT_META  = "/crdt/vector_clock"
PEERS_PATH = "/crdt/peers"


class LWWRegister:
    """
    Last-Write-Wins register for a single alias.
    
    Stores (timestamp, oid, node_id). Higher timestamp wins.
    Ties broken by node_id lexicographic order.
    """

    def __init__(self, path: str, oid: str,
                 ts: float, node_id: str):
        """Initialise an LWW register."""
        self.path    = path
        self.oid     = oid
        self.ts      = ts
        self.node_id = node_id

    def merge(self, other: "LWWRegister") -> "LWWRegister":
        """
        Merge this register with another.

        Args:
            other (LWWRegister): The remote register to merge.

        Returns:
            LWWRegister: The winner (highest timestamp, ties by node_id).
        """
        if other.ts > self.ts:
            return other
        if other.ts == self.ts and other.node_id > self.node_id:
            return other
        return self

    def to_dict(self) -> dict:
        """Serialize to dict."""
        return {"path": self.path, "oid": self.oid,
                "ts": self.ts, "node_id": self.node_id}

    @staticmethod
    def from_dict(d: dict) -> "LWWRegister":
        """Deserialize from dict."""
        return LWWRegister(d["path"], d["oid"], d["ts"], d["node_id"])


class CRDTStore:
    """
    CRDT-backed distributed SOS synchronisation.
    
    Wraps the local SOS with LWW-Register metadata to enable
    conflict-free merges with peer NOVA instances.
    """

    def __init__(self, sos: "SemanticObjectStore",
                 node_id: str = None):
        """Initialise the CRDT store."""
        self.sos     = sos
        self.node_id = node_id or self._default_node_id()
        self._registers: Dict[str, LWWRegister] = {}
        self._peers:     List[dict]               = []
        self._lock       = threading.Lock()
        self._ensure_dirs()
        self._load()

    def _default_node_id(self) -> str:
        """Generate a stable node ID from hostname."""
        import socket
        return hashlib.sha256(socket.gethostname().encode()).hexdigest()[:12]

    def _ensure_dirs(self):
        """Create CRDT metadata directories."""
        for path in ("/crdt", "/crdt/objects"):
            if not self.sos.exists(path):
                self.sos.mkdir(path, parents=True)

    def _load(self):
        """Load LWW register state and peer list from SOS."""
        try:
            conn = self.sos._pool.get()
            rows = conn.execute("SELECT path, oid FROM aliases WHERE is_dir=0").fetchall()
            for row in rows:
                self._registers[row["path"]] = LWWRegister(
                    row["path"], row["oid"], time.time(), self.node_id
                )
        except Exception:
            pass
        try:
            self._peers = json.loads(self.sos.read(PEERS_PATH))
        except Exception:
            self._peers = []

    def record_write(self, path: str, oid: str):
        """
        Record a local write to the CRDT register.

        Args:
            path (str): The path that was written.
            oid (str): The new OID.
        """
        with self._lock:
            self._registers[path] = LWWRegister(
                path, oid, time.time(), self.node_id
            )

    def merge_register(self, remote: LWWRegister) -> bool:
        """
        Merge a remote register. Returns True if remote wins.

        Args:
            remote (LWWRegister): Register from a peer.

        Returns:
            bool: True if the remote value was accepted.
        """
        with self._lock:
            local = self._registers.get(remote.path)
            if local is None:
                self._registers[remote.path] = remote
                # Apply to SOS
                try:
                    self.sos.alias(remote.path, remote.oid)
                    self.sos._path_cache.set(remote.path, remote.oid)
                except Exception:
                    pass
                return True
            winner = local.merge(remote)
            if winner is remote:
                self._registers[remote.path] = remote
                try:
                    self.sos.alias(remote.path, remote.oid)
                    self.sos._path_cache.set(remote.path, remote.oid)
                except Exception:
                    pass
                return True
            return False

    def export_delta(self, since_ts: float = 0) -> List[dict]:
        """
        Export all registers updated since a timestamp.

        Args:
            since_ts (float): Only include registers updated after this time.

        Returns:
            List[dict]: Serialised registers.
        """
        with self._lock:
            return [r.to_dict() for r in self._registers.values()
                    if r.ts >= since_ts]

    def sync_with_peer(self, peer_url: str) -> dict:
        """
        Synchronise with a peer NOVA instance.

        Fetches their delta, merges it, then pushes our delta.

        Args:
            peer_url (str): Base URL of the peer REST API.

        Returns:
            dict: Sync statistics.
        """
        import urllib.request
        stats = {"received": 0, "sent": 0, "conflicts": 0}

        # Get peer's delta
        try:
            last_sync = self._get_last_sync_ts(peer_url)
            url = f"{peer_url}/crdt/delta?since={last_sync}"
            resp = urllib.request.urlopen(url, timeout=10)
            remote_regs = json.loads(resp.read())
            for reg_data in remote_regs:
                remote = LWWRegister.from_dict(reg_data)
                # First sync remote object content if we don't have it
                if not self.sos.get(remote.oid):
                    try:
                        obj_resp = urllib.request.urlopen(
                            f"{peer_url}/objects/{remote.oid}", timeout=10)
                        obj_data = json.loads(obj_resp.read())
                        self.sos.store(
                            obj_data.get("content", ""),
                            kind=obj_data.get("kind", "text"),
                        )
                    except Exception:
                        pass
                accepted = self.merge_register(remote)
                stats["received"] += 1
                if not accepted:
                    stats["conflicts"] += 1
        except Exception as e:
            stats["error"] = str(e)

        # Push our delta
        try:
            our_delta = self.export_delta(self._get_last_sync_ts(peer_url))
            body = json.dumps(our_delta).encode()
            req  = urllib.request.Request(
                f"{peer_url}/crdt/merge",
                data=body, method="POST",
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=10)
            stats["sent"] = len(our_delta)
        except Exception:
            pass

        self._set_last_sync_ts(peer_url, time.time())
        return stats

    def _get_last_sync_ts(self, peer_url: str) -> float:
        """Return the timestamp of the last sync with a peer."""
        try:
            key = hashlib.sha256(peer_url.encode()).hexdigest()[:8]
            return float(self.sos.read(f"/crdt/sync_{key}"))
        except Exception:
            return 0.0

    def _set_last_sync_ts(self, peer_url: str, ts: float):
        """Record the timestamp of a sync with a peer."""
        key = hashlib.sha256(peer_url.encode()).hexdigest()[:8]
        self.sos.write(f"/crdt/sync_{key}", str(ts))

    def add_peer(self, peer_url: str):
        """Add a peer to the sync list."""
        if not any(p["url"] == peer_url for p in self._peers):
            self._peers.append({"url": peer_url, "added_at": time.time()})
            self.sos.write(PEERS_PATH, json.dumps(self._peers))

    def status(self) -> dict:
        """Return CRDT sync status."""
        return {
            "node_id":    self.node_id,
            "registers":  len(self._registers),
            "peers":      len(self._peers),
        }


# ─────────────────────────────────────────────────────────────── Reputation

REPUTATION_PATH = "/security/reputation"

BEHAVIOUR_WEIGHTS = {
    "read_secret":      -30,   # read a secret/encrypted object
    "read_credentials": -20,   # read a file matching credential pattern
    "cross_namespace":  -15,   # access outside own home dir
    "high_cpu":         -5,    # sustained high CPU usage
    "unexpected_net":   -25,   # unexpected network call
    "normal_read":      +1,    # normal file read
    "completed_task":   +5,    # task completed successfully
    "whitelisted":      +1000, # admin has whitelisted this process
}

TRUST_LEVELS = {
    (80, 100):  "trusted",
    (50, 80):   "normal",
    (20, 50):   "suspicious",
    (0, 20):    "restricted",
}


def _trust_level(score: float) -> str:
    """Return trust level label for a score."""
    for (lo, hi), label in TRUST_LEVELS.items():
        if lo <= score < hi:
            return label
    return "restricted"


class ProcessReputation:
    """
    Tracks trust scores for running processes.
    
    Observes process behaviour and adjusts trust scores accordingly.
    Low-trust processes are flagged for restriction.
    """

    def __init__(self, sos: "SemanticObjectStore"):
        """Initialise the process reputation system."""
        self.sos    = sos
        self._scores: Dict[int, dict] = {}   # pid → {score, events, name}
        self._lock  = threading.Lock()
        self._whitelist: set = {"root"}
        self._ensure_dirs()
        self._load()

    def _ensure_dirs(self):
        """Create reputation storage directory."""
        if not self.sos.exists(REPUTATION_PATH):
            self.sos.mkdir(REPUTATION_PATH, parents=True)

    def _load(self):
        """Load saved reputation data."""
        try:
            data = json.loads(self.sos.read(f"{REPUTATION_PATH}/scores.json"))
            self._scores = {int(k): v for k, v in data.items()}
        except Exception:
            pass

    def _save(self):
        """Persist reputation scores."""
        try:
            self.sos.write(f"{REPUTATION_PATH}/scores.json",
                           json.dumps(self._scores))
        except Exception:
            pass

    def _get_or_create(self, pid: int) -> dict:
        """Get or create a reputation record for a PID."""
        if pid not in self._scores:
            name = "unknown"
            try:
                import psutil
                proc = psutil.Process(pid)
                name = proc.name()
            except Exception:
                pass
            self._scores[pid] = {
                "pid": pid, "name": name,
                "score": 50.0,   # start neutral
                "events": [],
                "created_at": time.time(),
                "restricted": False,
            }
        return self._scores[pid]

    def record_event(self, pid: int, event: str, detail: str = ""):
        """
        Record a behaviour event for a process.

        Args:
            pid (int): Process ID.
            event (str): Event type key (see BEHAVIOUR_WEIGHTS).
            detail (str): Human-readable detail.
        """
        with self._lock:
            rec   = self._get_or_create(pid)
            delta = BEHAVIOUR_WEIGHTS.get(event, 0)
            rec["score"] = max(0, min(100, rec["score"] + delta))
            rec["events"].append({
                "ts": time.time(), "event": event,
                "delta": delta, "detail": detail[:80],
            })
            if len(rec["events"]) > 100:
                rec["events"] = rec["events"][-50:]
            # Auto-restrict very low trust
            if rec["score"] < 10 and not rec["restricted"]:
                rec["restricted"] = True
            self._save()

    def get_score(self, pid: int) -> float:
        """Return the trust score for a PID."""
        with self._lock:
            return self._scores.get(pid, {}).get("score", 50.0)

    def is_trusted(self, pid: int) -> bool:
        """Return True if the process has sufficient trust."""
        return self.get_score(pid) >= 20

    def is_restricted(self, pid: int) -> bool:
        """Return True if the process is restricted."""
        with self._lock:
            return self._scores.get(pid, {}).get("restricted", False)

    def whitelist(self, pid: int):
        """Permanently whitelist a process (admin action)."""
        with self._lock:
            rec = self._get_or_create(pid)
            rec["score"]      = 100.0
            rec["restricted"] = False
            self.record_event(pid, "whitelisted")

    def restrict(self, pid: int):
        """Force-restrict a process."""
        with self._lock:
            rec = self._get_or_create(pid)
            rec["restricted"] = True
            rec["score"]      = max(0, rec["score"] - 30)

    def all_scores(self) -> List[dict]:
        """Return all reputation records sorted by score."""
        with self._lock:
            recs = list(self._scores.values())
        return sorted(recs, key=lambda r: r["score"])

    def cleanup_dead_processes(self):
        """Remove reputation records for processes that no longer exist."""
        try:
            import psutil
            live_pids = set(psutil.pids())
        except ImportError:
            return
        with self._lock:
            dead = [pid for pid in self._scores if pid not in live_pids]
            for pid in dead:
                del self._scores[pid]
        if dead:
            self._save()
