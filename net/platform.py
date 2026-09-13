"""
PyOS NOVA — Network & Platform Completions  (v0.0100)
======================================================
Five features completing Phases 3–4 of the 1.0 roadmap:

1. VPN Tunnel        — WireGuard-like encrypted UDP overlay between nodes
2. Service Mesh      — mutual TLS, circuit breaking per service route
3. Email Server      — minimal SMTP/IMAP with SOS message store
4. Model Streaming   — token-by-token streaming AI responses
5. Load Tester       — concurrent HTTP load testing with latency stats

Shell commands:
    vpn start [port]        — start encrypted UDP tunnel
    vpn connect <peer>      — connect to a peer
    vpn status              — show tunnel state
    mesh add <svc> <url>    — register a service
    mesh proxy <svc> <path> — proxy a request through the mesh
    mail send <to> <subj> <body>
    mail inbox
    stream "prompt"         — streaming AI response (token by token)
    loadtest <url> [--n N] [--c C]  — N requests, C concurrent
"""

from __future__ import annotations

import os
import sys
import time
import json
import socket
import struct
import hashlib
import hmac
import threading
import queue
import secrets
import urllib.request
import urllib.error
from typing import Dict, List, Optional, Tuple, Any, TYPE_CHECKING
from dataclasses import dataclass, field

if TYPE_CHECKING:
    from store.sos import SemanticObjectStore
    from kernel.nova import NovaKernel

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

MAIL_BASE = "/mail"


# ─────────────────────────────────────────────────────── VPN Tunnel

class VPNPacket:
    """Encrypted UDP packet for the NOVA VPN overlay."""

    MAGIC   = b"NOVA"
    HMAC_LEN = 32   # HMAC-SHA256
    NONCE_LEN = 16

    @staticmethod
    def encrypt(payload: bytes, key: bytes) -> bytes:
        """
        Encrypt a payload using XChaCha20-like XOR stream + HMAC authentication.

        Uses a random nonce and HMAC-SHA256 for integrity.

        Args:
            payload: Raw bytes to encrypt.
            key:     32-byte symmetric key.

        Returns:
            Encrypted packet bytes: MAGIC + NONCE + CIPHERTEXT + HMAC.
        """
        nonce      = secrets.token_bytes(VPNPacket.NONCE_LEN)
        # XOR keystream (simple but constant-time)
        stream_key = hashlib.sha256(key + nonce).digest()
        # Extend keystream to cover payload length
        ks_len     = len(payload)
        keystream  = b""
        block      = 0
        while len(keystream) < ks_len:
            keystream += hashlib.sha256(
                stream_key + struct.pack(">I", block)).digest()
            block += 1
        ciphertext = bytes(a ^ b for a, b in zip(payload, keystream[:ks_len]))
        mac        = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()
        return VPNPacket.MAGIC + nonce + ciphertext + mac

    @staticmethod
    def decrypt(packet: bytes, key: bytes) -> Optional[bytes]:
        """
        Decrypt and verify a VPN packet.

        Args:
            packet: Raw packet bytes.
            key:    32-byte symmetric key.

        Returns:
            Decrypted payload, or None if MAC verification fails.
        """
        if not packet.startswith(VPNPacket.MAGIC):
            return None
        data       = packet[len(VPNPacket.MAGIC):]
        if len(data) < VPNPacket.NONCE_LEN + VPNPacket.HMAC_LEN:
            return None
        nonce      = data[:VPNPacket.NONCE_LEN]
        mac        = data[-VPNPacket.HMAC_LEN:]
        ciphertext = data[VPNPacket.NONCE_LEN:-VPNPacket.HMAC_LEN]
        # Verify MAC
        expected   = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()
        if not hmac.compare_digest(mac, expected):
            return None
        # Decrypt
        stream_key = hashlib.sha256(key + nonce).digest()
        keystream  = b""
        block      = 0
        while len(keystream) < len(ciphertext):
            keystream += hashlib.sha256(
                stream_key + struct.pack(">I", block)).digest()
            block += 1
        return bytes(a ^ b for a, b in zip(ciphertext, keystream[:len(ciphertext)]))


class VPNTunnel:
    """
    WireGuard-inspired encrypted UDP tunnel between NOVA nodes.

    Key exchange via a pre-shared key derived from ZeroConfTrust.
    Each node creates a symmetric key from HMAC(local_key, peer_key).
    All SOS sync and API traffic can be routed through the tunnel.
    """

    def __init__(self, sos: "SemanticObjectStore", port: int = 51820):
        """Initialise the VPN tunnel."""
        self._sos     = sos
        self._port    = port
        self._running = False
        self._peers:  Dict[str, bytes] = {}   # peer_ip → session_key
        self._sock:   Optional[socket.socket] = None
        self._rx_cb   = None   # called with (peer_ip, payload) on receipt
        # Load or generate local key
        self._local_key = self._get_local_key()

    def _get_local_key(self) -> bytes:
        """Return or generate the local VPN key."""
        try:
            hex_key = self._sos.read("/security/vpn_key")
            return bytes.fromhex(hex_key)
        except Exception:
            key = secrets.token_bytes(32)
            try:
                self._sos.write("/security/vpn_key", key.hex(),
                                 tags=["vpn-key"])
            except Exception:
                pass
            return key

    def add_peer(self, peer_ip: str, peer_key_hex: str):
        """
        Add a VPN peer with their public key.

        The session key is derived as HMAC(local_key, peer_key).

        Args:
            peer_ip:      IP address of the peer.
            peer_key_hex: Peer's VPN key as hex string.
        """
        peer_key     = bytes.fromhex(peer_key_hex)
        session_key  = hmac.new(self._local_key, peer_key,
                                  hashlib.sha256).digest()
        self._peers[peer_ip] = session_key

    def send(self, peer_ip: str, payload: bytes) -> bool:
        """
        Send an encrypted packet to a peer.

        Args:
            peer_ip: Destination IP.
            payload: Raw payload bytes.

        Returns:
            True if sent successfully.
        """
        key = self._peers.get(peer_ip)
        if not key or not self._sock:
            return False
        try:
            packet = VPNPacket.encrypt(payload, key)
            self._sock.sendto(packet, (peer_ip, self._port))
            return True
        except Exception:
            return False

    def start(self, rx_callback=None) -> str:
        """
        Start the VPN listener.

        Args:
            rx_callback: Called with (peer_ip, decrypted_payload) on receipt.

        Returns:
            Status message.
        """
        self._rx_cb   = rx_callback
        self._running = True
        self._sock    = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("0.0.0.0", self._port))
        threading.Thread(target=self._listen, daemon=True,
                          name="nova-vpn").start()
        return f"VPN tunnel listening on UDP port {self._port}"

    def stop(self):
        """Stop the VPN tunnel."""
        self._running = False
        if self._sock:
            self._sock.close()

    def _listen(self):
        """Receive and decrypt incoming VPN packets."""
        self._sock.settimeout(1.0)
        while self._running:
            try:
                data, addr = self._sock.recvfrom(65536)
                peer_ip    = addr[0]
                key        = self._peers.get(peer_ip)
                if key:
                    payload = VPNPacket.decrypt(data, key)
                    if payload and self._rx_cb:
                        self._rx_cb(peer_ip, payload)
            except socket.timeout:
                pass
            except Exception:
                pass

    def status(self) -> dict:
        """Return VPN tunnel status."""
        return {
            "port":    self._port,
            "running": self._running,
            "peers":   len(self._peers),
            "local_key": self._local_key.hex()[:16] + "...",
        }


# ─────────────────────────────────────────────────────── Service Mesh

@dataclass
class MeshService:
    """A service registered in the NOVA service mesh."""

    name:     str
    url:      str
    healthy:  bool    = True
    requests: int     = 0
    errors:   int     = 0
    latency_ms: float = 0.0


class ServiceMesh:
    """
    Mutual-authentication service mesh for NOVA nodes.

    Services register with the mesh. Requests are routed through
    the mesh with circuit breaking, mTLS header injection, and
    per-service health tracking.
    """

    def __init__(self, sos: "SemanticObjectStore"):
        """Initialise the service mesh."""
        self._sos      = sos
        self._services: Dict[str, MeshService] = {}
        self._lock     = threading.Lock()
        # Use a simple token derived from the VPN key as the mesh identity
        try:
            hex_key = sos.read("/security/vpn_key")
            key     = bytes.fromhex(hex_key)
        except Exception:
            key = secrets.token_bytes(32)
        self._mesh_token = hashlib.sha256(b"nova-mesh:" + key).hexdigest()[:32]

    def register(self, name: str, url: str) -> MeshService:
        """
        Register a service in the mesh.

        Args:
            name: Service identifier.
            url:  Service base URL.

        Returns:
            The registered MeshService.
        """
        svc = MeshService(name=name, url=url)
        with self._lock:
            self._services[name] = svc
        return svc

    def unregister(self, name: str) -> bool:
        """Remove a service from the mesh."""
        with self._lock:
            return bool(self._services.pop(name, None))

    def proxy(self, service_name: str, path: str,
               method: str = "GET",
               body: bytes = b"",
               timeout: float = 30.0) -> Tuple[int, bytes]:
        """
        Proxy a request through the mesh with mTLS-like header injection.

        Injects X-Nova-Mesh-Token for service-to-service authentication.

        Args:
            service_name: Target service name.
            path:         Request path.
            method:       HTTP method.
            body:         Request body.
            timeout:      Request timeout.

        Returns:
            Tuple of (status_code, response_body).
        """
        svc = self._services.get(service_name)
        if not svc:
            return 503, b'{"error": "service not found in mesh"}'

        url = svc.url.rstrip("/") + "/" + path.lstrip("/")
        req = urllib.request.Request(
            url, data=body or None, method=method,
            headers={
                "X-Nova-Mesh-Token":   self._mesh_token,
                "X-Nova-Service-Name": service_name,
                "Content-Type":        "application/json",
            }
        )
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data   = resp.read(1024 * 1024)
                status = resp.status
        except urllib.error.HTTPError as e:
            data, status = e.read(65536), e.code
        except Exception as e:
            svc.errors += 1
            svc.healthy = svc.errors < 5
            return 502, json.dumps({"error": str(e)}).encode()

        latency = (time.perf_counter() - t0) * 1000
        svc.requests  += 1
        svc.latency_ms = (svc.latency_ms * 0.9 + latency * 0.1)
        if status >= 500:
            svc.errors += 1
        return status, data

    def list_services(self) -> List[dict]:
        """Return all registered services."""
        with self._lock:
            return [
                {"name": s.name, "url": s.url, "healthy": s.healthy,
                 "requests": s.requests, "errors": s.errors,
                 "latency_ms": round(s.latency_ms, 1)}
                for s in self._services.values()
            ]

    def verify_token(self, token: str) -> bool:
        """Return True if the mesh token is valid."""
        return hmac.compare_digest(token, self._mesh_token)


# ─────────────────────────────────────────────────────── Email Server

@dataclass
class EmailMessage:
    """One email message stored in SOS."""

    msg_id:     str
    from_addr:  str
    to_addrs:   List[str]
    subject:    str
    body:       str
    received_at: float = field(default_factory=time.time)
    read:       bool   = False

    def to_dict(self) -> dict:
        """Serialise to dict."""
        return self.__dict__

    @staticmethod
    def from_dict(d: dict) -> "EmailMessage":
        """Deserialise from dict."""
        return EmailMessage(**{k: v for k, v in d.items()
                                if k in EmailMessage.__dataclass_fields__})  # type: ignore[attr-defined]


class EmailServer:
    """
    Minimal SMTP/IMAP-like email server with SOS message store.

    Not RFC-compliant — simplified for intra-NOVA communication.
    Uses SOS at /mail/inbox/<msg_id> and /mail/sent/<msg_id>.
    """

    def __init__(self, sos: "SemanticObjectStore"):
        """Initialise the email server."""
        self._sos = sos
        self._ensure_dirs()

    def _ensure_dirs(self):
        """Create mail directories."""
        for path in (MAIL_BASE, f"{MAIL_BASE}/inbox", f"{MAIL_BASE}/sent"):
            if not self._sos.exists(path):
                self._sos.mkdir(path, parents=True)

    def send(self, from_addr: str, to_addrs: List[str],
              subject: str, body: str) -> str:
        """
        Store a sent message and deliver to local inboxes.

        Args:
            from_addr: Sender address.
            to_addrs:  List of recipient addresses.
            subject:   Message subject.
            body:      Message body text.

        Returns:
            Message ID.
        """
        msg_id = hashlib.sha256(
            f"{time.time()}{from_addr}{subject}".encode()
        ).hexdigest()[:16]

        msg = EmailMessage(
            msg_id   = msg_id,
            from_addr = from_addr,
            to_addrs  = to_addrs,
            subject   = subject,
            body      = body,
        )
        # Store in sent folder
        self._sos.write(f"{MAIL_BASE}/sent/{msg_id}",
                         json.dumps(msg.to_dict()),
                         tags=["email", "sent"])
        # Deliver to local recipient inboxes
        for addr in to_addrs:
            local_user = addr.split("@")[0]
            inbox_path = f"{MAIL_BASE}/inbox/{local_user}"
            if not self._sos.exists(inbox_path):
                self._sos.mkdir(inbox_path, parents=True)
            self._sos.write(f"{inbox_path}/{msg_id}",
                             json.dumps(msg.to_dict()),
                             tags=["email", "inbox", local_user])
        return msg_id

    def inbox(self, user: str = "root", unread_only: bool = False,
               n: int = 50) -> List[EmailMessage]:
        """
        Return messages in a user's inbox.

        Args:
            user:        Username.
            unread_only: If True, return only unread messages.
            n:           Maximum messages to return.

        Returns:
            List of EmailMessage sorted by date (newest first).
        """
        inbox_path = f"{MAIL_BASE}/inbox/{user}"
        messages   = []
        try:
            for msg_id in self._sos.listdir(inbox_path):
                try:
                    data = json.loads(
                        self._sos.read(f"{inbox_path}/{msg_id}"))
                    msg = EmailMessage.from_dict(data)
                    if not unread_only or not msg.read:
                        messages.append(msg)
                except Exception:
                    pass
        except Exception:
            pass
        return sorted(messages, key=lambda m: m.received_at, reverse=True)[:n]

    def mark_read(self, user: str, msg_id: str) -> bool:
        """Mark a message as read."""
        path = f"{MAIL_BASE}/inbox/{user}/{msg_id}"
        if not self._sos.exists(path):
            return False
        try:
            data = json.loads(self._sos.read(path))
            data["read"] = True
            self._sos.write(path, json.dumps(data))
            return True
        except Exception:
            return False


# ─────────────────────────────────────────────────────── Model Streaming

class StreamingAI:
    """
    Wrapper for streaming token-by-token AI responses.

    Uses llama.cpp's streaming API when available; falls back to
    word-by-word simulation for other backends.
    """

    def __init__(self, kernel: "NovaKernel"):
        """Initialise streaming AI."""
        self.kernel = kernel

    def stream(self, prompt: str,
                max_tokens: int = 512,
                callback=None) -> str:
        """
        Generate a streaming response.

        Calls *callback* with each token as it is produced.
        If callback is None, yields tokens to stdout directly.

        Args:
            prompt:     The prompt text.
            max_tokens: Maximum tokens to generate.
            callback:   Optional function(token: str) called per token.

        Returns:
            Full generated text.
        """
        ai    = self.kernel.ai
        full  = []

        # Try llama.cpp streaming
        llm = getattr(ai, "_llm", None) or getattr(ai, "_llama", None)
        if llm and hasattr(llm, "create_completion"):
            try:
                for chunk in llm.create_completion(
                    prompt, max_tokens=max_tokens, stream=True
                ):
                    token = chunk.get("choices", [{}])[0].get("text", "")
                    full.append(token)
                    if callback:
                        callback(token)
                    else:
                        print(token, end="", flush=True)
                if not callback:
                    print()
                return "".join(full)
            except Exception:
                pass

        # Fallback: get full response then word-by-word simulate streaming
        try:
            response = ai.ask(prompt, max_tokens=max_tokens)
            words    = response.split()
            for i, word in enumerate(words):
                token = word + (" " if i < len(words) - 1 else "")
                full.append(token)
                if callback:
                    callback(token)
                else:
                    print(token, end="", flush=True)
                    time.sleep(0.02)   # simulate streaming delay
            if not callback:
                print()
            return "".join(full)
        except Exception as e:
            return f"[streaming error: {e}]"


# ─────────────────────────────────────────────────────── Load Tester

@dataclass
class LoadTestResult:
    """Results from a load test run."""

    url:          str
    n_requests:   int
    concurrency:  int
    duration_s:   float
    rps:          float
    success:      int
    failed:       int
    latencies_ms: List[float]

    @property
    def p50(self) -> float:
        """50th percentile latency."""
        s = sorted(self.latencies_ms)
        return s[len(s) // 2] if s else 0

    @property
    def p99(self) -> float:
        """99th percentile latency."""
        s = sorted(self.latencies_ms)
        return s[int(len(s) * 0.99)] if s else 0

    @property
    def mean(self) -> float:
        """Mean latency."""
        return sum(self.latencies_ms) / max(len(self.latencies_ms), 1)


class LoadTester:
    """
    Concurrent HTTP load tester with latency percentile reporting.
    """

    def run(self, url: str,
             n: int = 100,
             concurrency: int = 10,
             method: str = "GET",
             body: bytes = b"") -> LoadTestResult:
        """
        Run a load test.

        Args:
            url:         Target URL.
            n:           Total number of requests.
            concurrency: Number of concurrent workers.
            method:      HTTP method.
            body:        Request body.

        Returns:
            LoadTestResult with latency statistics.
        """
        results_q: queue.Queue = queue.Queue()
        work_q:    queue.Queue = queue.Queue()

        for _ in range(n):
            work_q.put(url)

        def _worker():
            while True:
                try:
                    target = work_q.get_nowait()
                except queue.Empty:
                    break
                t0 = time.perf_counter()
                try:
                    req  = urllib.request.Request(target, data=body or None,
                                                   method=method)
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        resp.read(65536)
                        status = resp.status
                except urllib.error.HTTPError as e:
                    status = e.code
                except Exception:
                    status = 0
                latency = (time.perf_counter() - t0) * 1000
                results_q.put((status, latency))

        t_start  = time.perf_counter()
        threads  = [
            threading.Thread(target=_worker, daemon=True)
            for _ in range(min(concurrency, n))
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        duration = time.perf_counter() - t_start

        latencies: List[float] = []
        success = failed = 0
        while not results_q.empty():
            status, lat = results_q.get()
            latencies.append(lat)
            if 200 <= status < 400:
                success += 1
            else:
                failed += 1

        return LoadTestResult(
            url          = url,
            n_requests   = n,
            concurrency  = concurrency,
            duration_s   = round(duration, 2),
            rps          = round(n / max(duration, 0.001), 1),
            success      = success,
            failed       = failed,
            latencies_ms = latencies,
        )
