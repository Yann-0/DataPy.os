"""
PyOS NOVA — SSH Server
========================
Accept SSH connections via paramiko.
Authenticate via ZK credentials or password.
Each connection gets a full NOVA shell session.

Missing paramiko is an error. There is no plaintext TCP fallback.

Shell commands:
  sshd [port]    — start SSH server (default 2222)
  sshd stop      — stop SSH server
  sshd status    — show server status
"""
from __future__ import annotations
import os, sys, time, threading, socket
from typing import Optional, TYPE_CHECKING

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from kernel.nova import NovaKernel


class NovaSSHServer:
    """SSH server exposing a NOVA shell over encrypted connections."""

    def __init__(self, kernel: "NovaKernel", port: int = 2222):
        """Initialise the SSH server."""
        self.kernel   = kernel
        self.port     = port
        self._running = False

    def _has_paramiko(self) -> bool:
        """Return True if paramiko is installed."""
        try:
            import paramiko; return True
        except ImportError:
            return False

    def start(self) -> str:
        """
        Start the SSH server.

        Returns:
            str: Status message.
        """
        if self._has_paramiko():
            return self._start_paramiko()
        raise RuntimeError(
            "SSH dependencies missing (paramiko). Refusing to open an "
            "unauthenticated plaintext TCP shell."
        )

    def _host_key_path(self):
        data = os.environ.get("NOVA_DATA", os.path.expanduser("~/.nova"))
        certs = os.path.join(data, "certs")
        os.makedirs(certs, exist_ok=True)
        return os.path.join(certs, "ssh_host_rsa")

    def _load_host_key(self):
        import paramiko
        path = self._host_key_path()
        if os.path.isfile(path):
            return paramiko.RSAKey.from_private_key_file(path)
        key = paramiko.RSAKey.generate(2048)
        key.write_private_key_file(path)
        return key

    def _start_paramiko(self) -> str:
        """Start paramiko-based SSH server."""
        import paramiko

        host_key = self._load_host_key()
        kernel = self.kernel
        bind_host = os.environ.get("NOVA_SSH_HOST", "127.0.0.1")

        class _Interface(paramiko.ServerInterface):
            def check_channel_request(self, kind, chanid):
                return (paramiko.OPEN_SUCCEEDED if kind == "session"
                        else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED)

            def check_auth_password(self, username, password):
                zk = getattr(kernel, "zk", None)
                if zk and zk.verify_proof(username, password):
                    return paramiko.AUTH_SUCCESSFUL
                return paramiko.AUTH_FAILED

            def get_allowed_auths(self, username):
                return "password"

            def check_channel_shell_request(self, ch): return True
            def check_channel_pty_request(self, ch, *a): return True

        def _read_line(ch) -> str:
            buf = b""
            while True:
                chunk = ch.recv(1)
                if not chunk or chunk == b"\x04":
                    raise EOFError
                if chunk in (b"\r", b"\n"):
                    ch.send(b"\r\n")
                    return buf.decode("utf-8", "replace")
                buf += chunk

        def _handle(sock):
            """Run one SSH session without swapping process-global stdio."""
            t = None
            ch = None
            try:
                from shell.nova_shell import NovaShell
                t = paramiko.Transport(sock)
                t.add_server_key(host_key)
                t.start_server(server=_Interface())
                ch = t.accept(20)
                if ch is None:
                    return
                username = t.get_username() or "remote"
                shell = NovaShell(kernel)
                shell.user = username
                ch.send(b"datapy ssh (per-session execute, no global stdio)\r\n")
                while True:
                    ch.send(b"$ ")
                    line = _read_line(ch).strip()
                    if not line:
                        continue
                    if line in {"exit", "quit"}:
                        break
                    result = shell.execute(line)
                    msg = (
                        f"ok {line}\r\n" if result.ok
                        else f"error {result.error or line}\r\n"
                    )
                    ch.send(msg.encode("utf-8", "replace"))
            except Exception:
                pass
            finally:
                if ch is not None:
                    try:
                        ch.close()
                    except Exception:
                        pass
                if t is not None:
                    try:
                        t.close()
                    except Exception:
                        pass

        def _loop():
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((bind_host, self.port))
            s.listen(5)
            s.settimeout(1.0)
            while self._running:
                try:
                    client, _ = s.accept()
                    threading.Thread(
                        target=_handle, args=(client,), daemon=True
                    ).start()
                except OSError:
                    continue
            s.close()

        self._running = True
        threading.Thread(target=_loop, daemon=True, name="nova-sshd").start()
        return f"SSH listening on port {self.port} (paramiko)"

    def _start_tcp_fallback(self) -> str:
        """Plaintext fallback is never started."""
        raise RuntimeError(
            "unauthenticated plaintext TCP shell is not a supported fallback"
        )

    def stop(self):
        """Stop the SSH server."""
        self._running = False

    @property
    def running(self) -> bool:
        """Return True if the server is running."""
        return self._running
