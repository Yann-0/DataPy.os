"""
PyOS NOVA — Network Stack
===========================
REST API is DataPlane-gated (loopback by default). File server is loopback
and disabled under lockdown. SSH is paramiko-only.

REST API endpoints:
  GET  /health                 — public
  GET  /objects/:oid           — capability-gated get
  GET  /aliases/:handle        — capability-gated get
  POST /write                 — capability-gated put (handle required)
  GET  /search?q=query        — capability-gated find
  GET  /tags/:tag             — capability-gated find
"""

import os, sys, json, threading, socket, time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote
from typing import Optional, TYPE_CHECKING

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from store.sos import SemanticObjectStore
    from kernel.nova import NovaKernel


MAX_API_BODY = 1_048_576
API_TIMEOUT_S = 15


class _NovaHTTPServer(HTTPServer):
    """HTTPServer that holds a DataPlane for request-scoped sessions."""

    def __init__(self, addr, handler, dataplane, sos):
        super().__init__(addr, handler)
        self.dataplane = dataplane
        self.sos = sos


# ─────────────────────────────────────────────────── REST API
class SOSAPIHandler(BaseHTTPRequestHandler):
    """REST API handler. CRUD goes through DataPlane, never raw SOS."""

    timeout = API_TIMEOUT_S

    def log_message(self, fmt, *args):
        """Suppress default stderr access logs."""
        pass

    def _send_json(self, data, status=200):
        """Send a JSON response."""
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, msg, status=404):
        """Send a JSON error payload."""
        self._send_json({"error": msg}, status)

    def _token(self) -> Optional[str]:
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth.split(None, 1)[1].strip()
        header = self.headers.get("X-Nova-Token")
        return header.strip() if header else None

    def _plane(self):
        from store.dataplane import DataPlaneError
        plane = getattr(self.server, "dataplane", None)
        if plane is None:
            raise DataPlaneError("dataplane unavailable; denying")
        token = self._token()
        if plane.enforce and not token:
            raise DataPlaneError("capability required")
        return plane.session(token, actor="http")

    def do_OPTIONS(self):
        """CORS preflight."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS"
        )
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization, X-Nova-Token",
        )
        self.end_headers()

    def do_GET(self):
        """Authorize then read via DataPlane."""
        from store.dataplane import DataPlaneError
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = parse_qs(parsed.query)
        try:
            if path == "/health":
                self._send_json({"status": "ok", "version": "1.0.0"})
                return
            plane = self._plane()
            if path == "/stats":
                rows = plane.find(limit=1)
                self._send_json({"authorized": True, "sample": len(rows)})
            elif path.startswith("/objects/"):
                rec = plane.get(unquote(path[9:]))
                self._send_json({
                    "oid": rec.oid, "handle": rec.handle, "kind": rec.kind,
                    "size": rec.size, "version": rec.version, "tags": rec.tags,
                    "content": rec.content[:4096],
                })
            elif path.startswith("/aliases/"):
                handle = unquote(path[9:]).lstrip("/")
                rec = plane.get(handle)
                self._send_json({
                    "handle": rec.handle, "oid": rec.oid,
                    "kind": rec.kind, "size": rec.size,
                })
            elif path == "/search":
                q = params.get("q", [""])[0]
                limit = int(params.get("limit", ["20"])[0])
                results = plane.find(query=q, limit=min(max(limit, 1), 200))
                self._send_json({"query": q, "results": results})
            elif path.startswith("/tags/"):
                tag = unquote(path[6:])
                results = plane.find(tag=tag)
                self._send_json({
                    "tag": tag, "count": len(results), "results": results,
                })
            elif path == "/ls" or path.startswith("/ls/"):
                results = plane.find()
                self._send_json({"items": results})
            else:
                self._send_error("Not found", 404)
        except DataPlaneError as exc:
            self._send_error(str(exc), 403)
        except Exception as exc:
            self._send_error(str(exc), 500)

    def do_POST(self):
        """Authorize then mutate via DataPlane."""
        from store.dataplane import DataPlaneError
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        try:
            content_len = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            self._send_error("invalid Content-Length", 400)
            return
        if content_len > MAX_API_BODY:
            remaining = content_len
            while remaining:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            self._send_error("payload too large", 413)
            return
        body_bytes = self.rfile.read(content_len) if content_len else b"{}"
        try:
            data = json.loads(body_bytes)
        except Exception:
            self._send_error("Invalid JSON body", 400)
            return
        try:
            plane = self._plane()
            if path == "/objects":
                self._send_error(
                    "anonymous blob store disabled; POST /write with handle",
                    400,
                )
            elif path == "/write":
                handle = data.get("handle") or data.get("path", "")
                content = data.get("content", "")
                kind = data.get("kind", "text")
                tags = data.get("tags", [])
                if not handle:
                    self._send_error("handle required", 400)
                    return
                rec = plane.put(handle, content, kind=kind, tags=tags)
                self._send_json(
                    {"handle": rec.handle, "oid": rec.oid}, 201
                )
            else:
                self._send_error("Not found", 404)
        except DataPlaneError as exc:
            self._send_error(str(exc), 403)
        except Exception as exc:
            self._send_error(str(exc), 500)


class APIServer:
    """NOVA REST API server."""

    def __init__(self, sos: "SemanticObjectStore", port: int = 8080,
                 host: str = "127.0.0.1", dataplane=None):
        """Bind loopback by default. ``dataplane`` is required for CRUD."""
        self.sos = sos
        self.port = port
        self.host = host
        self.dataplane = dataplane
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> str:
        """Start the REST API on the configured host (loopback by default)."""
        if self.host in {"0.0.0.0", "::"}:
            self.host = "127.0.0.1"
        self._server = _NovaHTTPServer(
            (self.host, self.port), SOSAPIHandler, self.dataplane, self.sos
        )
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="nova-api"
        )
        self._thread.start()
        return f"http://{self.host}:{self.port}"

    def stop(self):
        """Stop the operation."""
        if self._server:
            self._server.shutdown()

    def _get_local_ip(self) -> str:
        """Return the local ip.


            Returns:
                str: Result.
            """
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"


# ─────────────────────────────────────────────────── HTTP file server
class FileServerHandler(BaseHTTPRequestHandler):
    """Serves files from the SOS as a simple HTTP server."""
    sos: "SemanticObjectStore" = None

    """Log message to the activity log."""
    def log_message(self, *a): pass

    def do_GET(self):
        """Do g e t."""
        path = unquote(self.path)
        sos  = self.__class__.sos

        if path == "/" or path == "":
            self._send_index()
            return
        try:
            content = sos.read_bytes(path)
            self.send_response(200)
            ct = self._content_type(path)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", len(content))
            self.end_headers()
            self.wfile.write(content)
        except Exception:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")

    def _send_index(self):
        """Send index."""
        sos  = self.__class__.sos
        items = []
        try:
            for name in sos.listdir("/home/root"):
                items.append(f'<li><a href="/home/root/{name}">{name}</a></li>')
        except Exception: pass
        html = f"<h1>PyOS NOVA File Server</h1><ul>{''.join(items)}</ul>"
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type","text/html")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _content_type(self, path: str) -> str:
        """Content type.

            Args:
            path (str): Path.


            Returns:
                str: Result.
            """
        ext = path.rsplit(".",1)[-1].lower()
        return {"py":"text/x-python","json":"application/json","md":"text/markdown",
                "txt":"text/plain","html":"text/html","css":"text/css",
                "js":"application/javascript"}.get(ext,"application/octet-stream")


class FileServer:
    """Loopback file server. Disabled when DataPlane lockdown is on."""

    def __init__(self, sos, port: int = 8081, host: str = "127.0.0.1"):
        """Initialise the instance."""
        self.sos = sos
        self.port = port
        self.host = host
        self.dataplane = None
        self._server = None
        self._thread = None

    def start(self) -> str:
        """Start on loopback. Refuse if lockdown is enabled."""
        plane = getattr(self, "dataplane", None)
        if plane is not None and getattr(plane, "enforce", False):
            raise RuntimeError("file server disabled under capability lockdown")
        if self.host in {"0.0.0.0", "::"}:
            self.host = "127.0.0.1"
        FileServerHandler.sos = self.sos
        self._server = HTTPServer((self.host, self.port), FileServerHandler)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                         daemon=True, name="nova-serve")
        self._thread.start()
        return f"http://{self.host}:{self.port}"

    def stop(self):
        """Stop the operation."""
        if self._server: self._server.shutdown()


# ─────────────────────────────────────────────────── SOS sync
class SOSSync:
    """
    Content-addressed sync between two NOVA instances.
    Only transfers OIDs that the remote doesn't have.
    Uses the REST API.
    """

    def __init__(self, sos: "SemanticObjectStore"):
        """Initialise the instance."""
        self.sos = sos

    def sync_to(self, remote_url: str, path_prefix: str = "/") -> dict:
        """Push local objects to remote."""
        import urllib.request

        pushed = 0; skipped = 0; errors = 0
        conn   = self.sos._pool.get()

        # Get all aliases under path_prefix
        rows = conn.execute(
            "SELECT path, oid FROM aliases WHERE path LIKE ?",
            (path_prefix.rstrip("/")+"%",)
        ).fetchall()

        for row in rows:
            oid = row["oid"]
            # Check if remote has it
            try:
                req = urllib.request.Request(f"{remote_url}/objects/{oid}")
                urllib.request.urlopen(req, timeout=3)
                skipped += 1
                continue
            except Exception:
                pass

            # Push it
            obj = self.sos.get(oid)
            if not obj: continue
            try:
                body = json.dumps({
                    "content": obj.text[:65536], "kind": obj.kind,
                    "meta": obj.meta, "tags": obj.tags,
                }).encode()
                req = urllib.request.Request(
                    f"{remote_url}/objects",
                    data=body, method="POST",
                    headers={"Content-Type": "application/json"},
                )
                urllib.request.urlopen(req, timeout=10)
                pushed += 1
            except Exception:
                errors += 1

        return {"pushed": pushed, "skipped": skipped, "errors": errors}

    def sync_from(self, remote_url: str, path_prefix: str = "/") -> dict:
        """Pull objects from remote that we don't have."""
        import urllib.request

        pulled = 0; skipped = 0; errors = 0
        try:
            url  = f"{remote_url}/ls{path_prefix}"
            resp = urllib.request.urlopen(url, timeout=5)
            data = json.loads(resp.read())
            items = data.get("items", [])
        except Exception as e:
            return {"error": str(e)}

        for item in items:
            path = item.get("path","")
            if not path: continue
            if self.sos.exists(path):
                skipped += 1
                continue
            try:
                resp = urllib.request.urlopen(f"{remote_url}/aliases/{path.lstrip('/')}", timeout=5)
                obj_data = json.loads(resp.read())
                oid = obj_data.get("oid","")
                if oid:
                    resp2 = urllib.request.urlopen(f"{remote_url}/objects/{oid}", timeout=5)
                    full  = json.loads(resp2.read())
                    self.sos.write(path, full.get("content",""),
                                   kind=full.get("kind","text"),
                                   tags=full.get("tags",[]))
                    pulled += 1
            except Exception:
                errors += 1

        return {"pulled": pulled, "skipped": skipped, "errors": errors}


# ─────────────────────────────────────────────────── mDNS discovery
class NOVADiscovery:
    """
    Discover other NOVA instances on the local network using UDP broadcast.
    (Simplified mDNS-like protocol without requiring zeroconf library)
    """

    PORT      = 54321
    BROADCAST = "255.255.255.255"
    ANNOUNCE  = b"NOVA_ANNOUNCE"
    QUERY     = b"NOVA_QUERY"

    def __init__(self, api_port: int = 8080):
        """Initialise the instance."""
        self.api_port  = api_port
        self._peers:   dict = {}    # ip → {hostname, api_port, seen}
        self._running  = False
        self._sock:    Optional[socket.socket] = None

    def start(self):
        """Start the operation."""
        self._running = True
        threading.Thread(target=self._listen, daemon=True, name="nova-mdns").start()
        threading.Thread(target=self._announce_loop, daemon=True).start()

    def stop(self):
        """Stop the operation."""
        self._running = False

    def _announce_loop(self):
        """Announce this node's presence."""
        while self._running:
            self._announce()
            time.sleep(15)

    def _announce(self):
        """Announce this node's presence."""
        try:
            hostname = socket.gethostname()
            payload  = json.dumps({"hostname": hostname, "api_port": self.api_port}).encode()
            msg      = self.ANNOUNCE + b":" + payload
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(msg, (self.BROADCAST, self.PORT))
            s.close()
        except Exception: pass

    def _listen(self):
        """Listen for the operation."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("", self.PORT))
            s.settimeout(1.0)
            while self._running:
                try:
                    data, addr = s.recvfrom(1024)
                    if data.startswith(self.ANNOUNCE + b":"):
                        info = json.loads(data[len(self.ANNOUNCE)+1:])
                        ip   = addr[0]
                        if ip != self._local_ip():
                            self._peers[ip] = {**info, "seen": time.time()}
                except socket.timeout: pass
        except Exception: pass

    def _local_ip(self) -> str:
        """Local ip.


            Returns:
                str: Result.
            """
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close(); return ip
        except: return "127.0.0.1"

    def peers(self) -> list:
        """Peers.


            Returns:
                list: Result.
            """
        now   = time.time()
        alive = {ip: info for ip, info in self._peers.items()
                 if now - info["seen"] < 60}
        self._peers = alive
        return [{"ip": ip, **info} for ip, info in alive.items()]

    def query(self) -> list:
        """Send a query and wait 2 seconds for responses."""
        try:
            msg = self.QUERY
            s   = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(msg, (self.BROADCAST, self.PORT))
            s.close()
        except Exception: pass
        time.sleep(2)
        return self.peers()
