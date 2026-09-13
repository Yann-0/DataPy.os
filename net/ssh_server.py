"""
PyOS NOVA — SSH Server
========================
Accept SSH connections via paramiko.
Authenticate via ZK credentials or password.
Each connection gets a full NOVA shell session.

Falls back to plain TCP if paramiko is not installed.

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
        return self._start_tcp_fallback()

    def _start_paramiko(self) -> str:
        """Start paramiko-based SSH server."""
        import paramiko

        host_key = paramiko.RSAKey.generate(2048)
        kernel   = self.kernel

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

        def _handle(sock):
            try:
                import io
                from shell.nova_shell import NovaShell
                t = paramiko.Transport(sock)
                t.add_server_key(host_key)
                t.start_server(server=_Interface())
                ch = t.accept(20)
                if ch is None: return
                shell = NovaShell(kernel)

                class W:
                    encoding = "utf-8"
                    def write(self, s):
                        try: ch.send(s.encode("utf-8","replace"))
                        except: pass
                    def flush(self): pass
                    def fileno(self): raise io.UnsupportedOperation
                    def isatty(self): return True

                import builtins
                old = builtins.input
                def _inp(p=""):
                    ch.send(p.encode())
                    buf = b""
                    while True:
                        c = ch.recv(1)
                        if not c or c == b"\x04": raise EOFError
                        if c in (b"\r", b"\n"):
                            ch.send(b"\r\n")
                            return buf.decode("utf-8","replace")
                        elif c == b"\x7f":
                            if buf: buf = buf[:-1]; ch.send(b"\x08 \x08")
                        else:
                            buf += c; ch.send(c)

                builtins.input = _inp
                old_o, old_e = sys.stdout, sys.stderr
                sys.stdout = sys.stderr = W()
                try: shell.run()
                finally:
                    builtins.input = old
                    sys.stdout, sys.stderr = old_o, old_e
                    ch.close()
            except Exception: pass

        def _loop():
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", self.port))
            s.listen(5); s.settimeout(1.0)
            while self._running:
                try:
                    c, _ = s.accept()
                    threading.Thread(target=_handle, args=(c,), daemon=True).start()
                except: pass
            s.close()

        self._running = True
        threading.Thread(target=_loop, daemon=True, name="nova-sshd").start()
        return f"SSH listening on port {self.port} (paramiko)"

    def _start_tcp_fallback(self) -> str:
        """Start plain TCP shell fallback."""
        kernel = self.kernel

        def _handle(conn):
            try:
                import io
                from shell.nova_shell import NovaShell
                shell = NovaShell(kernel)
                f = conn.makefile("rw", encoding="utf-8", errors="replace", newline="")
                import builtins
                builtins.input = lambda p="": (f.write(p) or f.readline().rstrip("\n"))
                sys.stdout = sys.stderr = f
                try: shell.run()
                finally: f.close(); conn.close()
            except Exception: pass

        def _loop():
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", self.port)); s.listen(5); s.settimeout(1.0)
            while self._running:
                try:
                    c, _ = s.accept()
                    threading.Thread(target=_handle, args=(c,), daemon=True).start()
                except: pass
            s.close()

        self._running = True
        threading.Thread(target=_loop, daemon=True, name="nova-sshd").start()
        return f"TCP shell on port {self.port} (install paramiko for SSH)"

    def stop(self):
        """Stop the SSH server."""
        self._running = False

    @property
    def running(self) -> bool:
        """Return True if the server is running."""
        return self._running
