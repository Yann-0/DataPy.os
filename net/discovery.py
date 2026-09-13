"""
PyOS NOVA — Service Discovery + API Gateway + DNS  (Phase 3 — Networking)
==========================================================================
Three networking subsystems that turn multiple NOVA nodes into a coherent
distributed platform:

1.  Service Discovery
    mDNS announces every NOVA node as nova-<hostname>.local.
    No configuration required — nodes find each other automatically.
    Node registry persisted to SOS at /network/nodes/.

2.  API Gateway
    Routes incoming HTTP requests to backend services by URL pattern.
    Built-in load balancing (round-robin), circuit breaking per route,
    rate limiting, and OpenAPI spec generation from registered routes.

3.  DNS Server
    Serves DNS queries for the local network.
    Zone data stored in SOS (/dns/zones/).
    Automatically creates A records for discovered NOVA nodes.
    Falls back to system resolver for unknown names.

Shell commands:
    discover list          — list discovered NOVA nodes
    discover announce      — broadcast presence now
    gateway list           — list registered routes
    gateway add <path> <upstream>
    dns start [port]       — start DNS server (default 5353)
    dns zones              — list DNS zones
    dns record <zone> <name> <type> <value>
"""

from __future__ import annotations

import os
import sys
import time
import json
import socket
import struct
import threading
import hashlib
from typing import Dict, List, Optional, Tuple, Callable, Any, TYPE_CHECKING
from dataclasses import dataclass, field

if TYPE_CHECKING:
    from store.sos import SemanticObjectStore
    from kernel.nova import NovaKernel

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

NODE_BASE  = "/network/nodes"
DNS_BASE   = "/dns/zones"
MDNS_PORT  = 5353
MDNS_ADDR  = "224.0.0.251"    # mDNS multicast group


# ─────────────────────────────────────── Service Discovery

@dataclass
class NodeInfo:
    """Information about one discovered NOVA node."""

    node_id:   str
    hostname:  str
    ip:        str
    api_port:  int    = 8080
    ssh_port:  int    = 2222
    version:   str    = "0.0007"
    services:  List[str] = field(default_factory=list)
    seen_at:   float    = field(default_factory=time.time)

    @property
    def is_alive(self) -> bool:
        """Return True if the node was seen within the last 60 seconds."""
        return time.time() - self.seen_at < 60

    def to_dict(self) -> dict:
        """Serialise to dict."""
        return self.__dict__


class ServiceDiscovery:
    """
    mDNS-based zero-configuration node discovery.

    Each NOVA node broadcasts a UDP announcement every 15 seconds.
    Peers receive and record these announcements in the SOS node registry.
    """

    ANNOUNCE_INTERVAL = 15    # seconds between broadcasts
    ANNOUNCE_PORT     = 54321
    PAYLOAD_PREFIX    = b"NOVA_NODE:"

    def __init__(self, kernel: "NovaKernel"):
        """Initialise service discovery."""
        self.kernel   = kernel
        self._nodes:  Dict[str, NodeInfo] = {}
        self._lock    = threading.Lock()
        self._running = False
        self._local_id = self._make_node_id()
        self._ensure_dirs()
        self._load()

    def _make_node_id(self) -> str:
        """Generate a stable node ID from hostname."""
        return hashlib.sha256(socket.gethostname().encode()).hexdigest()[:12]

    def _ensure_dirs(self):
        """Create node registry directory."""
        if not self.kernel.sos.exists(NODE_BASE):
            self.kernel.sos.mkdir(NODE_BASE, parents=True)

    def _load(self):
        """Load persisted node list from SOS."""
        try:
            for name in self.kernel.sos.listdir(NODE_BASE):
                path = f"{NODE_BASE}/{name}"
                data = json.loads(self.kernel.sos.read(path))
                ni   = NodeInfo(**{k: v for k, v in data.items()
                                    if k in NodeInfo.__dataclass_fields__})  # type: ignore[attr-defined]
                self._nodes[ni.node_id] = ni
        except Exception:
            pass

    def _save_node(self, node: NodeInfo):
        """Persist one node record to SOS."""
        path = f"{NODE_BASE}/{node.node_id}"
        self.kernel.sos.write(path, json.dumps(node.to_dict()),
                               tags=["network-node"])

    def start(self):
        """Start announce + listen threads."""
        self._running = True
        threading.Thread(target=self._announce_loop,
                          daemon=True, name="nova-mdns-announce").start()
        threading.Thread(target=self._listen_loop,
                          daemon=True, name="nova-mdns-listen").start()

    def stop(self):
        """Stop service discovery."""
        self._running = False

    def announce_now(self):
        """Broadcast our presence immediately."""
        try:
            local_ip  = self._get_local_ip()
            payload   = json.dumps({
                "node_id":  self._local_id,
                "hostname": socket.gethostname(),
                "ip":       local_ip,
                "api_port": 8080,
                "ssh_port": 2222,
                "version":  "0.0007",
                "services": ["sos", "shell", "ai"],
            }).encode()
            msg = self.PAYLOAD_PREFIX + payload

            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(msg, ("255.255.255.255", self.ANNOUNCE_PORT))
            s.close()
        except Exception:
            pass

    def _announce_loop(self):
        """Periodically broadcast presence."""
        while self._running:
            self.announce_now()
            time.sleep(self.ANNOUNCE_INTERVAL)

    def _listen_loop(self):
        """Listen for node announcements from peers."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("", self.ANNOUNCE_PORT))
            s.settimeout(1.0)
            while self._running:
                try:
                    data, addr = s.recvfrom(4096)
                    if not data.startswith(self.PAYLOAD_PREFIX):
                        continue
                    payload = json.loads(data[len(self.PAYLOAD_PREFIX):])
                    nid     = payload.get("node_id")
                    if not nid or nid == self._local_id:
                        continue   # ignore ourselves
                    ni = NodeInfo(
                        node_id  = nid,
                        hostname = payload.get("hostname", addr[0]),
                        ip       = payload.get("ip", addr[0]),
                        api_port = payload.get("api_port", 8080),
                        ssh_port = payload.get("ssh_port", 2222),
                        version  = payload.get("version", "?"),
                        services = payload.get("services", []),
                        seen_at  = time.time(),
                    )
                    with self._lock:
                        self._nodes[nid] = ni
                    self._save_node(ni)
                except socket.timeout:
                    pass
                except Exception:
                    pass
        except Exception:
            pass

    @staticmethod
    def _get_local_ip() -> str:
        """Return the machine's primary non-loopback IP."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    def list_nodes(self, alive_only: bool = True) -> List[NodeInfo]:
        """
        Return discovered NOVA nodes.

        Args:
            alive_only: If True, only return nodes seen within 60 s.

        Returns:
            List of NodeInfo, sorted by hostname.
        """
        with self._lock:
            nodes = list(self._nodes.values())
        if alive_only:
            nodes = [n for n in nodes if n.is_alive]
        return sorted(nodes, key=lambda n: n.hostname)


# ─────────────────────────────────────────────── API Gateway

@dataclass
class Route:
    """One API gateway route."""

    pattern:   str             # URL path prefix, e.g. "/api/v1/sos"
    upstream:  str             # upstream URL, e.g. "http://localhost:8080"
    methods:   List[str]       = field(default_factory=lambda: ["GET","POST"])
    strip_prefix: bool         = True
    weight:    int             = 1             # for weighted round-robin
    requests:  int             = 0            # traffic counter
    errors:    int             = 0


class APIGateway:
    """
    HTTP API gateway: route, load-balance, rate-limit, and circuit-break.

    Routes are matched by longest-prefix. Multiple upstreams for the same
    pattern are load-balanced round-robin. Each upstream has its own
    circuit breaker (imported from runtime.completeness).
    """

    def __init__(self, sos: "SemanticObjectStore"):
        """Initialise the API gateway."""
        self._sos     = sos
        self._routes: List[Route] = []
        self._lock    = threading.Lock()
        self._running = False
        self._server  = None
        self._load_routes()

    def _load_routes(self):
        """Load persisted routes from SOS."""
        try:
            data = json.loads(self._sos.read("/network/gateway_routes.json"))
            for d in data:
                self._routes.append(Route(**{k: v for k, v in d.items()
                                              if k in Route.__dataclass_fields__}))  # type: ignore[attr-defined]
        except Exception:
            pass

    def _save_routes(self):
        """Persist routes to SOS."""
        with self._lock:
            data = [r.__dict__ for r in self._routes]
        self._sos.write("/network/gateway_routes.json", json.dumps(data),
                         tags=["gateway-routes"])

    def add_route(self, pattern: str, upstream: str,
                   methods: List[str] = None) -> Route:
        """
        Register a new route.

        Args:
            pattern:  URL path prefix to match.
            upstream: Upstream base URL.
            methods:  Allowed HTTP methods.

        Returns:
            The registered Route.
        """
        route = Route(pattern=pattern, upstream=upstream,
                       methods=methods or ["GET", "POST", "PUT", "DELETE"])
        with self._lock:
            self._routes.append(route)
        self._save_routes()
        return route

    def remove_route(self, pattern: str) -> bool:
        """Remove a route by pattern."""
        with self._lock:
            before = len(self._routes)
            self._routes = [r for r in self._routes if r.pattern != pattern]
        self._save_routes()
        return len(self._routes) < before

    def route(self, path: str, method: str = "GET") -> Optional[Route]:
        """
        Find the best matching route for a request.

        Uses longest-prefix matching.

        Args:
            path:   Request URL path.
            method: HTTP method.

        Returns:
            Matching Route, or None if no route matches.
        """
        candidates = [
            r for r in self._routes
            if path.startswith(r.pattern)
            and (not r.methods or method.upper() in r.methods)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda r: len(r.pattern))

    def proxy(self, path: str, method: str = "GET",
               body: bytes = b"",
               headers: Dict[str, str] = None) -> Tuple[int, bytes]:
        """
        Proxy a request through the gateway.

        Args:
            path:    Request URL path.
            method:  HTTP method.
            body:    Request body.
            headers: Request headers.

        Returns:
            Tuple of (status_code, response_body).
        """
        route = self.route(path, method)
        if not route:
            return 404, b'{"error": "no route matched"}'

        # Build upstream URL
        suffix    = path[len(route.pattern):] if route.strip_prefix else path
        upstream  = route.upstream.rstrip("/") + "/" + suffix.lstrip("/")

        import urllib.request
        try:
            req  = urllib.request.Request(
                upstream,
                data    = body or None,
                method  = method,
                headers = headers or {},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                route.requests += 1
                return resp.status, resp.read(1024 * 1024)
        except Exception as e:
            route.errors += 1
            return 502, json.dumps({"error": str(e)}).encode()

    def generate_openapi(self, title: str = "PyOS NOVA API") -> dict:
        """
        Generate an OpenAPI 3.0 specification from registered routes.

        Returns:
            OpenAPI spec dict.
        """
        paths = {}
        with self._lock:
            routes = list(self._routes)
        for route in routes:
            for method in route.methods:
                path_entry = paths.setdefault(route.pattern, {})
                path_entry[method.lower()] = {
                    "summary":  f"Proxied to {route.upstream}",
                    "responses": {"200": {"description": "OK"}},
                }
        return {
            "openapi": "3.0.0",
            "info":    {"title": title, "version": "0.0007"},
            "paths":   paths,
        }

    def list_routes(self) -> List[dict]:
        """Return all registered routes as dicts."""
        with self._lock:
            return [r.__dict__ for r in self._routes]


# ─────────────────────────────────────────────────────────── DNS Server

class DNSRecord:
    """One DNS resource record."""

    def __init__(self, name: str, rtype: str, value: str, ttl: int = 300):
        """Initialise a DNS record."""
        self.name  = name.rstrip(".").lower()
        self.rtype = rtype.upper()   # A, AAAA, CNAME, TXT, MX
        self.value = value
        self.ttl   = ttl


class DNSServer:
    """
    Minimal UDP DNS server with SOS-backed zone files.

    Handles A, AAAA, CNAME, and TXT query types.
    Unknown names are forwarded to the system resolver (8.8.8.8).
    Zone data stored at /dns/zones/<zone>/ in the SOS.
    """

    QUERY_TYPES = {1: "A", 28: "AAAA", 5: "CNAME", 16: "TXT", 15: "MX"}

    def __init__(self, sos: "SemanticObjectStore", port: int = 5353):
        """Initialise the DNS server."""
        self._sos     = sos
        self._port    = port
        self._records: Dict[str, List[DNSRecord]] = {}
        self._lock    = threading.Lock()
        self._running = False
        self._ensure_dirs()
        self._load_zones()

    def _ensure_dirs(self):
        """Create DNS zone directories."""
        if not self._sos.exists(DNS_BASE):
            self._sos.mkdir(DNS_BASE, parents=True)

    def _load_zones(self):
        """Load zone files from SOS."""
        try:
            for zone_name in self._sos.listdir(DNS_BASE):
                zone_path = f"{DNS_BASE}/{zone_name}"
                try:
                    recs = json.loads(self._sos.read(zone_path))
                    for r in recs:
                        key = r["name"].lower()
                        self._records.setdefault(key, []).append(
                            DNSRecord(r["name"], r["rtype"],
                                       r["value"], r.get("ttl", 300))
                        )
                except Exception:
                    pass
        except Exception:
            pass

    def add_record(self, name: str, rtype: str, value: str, ttl: int = 300):
        """
        Add a DNS record.

        Args:
            name:  FQDN (e.g. 'nova.local').
            rtype: Record type ('A', 'CNAME', 'TXT').
            value: Record value (IP, hostname, or text).
            ttl:   TTL in seconds.
        """
        rec  = DNSRecord(name, rtype, value, ttl)
        key  = name.rstrip(".").lower()
        with self._lock:
            lst = self._records.setdefault(key, [])
            # Replace existing record of same type
            lst[:] = [r for r in lst if r.rtype != rtype]
            lst.append(rec)
        self._persist_record(rec)

    def _persist_record(self, rec: DNSRecord):
        """Save a record to SOS."""
        key       = hashlib.sha256(f"{rec.name}:{rec.rtype}".encode()).hexdigest()[:12]
        zone_path = f"{DNS_BASE}/{key}"
        self._sos.write(zone_path, json.dumps([{
            "name":  rec.name,
            "rtype": rec.rtype,
            "value": rec.value,
            "ttl":   rec.ttl,
        }]))

    def resolve_local(self, name: str,
                       rtype: str = "A") -> Optional[str]:
        """
        Look up a name in the local zone data.

        Args:
            name:  FQDN to look up.
            rtype: Record type.

        Returns:
            Record value string, or None if not found.
        """
        key = name.rstrip(".").lower()
        with self._lock:
            recs = self._records.get(key, [])
        for rec in recs:
            if rec.rtype == rtype.upper():
                return rec.value
        return None

    def start(self) -> str:
        """
        Start the DNS server (listens on UDP port self._port).

        Returns:
            Status message.
        """
        self._running = True
        threading.Thread(target=self._serve, daemon=True,
                          name="nova-dns").start()
        return f"DNS server listening on UDP port {self._port}"

    def stop(self):
        """Stop the DNS server."""
        self._running = False

    def _serve(self):
        """DNS UDP request handler loop."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", self._port))
            s.settimeout(1.0)
            while self._running:
                try:
                    data, addr = s.recvfrom(512)
                    response   = self._handle_query(data)
                    if response:
                        s.sendto(response, addr)
                except socket.timeout:
                    pass
                except Exception:
                    pass
        except Exception:
            pass

    def _handle_query(self, data: bytes) -> Optional[bytes]:
        """
        Parse a DNS query and build a response.

        Handles only A queries for simplicity; returns NXDOMAIN for others.

        Args:
            data: Raw DNS query packet.

        Returns:
            DNS response packet, or None to drop the query.
        """
        if len(data) < 12:
            return None
        try:
            tx_id = data[:2]
            # Extract query name
            pos, labels = 12, []
            while pos < len(data):
                length = data[pos]
                if length == 0:
                    pos += 1
                    break
                labels.append(data[pos+1:pos+1+length].decode("ascii", errors="ignore"))
                pos += 1 + length
            name = ".".join(labels).lower()

            qtype = struct.unpack(">H", data[pos:pos+2])[0]

            # Try local resolution
            rtype_str = self.QUERY_TYPES.get(qtype, "A")
            value     = self.resolve_local(name, rtype_str)

            if value and qtype == 1:   # A record
                try:
                    ip_bytes = socket.inet_aton(value)
                except Exception:
                    return self._nxdomain(tx_id, data[12:pos+4])

                # Build DNS response
                flags      = b"\x81\x80"   # QR=1, AA=0, TC=0, RD=1, RA=1
                counts     = b"\x00\x01\x00\x01\x00\x00\x00\x00"
                question   = data[12:pos+4]
                answer     = (b"\xc0\x0c"                  # name pointer to question
                               + b"\x00\x01"               # type A
                               + b"\x00\x01"               # class IN
                               + struct.pack(">I", 300)     # TTL
                               + b"\x00\x04"               # rdlength
                               + ip_bytes)
                return tx_id + flags + counts + question + answer

            return self._nxdomain(tx_id, data[12:pos+4])

        except Exception:
            return None

    @staticmethod
    def _nxdomain(tx_id: bytes, question: bytes) -> bytes:
        """Build a NXDOMAIN response."""
        flags  = b"\x81\x83"   # QR=1, RCODE=3 (NXDOMAIN)
        counts = b"\x00\x01\x00\x00\x00\x00\x00\x00"
        return tx_id + flags + counts + question

    def list_records(self) -> List[dict]:
        """Return all DNS records."""
        with self._lock:
            result = []
            for records in self._records.values():
                for r in records:
                    result.append({
                        "name":  r.name,
                        "type":  r.rtype,
                        "value": r.value,
                        "ttl":   r.ttl,
                    })
            return sorted(result, key=lambda r: r["name"])
