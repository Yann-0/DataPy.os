"""
PyOS NOVA — Enterprise Platform  (Phases 5 & 6)
=================================================
Production-grade enterprise features:

Phase 5 — Platform:
  · DockerBuilder  — build and export OCI images (nova → Docker Hub)
  · CloudManifests — generate Kubernetes, Terraform, and docker-compose

Phase 6 — Enterprise:
  · TenantManager  — multi-tenancy: per-user SOS namespaces + quota
  · RBAC           — role-based access control (roles → capabilities)
  · RaftCluster    — leader election and distributed consensus
  · Compliance     — GDPR right-to-erasure, PHI detection, audit export
  · Observability  — Prometheus metrics endpoint + OpenTelemetry spans

Shell commands:
    tenant create <n>           — create a new tenant
    tenant list                    — list all tenants
    tenant quota <n> <mb>       — set storage quota
    rbac role create <n>        — create a role
    rbac role grant <role> <cap>   — grant a capability to a role
    rbac assign <user> <role>      — assign a role to a user
    ha status                      — show cluster consensus state
    ha elect                       — trigger a leader election
    gdpr erase <user>              — GDPR right-to-erasure
    metrics                        — show Prometheus metrics snapshot
    otel export [endpoint]         — push traces to OTLP endpoint
    deploy k8s                     — generate Kubernetes manifests
    deploy compose                 — generate docker-compose.yml
    deploy terraform               — generate Terraform module
"""

from __future__ import annotations

import os
import sys
import json
import time
import random
import hashlib
import threading
from typing import Dict, List, Optional, Set, Tuple, Any, TYPE_CHECKING
from dataclasses import dataclass, field
from enum import Enum

if TYPE_CHECKING:
    from store.sos import SemanticObjectStore
    from kernel.nova import NovaKernel

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

TENANT_BASE = "/tenants"
RBAC_BASE   = "/security/rbac"
HA_BASE     = "/ha"


# ─────────────────────────────────────────────────────────── Multi-tenancy

@dataclass
class Tenant:
    """One NOVA tenant (isolated user workspace)."""

    name:         str
    created_at:   float    = field(default_factory=time.time)
    quota_mb:     int      = 512         # storage quota in MB
    used_mb:      float    = 0.0
    enabled:      bool     = True
    roles:        List[str] = field(default_factory=list)
    sos_namespace: str     = ""          # e.g. "/tenants/alice"

    def to_dict(self) -> dict:
        """Serialise to dict."""
        return self.__dict__

    @staticmethod
    def from_dict(d: dict) -> "Tenant":
        """Deserialise from dict."""
        return Tenant(**{k: v for k, v in d.items()
                          if k in Tenant.__dataclass_fields__})  # type: ignore[attr-defined]


class TenantManager:
    """
    Manages isolated per-user namespaces with storage quota enforcement.

    Each tenant gets:
    - A private SOS namespace at /tenants/<name>/
    - A role list controlling their capabilities
    - A storage quota enforced on every write
    """

    def __init__(self, kernel: "NovaKernel"):
        """Initialise the tenant manager."""
        self.kernel   = kernel
        self._tenants: Dict[str, Tenant] = {}
        self._lock    = threading.Lock()
        self._ensure_dirs()
        self._load()

    def _ensure_dirs(self):
        """Create tenant registry directories."""
        if not self.kernel.sos.exists(TENANT_BASE):
            self.kernel.sos.mkdir(TENANT_BASE, parents=True)

    def _load(self):
        """Load tenants from SOS."""
        try:
            for name in self.kernel.sos.listdir(TENANT_BASE):
                path = f"{TENANT_BASE}/{name}/meta.json"
                if self.kernel.sos.exists(path):
                    data = json.loads(self.kernel.sos.read(path))
                    t    = Tenant.from_dict(data)
                    self._tenants[t.name] = t
        except Exception:
            pass

    def _save_tenant(self, tenant: Tenant):
        """Persist one tenant to SOS."""
        path = f"{TENANT_BASE}/{tenant.name}/meta.json"
        if not self.kernel.sos.exists(f"{TENANT_BASE}/{tenant.name}"):
            self.kernel.sos.mkdir(f"{TENANT_BASE}/{tenant.name}", parents=True)
        self.kernel.sos.write(path, json.dumps(tenant.to_dict()),
                               tags=["tenant"])

    def create(self, name: str, quota_mb: int = 512,
                roles: List[str] = None) -> Tenant:
        """
        Create a new tenant.

        Args:
            name:     Tenant identifier (username).
            quota_mb: Storage quota in MB.
            roles:    Initial role assignments.

        Returns:
            The created Tenant.
        """
        tenant = Tenant(
            name          = name,
            quota_mb      = quota_mb,
            roles         = roles or ["viewer"],
            sos_namespace = f"{TENANT_BASE}/{name}/data",
        )
        if not self.kernel.sos.exists(tenant.sos_namespace):
            self.kernel.sos.mkdir(tenant.sos_namespace, parents=True)
        with self._lock:
            self._tenants[name] = tenant
        self._save_tenant(tenant)
        return tenant

    def get(self, name: str) -> Optional[Tenant]:
        """Return a tenant by name."""
        return self._tenants.get(name)

    def set_quota(self, name: str, quota_mb: int) -> bool:
        """Set storage quota for a tenant."""
        t = self._tenants.get(name)
        if not t:
            return False
        t.quota_mb = quota_mb
        self._save_tenant(t)
        return True

    def check_quota(self, name: str, write_bytes: int) -> bool:
        """
        Return True if the tenant has quota remaining for a write.

        Args:
            name:        Tenant name.
            write_bytes: Size of the proposed write in bytes.

        Returns:
            True if write is within quota.
        """
        t = self._tenants.get(name)
        if not t:
            return True   # unknown tenants are not quota-constrained
        projected = t.used_mb + write_bytes / 1024 / 1024
        return projected <= t.quota_mb

    def record_usage(self, name: str, bytes_written: int):
        """Update a tenant's used storage counter."""
        t = self._tenants.get(name)
        if t:
            t.used_mb += bytes_written / 1024 / 1024

    def disable(self, name: str) -> bool:
        """Disable a tenant (prevents login)."""
        t = self._tenants.get(name)
        if t:
            t.enabled = False
            self._save_tenant(t)
            return True
        return False

    def list_tenants(self) -> List[Tenant]:
        """Return all tenants sorted by name."""
        with self._lock:
            return sorted(self._tenants.values(), key=lambda t: t.name)


# ─────────────────────────────────────────────────────────── RBAC

@dataclass
class Role:
    """A named collection of capability permissions."""

    name:         str
    description:  str       = ""
    capabilities: List[str] = field(default_factory=list)
    created_at:   float     = field(default_factory=time.time)

    def to_dict(self) -> dict:
        """Serialise to dict."""
        return self.__dict__


# Default NOVA roles
DEFAULT_ROLES = [
    Role("admin",
         "Full access to all subsystems",
         ["sos.read", "sos.write", "sos.delete", "exec", "net.http",
          "shell.register", "security.manage", "tenant.manage"]),
    Role("developer",
         "Read/write SOS, run code, no security management",
         ["sos.read", "sos.write", "exec", "net.http", "shell.register"]),
    Role("viewer",
         "Read-only access",
         ["sos.read"]),
    Role("operator",
         "Read/write + shell commands, no exec",
         ["sos.read", "sos.write", "shell.register"]),
]


class RBAC:
    """
    Role-Based Access Control for PyOS NOVA.

    Maps users → roles → capabilities.
    Integrates with the Capabilities subsystem for enforcement.
    """

    def __init__(self, sos: "SemanticObjectStore"):
        """Initialise RBAC."""
        self._sos          = sos
        self._roles:       Dict[str, Role]       = {}
        self._assignments: Dict[str, Set[str]]    = {}   # user → role names
        self._lock         = threading.Lock()
        self._ensure_dirs()
        self._load()
        self._install_defaults()

    def _ensure_dirs(self):
        """Create RBAC directories."""
        if not self._sos.exists(RBAC_BASE):
            self._sos.mkdir(RBAC_BASE, parents=True)

    def _load(self):
        """Load roles and assignments from SOS."""
        try:
            roles_data = json.loads(self._sos.read(f"{RBAC_BASE}/roles.json"))
            for d in roles_data:
                r = Role(**{k: v for k, v in d.items()
                             if k in Role.__dataclass_fields__})  # type: ignore[attr-defined]
                self._roles[r.name] = r
        except Exception:
            pass
        try:
            assign_data = json.loads(
                self._sos.read(f"{RBAC_BASE}/assignments.json"))
            self._assignments = {k: set(v) for k, v in assign_data.items()}
        except Exception:
            pass

    def _save(self):
        """Persist roles and assignments."""
        roles_data  = [r.to_dict() for r in self._roles.values()]
        assign_data = {k: list(v) for k, v in self._assignments.items()}
        self._sos.write(f"{RBAC_BASE}/roles.json",
                         json.dumps(roles_data, indent=2), tags=["rbac"])
        self._sos.write(f"{RBAC_BASE}/assignments.json",
                         json.dumps(assign_data, indent=2), tags=["rbac"])

    def _install_defaults(self):
        """Install default roles if not present."""
        changed = False
        for role in DEFAULT_ROLES:
            if role.name not in self._roles:
                self._roles[role.name] = role
                changed = True
        # Ensure root user has admin role
        if "root" not in self._assignments:
            self._assignments["root"] = {"admin"}
            changed = True
        if changed:
            self._save()

    def create_role(self, name: str, description: str = "",
                     capabilities: List[str] = None) -> Role:
        """
        Create a new role.

        Args:
            name:         Role identifier.
            description:  Human-readable description.
            capabilities: List of allowed capability strings.

        Returns:
            The created Role.
        """
        role = Role(name=name, description=description,
                     capabilities=capabilities or [])
        with self._lock:
            self._roles[name] = role
        self._save()
        return role

    def grant(self, role_name: str, capability: str) -> bool:
        """Add a capability to a role."""
        role = self._roles.get(role_name)
        if not role:
            return False
        if capability not in role.capabilities:
            role.capabilities.append(capability)
            self._save()
        return True

    def revoke(self, role_name: str, capability: str) -> bool:
        """Remove a capability from a role."""
        role = self._roles.get(role_name)
        if not role:
            return False
        if capability in role.capabilities:
            role.capabilities.remove(capability)
            self._save()
        return True

    def assign(self, user: str, role_name: str) -> bool:
        """Assign a role to a user."""
        if role_name not in self._roles:
            return False
        with self._lock:
            self._assignments.setdefault(user, set()).add(role_name)
        self._save()
        return True

    def unassign(self, user: str, role_name: str) -> bool:
        """Remove a role from a user."""
        with self._lock:
            roles = self._assignments.get(user, set())
            if role_name in roles:
                roles.discard(role_name)
                self._save()
                return True
        return False

    def has_capability(self, user: str, capability: str) -> bool:
        """
        Return True if user has the given capability via any of their roles.

        Args:
            user:       Username.
            capability: Capability string (e.g. 'sos.write').

        Returns:
            True if allowed.
        """
        with self._lock:
            role_names = self._assignments.get(user, set())
        for rn in role_names:
            role = self._roles.get(rn)
            if role and capability in role.capabilities:
                return True
        return False

    def user_capabilities(self, user: str) -> Set[str]:
        """Return all capabilities a user has."""
        caps: Set[str] = set()
        with self._lock:
            role_names = self._assignments.get(user, set())
        for rn in role_names:
            role = self._roles.get(rn)
            if role:
                caps.update(role.capabilities)
        return caps

    def list_roles(self) -> List[Role]:
        """Return all roles sorted by name."""
        with self._lock:
            return sorted(self._roles.values(), key=lambda r: r.name)


# ─────────────────────────────────────────────────────────── Raft (HA)

class RaftState(Enum):
    """Raft node state."""
    FOLLOWER  = "follower"
    CANDIDATE = "candidate"
    LEADER    = "leader"


class RaftNode:
    """Raft leader-election helper. Not a replicated log.

    DataPy.os does not claim committed replicated SOS data from this
    module. Election and heartbeat state is not a durable consensus
    contract. Unsupported log operations fail explicitly.
    """

    HEARTBEAT_MS   = 150
    ELECTION_MIN   = 300
    ELECTION_MAX   = 600

    def __init__(self, node_id: str, peers: List[str],
                  sos: "SemanticObjectStore"):
        """
        Initialise a Raft node.

        Args:
            node_id: Unique identifier for this node.
            peers:   List of peer node API URLs.
            sos:     SOS for persisting Raft state.
        """
        self.node_id    = node_id
        self.peers      = list(peers)
        self._sos       = sos
        self._state     = RaftState.FOLLOWER
        self._term      = 0
        self._voted_for: Optional[str] = None
        self._leader:   Optional[str] = None
        self._votes     = 0
        self._last_hb   = time.time()
        self._lock      = threading.Lock()
        self._running   = False
        self._load_state()

    @staticmethod
    def majority(total_nodes: int) -> int:
        """Return floor(N/2)+1 votes required, including even cluster sizes."""
        if total_nodes < 1:
            return 1
        return total_nodes // 2 + 1

    def append_entries(self, *args, **kwargs) -> None:
        """Replicated-log API is not implemented on the supported single-node path."""
        raise NotImplementedError(
            "Raft log replication is experimental and not part of the "
            "supported single-node DataPy.os release"
        )

    def _load_state(self):
        """Load persisted Raft state from SOS."""
        try:
            data = json.loads(self._sos.read(f"{HA_BASE}/{self.node_id}.json"))
            self._term      = data.get("term", 0)
            self._voted_for = data.get("voted_for")
        except Exception:
            pass

    def _save_state(self):
        """Persist Raft state (durable write required by Raft safety)."""
        try:
            if not self._sos.exists(HA_BASE):
                self._sos.mkdir(HA_BASE, parents=True)
            self._sos.write(
                f"{HA_BASE}/{self.node_id}.json",
                json.dumps({
                    "term":      self._term,
                    "voted_for": self._voted_for,
                    "leader":    self._leader,
                    "state":     self._state.value,
                }),
            )
        except Exception:
            pass

    def start(self):
        """Start election timer and heartbeat threads."""
        self._running = True
        threading.Thread(target=self._election_loop,
                          daemon=True, name="nova-raft-election").start()
        threading.Thread(target=self._heartbeat_loop,
                          daemon=True, name="nova-raft-hb").start()

    def stop(self):
        """Stop the Raft node."""
        self._running = False

    def _election_loop(self):
        """Trigger elections when no heartbeat is received."""
        while self._running:
            timeout = random.uniform(self.ELECTION_MIN, self.ELECTION_MAX) / 1000
            time.sleep(timeout)
            with self._lock:
                if (self._state != RaftState.LEADER and
                        time.time() - self._last_hb > timeout):
                    self._start_election()

    def _start_election(self):
        """Become candidate and request votes from peers."""
        self._term      += 1
        self._state      = RaftState.CANDIDATE
        self._voted_for  = self.node_id
        self._votes      = 1   # vote for ourselves
        self._save_state()

        import urllib.request
        quorum = self.majority(len(self.peers) + 1)

        for peer_url in self.peers:
            try:
                req  = urllib.request.Request(
                    f"{peer_url}/raft/vote",
                    data=json.dumps({"term": self._term,
                                      "candidate": self.node_id}).encode(),
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
                resp = urllib.request.urlopen(req, timeout=0.1)
                data = json.loads(resp.read())
                if data.get("vote_granted"):
                    self._votes += 1
            except Exception:
                pass

        if self._votes >= quorum:
            self._become_leader()

    def _become_leader(self):
        """Transition to leader state."""
        self._state  = RaftState.LEADER
        self._leader = self.node_id
        self._save_state()

    def _heartbeat_loop(self):
        """Send heartbeats to followers when we are leader."""
        while self._running:
            time.sleep(self.HEARTBEAT_MS / 1000)
            if self._state == RaftState.LEADER:
                self._send_heartbeats()

    def _send_heartbeats(self):
        """Broadcast heartbeat to all peers."""
        import urllib.request
        for peer_url in self.peers:
            try:
                req = urllib.request.Request(
                    f"{peer_url}/raft/heartbeat",
                    data=json.dumps({"term":   self._term,
                                      "leader": self.node_id}).encode(),
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
                urllib.request.urlopen(req, timeout=0.1)
            except Exception:
                pass

    def receive_heartbeat(self, leader: str, term: int):
        """Process an incoming heartbeat from a leader."""
        with self._lock:
            if term >= self._term:
                self._term     = term
                self._state    = RaftState.FOLLOWER
                self._leader   = leader
                self._last_hb  = time.time()

    def receive_vote_request(self, candidate: str, term: int) -> bool:
        """
        Process a vote request.

        Returns True if we grant the vote.
        """
        with self._lock:
            if term < self._term:
                return False
            if term > self._term:
                self._term      = term
                self._voted_for = None
                self._state     = RaftState.FOLLOWER
            if self._voted_for in (None, candidate):
                self._voted_for = candidate
                self._last_hb   = time.time()
                self._save_state()
                return True
            return False

    @property
    def is_leader(self) -> bool:
        """Return True if this node is the current leader."""
        return self._state == RaftState.LEADER

    def status(self) -> dict:
        """Return node status."""
        return {
            "node_id": self.node_id,
            "state":   self._state.value,
            "term":    self._term,
            "leader":  self._leader,
            "peers":   len(self.peers),
        }


# ─────────────────────────────────────────────────────────── Compliance

class ComplianceManager:
    """
    Handles GDPR, HIPAA, and SOC 2 compliance operations.
    """

    # PHI (Protected Health Information) patterns
    PHI_PATTERNS = [
        r"\b\d{3}-\d{2}-\d{4}\b",              # SSN
        r"\b(?:diagnosis|prescription|dob|patient[_-]?id)\b",
        r"\b(?:ICD-?\d|CPT-?\d)",               # medical codes
        r"\b\d{11}\b",                           # NHS number
    ]

    def __init__(self, sos: "SemanticObjectStore"):
        """Initialise the compliance manager."""
        self._sos = sos
        import re
        self._phi_re = [re.compile(p, re.IGNORECASE) for p in self.PHI_PATTERNS]

    def erase_user(self, username: str) -> dict:
        """
        GDPR Article 17 right to erasure — delete all data for a user.

        Scans the SOS for objects tagged with the username, removes them,
        and records the erasure in the immutable audit trail.

        Args:
            username: The user whose data should be erased.

        Returns:
            dict with count of erased objects.
        """
        erased_ok = 0
        retained = []
        errors = []
        try:
            tagged = self._sos.find_by_tag(username, fuzzy=False)
            for item in tagged:
                path = item
                if len(item) == 32 and all(c in "0123456789abcdef" for c in item):
                    # Legacy OID; resolve aliases first.
                    conn = self._sos._pool.get()
                    paths = [r[0] for r in conn.execute(
                        "SELECT path FROM aliases WHERE oid=?", (item,)
                    ).fetchall()]
                else:
                    paths = [path]
                for pth in paths:
                    try:
                        if self._sos.exists(pth):
                            self._sos.remove(pth)
                            if not self._sos.exists(pth):
                                erased_ok += 1
                            else:
                                retained.append(pth)
                    except Exception as exc:
                        errors.append(f"{pth}: {exc}")
        except Exception as exc:
            errors.append(str(exc))

        record = {
            "user": username,
            "erased": erased_ok,
            "erased_objects": erased_ok,
            "retained": retained,
            "errors": errors,
            "ts": time.time(),
            "soft_delete": True,
            "complete_erasure": False,
            "note": "shared blobs and revision history are retained",
        }
        try:
            self._sos.write(
                f"/compliance/erasures/{username}_{int(time.time())}",
                json.dumps(record),
                tags=["gdpr-erasure"],
            )
        except Exception as exc:
            errors.append(f"audit write failed: {exc}")
            record["errors"] = errors
        return record

    def scan_phi(self, content: str) -> List[str]:
        """
        Scan text for PHI patterns.

        Args:
            content: Text to scan.

        Returns:
            List of matched PHI pattern names.
        """
        matches = []
        for i, pattern in enumerate(self._phi_re):
            if pattern.search(content):
                matches.append(self.PHI_PATTERNS[i])
        return matches

    def export_audit_log(self, since_ts: float = None) -> str:
        """
        Export audit log as SIEM-compatible JSON Lines.

        Args:
            since_ts: Only include entries after this timestamp.

        Returns:
            JSON Lines string.
        """
        lines = []
        try:
            conn   = self._sos._pool.get()
            rows   = conn.execute(
                "SELECT path, meta_json, created_at FROM aliases "
                "JOIN objects USING(oid) WHERE tags_json LIKE '%audit%'"
            ).fetchall()
            for row in rows:
                ts = row[2]
                if since_ts and ts < since_ts:
                    continue
                try:
                    meta = json.loads(row[1])
                    lines.append(json.dumps({
                        "@timestamp": time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
                        "path":      row[0],
                        **meta,
                    }))
                except Exception:
                    pass
        except Exception:
            pass
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────── Observability

class Observability:
    """
    Prometheus metrics endpoint + OpenTelemetry span export.
    """

    def __init__(self, kernel: "NovaKernel"):
        """Initialise the observability subsystem."""
        self.kernel    = kernel
        self._counters: Dict[str, float] = {}
        self._gauges:   Dict[str, float] = {}
        self._lock      = threading.Lock()

    def increment(self, name: str, value: float = 1.0,
                   labels: Dict[str, str] = None):
        """Increment a counter metric."""
        key = self._key(name, labels)
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + value

    def gauge(self, name: str, value: float,
               labels: Dict[str, str] = None):
        """Set a gauge metric."""
        key = self._key(name, labels)
        with self._lock:
            self._gauges[key] = value

    @staticmethod
    def _key(name: str, labels: Optional[Dict[str, str]]) -> str:
        """Build a Prometheus-style metric key with labels."""
        if not labels:
            return name
        label_str = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
        return f'{name}{{{label_str}}}'

    def prometheus_text(self) -> str:
        """
        Generate Prometheus text format metrics from current system state.

        Returns:
            Prometheus exposition format string.
        """
        lines = [
            "# HELP nova_info PyOS NOVA instance information",
            "# TYPE nova_info gauge",
            f'nova_info{{version="0.0007"}} 1',
        ]

        try:
            import psutil
            cpu   = psutil.cpu_percent(interval=0)
            ram   = psutil.virtual_memory()
            disk  = psutil.disk_usage("/")

            lines += [
                "# HELP nova_cpu_percent CPU usage percent",
                "# TYPE nova_cpu_percent gauge",
                f"nova_cpu_percent {cpu}",
                "# HELP nova_ram_bytes RAM usage in bytes",
                "# TYPE nova_ram_bytes gauge",
                f"nova_ram_bytes {ram.used}",
                f"nova_ram_total_bytes {ram.total}",
                "# HELP nova_disk_bytes Disk usage in bytes",
                "# TYPE nova_disk_bytes gauge",
                f"nova_disk_bytes {disk.used}",
                f"nova_disk_total_bytes {disk.total}",
            ]
        except ImportError:
            pass

        # SOS metrics
        try:
            conn  = self.kernel.sos._pool.get()
            n_obj = conn.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
            n_ali = conn.execute("SELECT COUNT(*) FROM aliases").fetchone()[0]
            lines += [
                "# HELP nova_sos_objects Total SOS objects",
                "# TYPE nova_sos_objects gauge",
                f"nova_sos_objects {n_obj}",
                "# HELP nova_sos_aliases Total SOS aliases",
                "# TYPE nova_sos_aliases gauge",
                f"nova_sos_aliases {n_ali}",
            ]
        except Exception:
            pass

        # Custom counters and gauges
        with self._lock:
            for key, val in self._counters.items():
                lines.append(f"nova_counter_{key} {val}")
            for key, val in self._gauges.items():
                lines.append(f"nova_gauge_{key} {val}")

        return "\n".join(lines)

    def export_otlp(self, endpoint: str = "http://localhost:4317") -> bool:
        """
        Export collected traces to an OpenTelemetry OTLP endpoint.

        Args:
            endpoint: OTLP HTTP endpoint URL.

        Returns:
            True if export succeeded.
        """
        try:
            tracer = getattr(self.kernel, "tracer", None)
            if not tracer:
                return False

            # Collect recent spans
            traces = tracer.recent_traces(n=50)
            if not traces:
                return True

            # Build minimal OTLP JSON payload
            resource_spans = []
            for trace_summary in traces:
                spans = tracer.get_trace(trace_summary["id"])
                otlp_spans = [
                    {
                        "traceId":       s.trace_id,
                        "spanId":        s.span_id,
                        "parentSpanId":  s.parent_id or "",
                        "name":          s.name,
                        "startTimeUnixNano": int(s.started_at * 1e9),
                        "endTimeUnixNano":   int((s.ended_at or time.time()) * 1e9),
                        "status": {"code": 2 if s.error else 1},
                    }
                    for s in spans
                ]
                resource_spans.append({
                    "resource": {"attributes": [
                        {"key": "service.name", "value": {"stringValue": "nova"}}
                    ]},
                    "scopeSpans": [{"spans": otlp_spans}],
                })

            payload = json.dumps({"resourceSpans": resource_spans}).encode()

            import urllib.request
            req = urllib.request.Request(
                f"{endpoint}/v1/traces",
                data=payload, method="POST",
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=5)
            return True

        except Exception:
            return False

    def start_metrics_server(self, port: int = 9090) -> str:
        """
        Start an HTTP server serving Prometheus metrics at /metrics.

        Args:
            port: HTTP port.

        Returns:
            Server URL.
        """
        from http.server import HTTPServer, BaseHTTPRequestHandler
        obs = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == "/metrics":
                    body = obs.prometheus_text().encode()
                    self.send_response(200)
                    self.send_header("Content-Type",
                                      "text/plain; version=0.0.4")
                    self.send_header("Content-Length", len(body))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()

        srv = HTTPServer(("127.0.0.1", port), _Handler)
        threading.Thread(target=srv.serve_forever, daemon=True,
                          name="nova-metrics").start()
        return f"http://127.0.0.1:{port}/metrics"


# ─────────────────────────────────────────────────────────── Cloud Manifests

class CloudManifests:
    """Generates deployment manifests for Kubernetes, Terraform, and Docker Compose."""

    def kubernetes(self, image: str = "ghcr.io/nova-os/nova:0.0007",
                    replicas: int = 1) -> str:
        """
        Generate Kubernetes manifests for deploying NOVA.

        Args:
            image:    Container image reference.
            replicas: Number of pod replicas.

        Returns:
            YAML manifest string.
        """
        return f"""---
# PyOS NOVA — Kubernetes Deployment  (v0.0007)
# kubectl apply -f nova.yaml

apiVersion: v1
kind: Namespace
metadata:
  name: nova-os

---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: nova
  namespace: nova-os
spec:
  serviceName: nova
  replicas: {replicas}
  selector:
    matchLabels:
      app: nova
  template:
    metadata:
      labels:
        app: nova
    spec:
      containers:
      - name: nova
        image: {image}
        ports:
        - containerPort: 8080
          name: api
        - containerPort: 2222
          name: ssh
        - containerPort: 9090
          name: metrics
        env:
        - name: NOVA_DATA
          value: /data
        volumeMounts:
        - name: nova-data
          mountPath: /data
        resources:
          requests:
            memory: "256Mi"
            cpu: "250m"
          limits:
            memory: "2Gi"
            cpu: "2"
        livenessProbe:
          httpGet:
            path: /health
            port: 8080
          initialDelaySeconds: 15
          periodSeconds: 20
        readinessProbe:
          httpGet:
            path: /ready
            port: 8080
          initialDelaySeconds: 5
          periodSeconds: 10
  volumeClaimTemplates:
  - metadata:
      name: nova-data
    spec:
      accessModes: ["ReadWriteOnce"]
      resources:
        requests:
          storage: 10Gi

---
apiVersion: v1
kind: Service
metadata:
  name: nova
  namespace: nova-os
spec:
  selector:
    app: nova
  ports:
  - name: api
    port: 8080
  - name: ssh
    port: 2222
  - name: metrics
    port: 9090
  type: ClusterIP
"""

    def docker_compose(self) -> str:
        """Generate a docker-compose.yml for local deployment."""
        return """# PyOS NOVA — Docker Compose  (v0.0007)
# docker compose up -d

version: "3.9"
services:
  nova:
    image: ghcr.io/nova-os/nova:0.0007
    restart: unless-stopped
    ports:
      - "8080:8080"   # REST API + web UI
      - "2222:2222"   # SSH server
      - "9090:9090"   # Prometheus metrics
    volumes:
      - nova-data:/data
    environment:
      NOVA_DATA: /data
      NOVA_MODEL: /models/default.gguf
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/health"]
      interval: 30s
      timeout: 10s
      retries: 3

  prometheus:
    image: prom/prometheus:latest
    restart: unless-stopped
    ports:
      - "9091:9090"
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml
    depends_on:
      - nova

  grafana:
    image: grafana/grafana:latest
    restart: unless-stopped
    ports:
      - "3000:3000"
    environment:
      GF_SECURITY_ADMIN_PASSWORD: nova
    depends_on:
      - prometheus

volumes:
  nova-data:
"""

    def terraform(self, provider: str = "aws",
                   region: str = "us-east-1") -> str:
        """Generate a Terraform module for cloud deployment."""
        return f"""# PyOS NOVA — Terraform Module  (v0.0007)
# terraform init && terraform apply

terraform {{
  required_providers {{
    {provider} = {{
      source  = "hashicorp/{provider}"
      version = "~> 5.0"
    }}
  }}
}}

provider "{provider}" {{
  region = "{region}"
}}

resource "aws_instance" "nova" {{
  ami           = "ami-0c55b159cbfafe1f0"  # Amazon Linux 2023
  instance_type = "t3.medium"

  user_data = <<-EOF
    #!/bin/bash
    curl -sL https://nova-os.dev/install.sh | bash
    systemctl enable --now nova
  EOF

  tags = {{
    Name    = "nova-os"
    Version = "0.0007"
  }}
}}

output "nova_public_ip" {{
  value = aws_instance.nova.public_ip
}}

output "nova_api_url" {{
  value = "http://\\${{aws_instance.nova.public_ip}}:8080"
}}
"""
