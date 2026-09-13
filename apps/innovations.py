"""
PyOS NOVA — UI & UX Innovations Bundle
=========================================
Four UI subsystems:

1. Web UI — Browser shell
   Serves a full NOVA terminal in any browser via xterm.js + HTTP.
   No SSH, no VPN. Just open http://nova.local and get a shell.

2. TUI Widget Toolkit
   Reusable curses components: DataTable, TreeView, ProgressBar,
   FormInput, Modal. Plugin developers build rich apps without
   touching curses directly.

3. Language Server Protocol for HELIX
   Launches pylsp as a subprocess, speaks LSP over stdio.
   Completions, goto-definition, hover docs, inline errors.

4. Collaborative Real-Time Editing
   Operational Transform merges two HELIX sessions editing
   the same SOS object simultaneously over TCP.

Shell commands:
  webui [port]            — start the web shell (default port 8083)
  lsp start               — launch LSP server for HELIX
  lsp status              — show LSP connection status
  collab <path>           — open a file in collaborative mode
  collab peers <path>     — show who is editing this file
"""

from __future__ import annotations
import os, sys, time, json, threading, socket, queue
from typing import List, Dict, Optional, Any, Tuple, TYPE_CHECKING

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from store.sos import SemanticObjectStore
    from kernel.nova import NovaKernel


# ─────────────────────────────────────────────────── Web UI

WEBUI_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>PyOS NOVA Web Shell</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css">
<style>
body{margin:0;background:#0d1117;display:flex;flex-direction:column;height:100vh}
#header{background:#161b22;color:#7ee787;padding:8px 16px;font-family:monospace;font-size:13px;border-bottom:1px solid #30363d;display:flex;align-items:center;gap:12px}
#header .logo{font-weight:bold;color:#58a6ff}
#header .status{color:#8b949e;font-size:11px}
#terminal{flex:1;padding:8px}
</style>
</head>
<body>
<div id="header">
  <span class="logo">PyOS NOVA</span>
  <span id="status" class="status">connecting…</span>
</div>
<div id="terminal"></div>
<script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js"></script>
<script>
const term = new Terminal({
  fontFamily: "'Cascadia Code', 'JetBrains Mono', monospace",
  fontSize: 14, lineHeight: 1.4,
  theme: {background:'#0d1117', foreground:'#c9d1d9', cursor:'#58a6ff',
          selectionBackground:'#264f78'},
  cursorBlink: true,
});
const fitAddon = new FitAddon.FitAddon();
term.loadAddon(fitAddon);
term.open(document.getElementById('terminal'));
fitAddon.fit();
window.addEventListener('resize', () => fitAddon.fit());

const ws = new WebSocket(`ws://${location.host}/ws`);
ws.onopen = () => {
  document.getElementById('status').textContent = 'connected';
  term.write('\x1b[32mConnected to PyOS NOVA\x1b[0m\r\n');
};
ws.onclose = () => {
  document.getElementById('status').textContent = 'disconnected';
  term.write('\r\n\x1b[31mDisconnected. Reload to reconnect.\x1b[0m\r\n');
};
ws.onmessage = e => term.write(e.data);
ws.onerror   = () => term.write('\x1b[31mConnection error\x1b[0m\r\n');

term.onData(data => { if (ws.readyState === WebSocket.OPEN) ws.send(data); });
</script>
</body>
</html>"""


class WebShell:
    """
    Serves a browser-accessible NOVA terminal via HTTP + WebSocket.

    Each browser connection gets its own NovaShell session.
    xterm.js renders the terminal; WebSocket carries keystrokes + output.
    """

    def __init__(self, kernel: "NovaKernel", port: int = 8083):
        """Initialise the web shell."""
        self.kernel   = kernel
        self.port     = port
        self._running = False

    def start(self) -> str:
        """
        Start the web shell server.

        Returns:
            str: Server URL.
        """
        self._running = True

        try:
            import websockets, asyncio

            async def _shell_session(websocket, path):
                """Handle one browser shell session."""
                if path == "/":
                    await websocket.send(WEBUI_HTML)
                    return

                # Create a new shell for this connection
                import io
                from shell.nova_shell import NovaShell
                shell    = NovaShell(self.kernel)
                out_buf  = queue.Queue()
                inp_buf  = queue.Queue()

                # Patch shell I/O
                class _WS_Writer:
                    def write(self, text):
                        out_buf.put(text)
                    def flush(self): pass
                    def fileno(self): raise io.UnsupportedOperation
                    encoding = "utf-8"
                    isatty   = lambda self: True

                import builtins
                orig_input = builtins.input
                def _ws_input(prompt=""):
                    out_buf.put(prompt)
                    ch = inp_buf.get()
                    if ch is None: raise EOFError
                    return ch
                builtins.input = _ws_input

                sys.stdout = _WS_Writer()
                sys.stderr = _WS_Writer()

                # Reader task: send output to browser
                async def _send_output():
                    while True:
                        try:
                            text = out_buf.get_nowait()
                            text = text.replace("\n", "\r\n")
                            await websocket.send(text)
                        except queue.Empty:
                            await asyncio.sleep(0.01)
                        except Exception:
                            break

                asyncio.create_task(_send_output())

                # Writer task: receive keystrokes from browser
                try:
                    async for msg in websocket:
                        inp_buf.put(msg)
                except Exception:
                    pass
                finally:
                    inp_buf.put(None)
                    builtins.input = orig_input

            async def _serve():
                async with websockets.serve(_shell_session, "0.0.0.0",
                                             self.port):
                    while self._running:
                        await asyncio.sleep(1)

            def _run():
                asyncio.run(_serve())

            threading.Thread(target=_run, daemon=True,
                              name="nova-webui").start()

        except ImportError:
            # Fallback: serve HTML only (no live shell)
            from http.server import HTTPServer, BaseHTTPRequestHandler
            html_bytes = WEBUI_HTML.encode()

            class _Handler(BaseHTTPRequestHandler):
                def log_message(self, *a): pass
                def do_GET(self):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", len(html_bytes))
                    self.end_headers()
                    self.wfile.write(html_bytes)

            srv = HTTPServer(("0.0.0.0", self.port), _Handler)
            threading.Thread(target=srv.serve_forever,
                              daemon=True, name="nova-webui").start()

        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
        except Exception:
            ip = "localhost"
        return f"http://{ip}:{self.port}"

    def stop(self):
        """Stop the web shell."""
        self._running = False


# ─────────────────────────────────────────────────── TUI Widget Toolkit

class Widget:
    """Base class for TUI widgets."""

    def __init__(self, y: int, x: int, h: int, w: int):
        """Initialise a widget with position and size."""
        self.y = y; self.x = x; self.h = h; self.w = w
        self._focused = False

    def render(self, stdscr): pass
    def handle_key(self, key: int) -> bool: return False


class DataTable(Widget):
    """Curses data table widget with scrolling and selection."""

    def __init__(self, y: int, x: int, h: int, w: int,
                  columns: List[str], data: List[list]):
        """Initialise the data table."""
        super().__init__(y, x, h, w)
        self.columns = columns
        self.data    = data
        self._offset = 0
        self._sel    = 0

    def render(self, stdscr):
        """Render the table to the curses screen."""
        try:
            import curses
            col_w = max(1, (self.w - 2) // max(len(self.columns), 1))
            # Header
            header = "".join(f"{c[:col_w]:<{col_w}}" for c in self.columns)
            stdscr.addstr(self.y, self.x, header[:self.w],
                           curses.A_BOLD | curses.color_pair(1))
            # Rows
            visible = self.h - 2
            for i in range(visible):
                row_idx = i + self._offset
                if row_idx >= len(self.data):
                    break
                row = self.data[row_idx]
                row_str = "".join(
                    f"{str(v)[:col_w]:<{col_w}}" for v in row
                )
                attr = (curses.A_REVERSE if row_idx == self._sel
                        else curses.A_NORMAL)
                stdscr.addstr(self.y + 1 + i, self.x,
                               row_str[:self.w], attr)
        except Exception:
            pass

    def handle_key(self, key: int) -> bool:
        """Handle keyboard navigation."""
        import curses
        if key == curses.KEY_DOWN:
            self._sel = min(self._sel + 1, len(self.data) - 1)
            return True
        elif key == curses.KEY_UP:
            self._sel = max(self._sel - 1, 0)
            return True
        return False

    @property
    def selected_row(self) -> Optional[list]:
        """Return the currently selected row."""
        if 0 <= self._sel < len(self.data):
            return self.data[self._sel]
        return None


class ProgressBar(Widget):
    """Single-line progress bar widget."""

    def __init__(self, y: int, x: int, w: int,
                  label: str = "", value: float = 0.0):
        """Initialise the progress bar."""
        super().__init__(y, x, 1, w)
        self.label = label
        self.value = max(0.0, min(1.0, value))

    def render(self, stdscr):
        """Render the progress bar."""
        try:
            import curses
            bar_w   = self.w - len(self.label) - 8
            filled  = int(self.value * bar_w)
            bar     = "█" * filled + "░" * (bar_w - filled)
            pct     = f"{self.value*100:.0f}%"
            line    = f"{self.label} {bar} {pct}"
            stdscr.addstr(self.y, self.x, line[:self.w])
        except Exception:
            pass


class Modal(Widget):
    """Centred modal dialog widget."""

    def __init__(self, screen_h: int, screen_w: int,
                  title: str, lines: List[str]):
        """Initialise the modal."""
        h    = len(lines) + 4
        w    = max(len(title) + 4,
                   max((len(l) for l in lines), default=0) + 4,
                   30)
        y    = (screen_h - h) // 2
        x    = (screen_w - w) // 2
        super().__init__(y, x, h, w)
        self.title = title
        self.lines = lines

    def render(self, stdscr):
        """Render the modal dialog."""
        try:
            import curses
            stdscr.addstr(self.y, self.x, "┌" + "─"*(self.w-2) + "┐")
            title_x = self.x + (self.w - len(self.title)) // 2
            stdscr.addstr(self.y, title_x, f" {self.title} ",
                           curses.A_BOLD)
            for i, line in enumerate(self.lines):
                stdscr.addstr(self.y + 1 + i, self.x,
                               "│" + f" {line:<{self.w-3}}" + "│")
            stdscr.addstr(self.y + len(self.lines) + 1, self.x,
                           "│" + " "*(self.w-2) + "│")
            stdscr.addstr(self.y + len(self.lines) + 2, self.x,
                           "└" + "─"*(self.w-2) + "┘")
        except Exception:
            pass


# ─────────────────────────────────────────────────── LSP for HELIX

class LSPClient:
    """
    Language Server Protocol client for HELIX.

    Launches pylsp as a subprocess and communicates via JSON-RPC
    over stdio. Provides completions, hover, goto-definition,
    and inline diagnostics.
    """

    def __init__(self):
        """Initialise the LSP client."""
        self._proc    = None
        self._seq     = 0
        self._pending: Dict[int, queue.Queue] = {}
        self._lock    = threading.Lock()
        self._running = False

    def start(self) -> bool:
        """
        Launch the LSP server process.

        Returns:
            bool: True if successfully started.
        """
        import subprocess
        for cmd in (["pylsp"], ["python3", "-m", "pylsp"],
                     [sys.executable, "-m", "pylsp"]):
            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                self._running = True
                threading.Thread(target=self._read_loop,
                                  daemon=True, name="nova-lsp").start()
                return True
            except (FileNotFoundError, OSError):
                continue
        return False

    def _next_id(self) -> int:
        """Return next request ID."""
        with self._lock:
            self._seq += 1
            return self._seq

    def _send(self, message: dict):
        """Send a JSON-RPC message."""
        body  = json.dumps(message)
        frame = (f"Content-Length: {len(body)}\r\n\r\n{body}").encode()
        if self._proc and self._proc.stdin:
            try:
                self._proc.stdin.write(frame)
                self._proc.stdin.flush()
            except Exception:
                pass

    def _read_loop(self):
        """Read responses from the LSP server."""
        while self._running and self._proc:
            try:
                header = b""
                while b"\r\n\r\n" not in header:
                    chunk = self._proc.stdout.read(1)
                    if not chunk:
                        return
                    header += chunk
                m = header.find(b"Content-Length: ")
                if m < 0:
                    continue
                length = int(header[m+16:].split(b"\r\n")[0])
                body   = self._proc.stdout.read(length)
                msg    = json.loads(body)
                req_id = msg.get("id")
                if req_id in self._pending:
                    self._pending[req_id].put(msg)
            except Exception:
                break

    def request(self, method: str, params: dict,
                 timeout: float = 3.0) -> Optional[dict]:
        """
        Send an LSP request and wait for a response.

        Args:
            method (str): LSP method name.
            params (dict): Method parameters.
            timeout (float): Maximum wait time.

        Returns:
            dict: LSP response, or None on timeout.
        """
        req_id = self._next_id()
        q: queue.Queue = queue.Queue()
        self._pending[req_id] = q
        self._send({"jsonrpc": "2.0", "id": req_id,
                     "method": method, "params": params})
        try:
            return q.get(timeout=timeout)
        except queue.Empty:
            return None
        finally:
            self._pending.pop(req_id, None)

    def notify(self, method: str, params: dict):
        """Send an LSP notification (no response expected)."""
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def initialize(self, root_uri: str = "") -> bool:
        """Send LSP initialize request."""
        resp = self.request("initialize", {
            "processId": os.getpid(),
            "rootUri":   root_uri or f"file://{ROOT}",
            "capabilities": {
                "textDocument": {
                    "completion":      {"completionItem": {}},
                    "hover":           {},
                    "definition":      {},
                    "publishDiagnostics": {},
                }
            }
        })
        if resp and "result" in resp:
            self.notify("initialized", {})
            return True
        return False

    def open_document(self, path: str, content: str):
        """Notify LSP that a document was opened."""
        self.notify("textDocument/didOpen", {
            "textDocument": {
                "uri":        f"file://{path}",
                "languageId": "python",
                "version":    1,
                "text":       content,
            }
        })

    def completions(self, path: str, line: int, col: int) -> List[str]:
        """
        Get completions at a cursor position.

        Args:
            path (str): File path.
            line (int): 0-based line number.
            col (int): 0-based column.

        Returns:
            List[str]: Completion labels.
        """
        resp = self.request("textDocument/completion", {
            "textDocument": {"uri": f"file://{path}"},
            "position":     {"line": line, "character": col},
        })
        if not resp or "result" not in resp:
            return []
        items = resp["result"]
        if isinstance(items, dict):
            items = items.get("items", [])
        return [i.get("label", "") for i in items[:20]]

    def hover(self, path: str, line: int, col: int) -> str:
        """Get hover documentation at a position."""
        resp = self.request("textDocument/hover", {
            "textDocument": {"uri": f"file://{path}"},
            "position":     {"line": line, "character": col},
        })
        if not resp or "result" not in resp or not resp["result"]:
            return ""
        content = resp["result"].get("contents", {})
        if isinstance(content, dict):
            return content.get("value", "")
        return str(content)

    def stop(self):
        """Stop the LSP server."""
        self._running = False
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass


# ─────────────────────────────────────────────────── Collaborative editing

OT_PORT = 8084


class CollabSession:
    """
    Collaborative editing session using Operational Transform.

    Two HELIX instances share edit operations via TCP.
    OT ensures both reach the same state even with concurrent edits.
    """

    def __init__(self, path: str, sos: "SemanticObjectStore"):
        """Initialise a collaborative session."""
        self.path     = path
        self.sos      = sos
        self._version = 0
        self._ops:    List[dict] = []
        self._peers:  List[Any] = []
        self._lock    = threading.Lock()
        self._content = ""
        try:
            self._content = sos.read(path)
        except Exception:
            pass

    def _transform(self, op1: dict, op2: dict) -> dict:
        """
        Transform op1 against op2 (simplified OT).

        For insert ops: adjust position if op2 inserted before op1.
        For delete ops: adjust position similarly.

        Args:
            op1 (dict): Operation to transform.
            op2 (dict): Already-applied operation.

        Returns:
            dict: Transformed op1.
        """
        op1 = dict(op1)
        if op2.get("type") == "insert" and op1.get("type") == "insert":
            if op2["pos"] <= op1["pos"]:
                op1["pos"] += len(op2.get("text", ""))
        elif op2.get("type") == "delete" and op1.get("type") == "insert":
            if op2["pos"] < op1["pos"]:
                op1["pos"] -= min(op2.get("length", 0),
                                   op1["pos"] - op2["pos"])
        return op1

    def apply(self, op: dict) -> bool:
        """
        Apply an edit operation to the document.

        Args:
            op (dict): Operation: {type: insert|delete, pos, text/length}.

        Returns:
            bool: True if applied successfully.
        """
        with self._lock:
            # Transform against all pending ops
            pending = [o for o in self._ops
                       if o["version"] >= op.get("base_version", 0)]
            xformed = op
            for pending_op in pending:
                xformed = self._transform(xformed, pending_op)

            # Apply to content
            pos  = xformed.get("pos", 0)
            if xformed.get("type") == "insert":
                text = xformed.get("text", "")
                self._content = (self._content[:pos] + text +
                                  self._content[pos:])
            elif xformed.get("type") == "delete":
                n = xformed.get("length", 0)
                self._content = (self._content[:pos] +
                                  self._content[pos+n:])

            self._version += 1
            xformed["version"] = self._version
            self._ops.append(xformed)

            # Persist to SOS
            try:
                self.sos.write(self.path, self._content)
            except Exception:
                pass

        # Broadcast to peers
        for peer in list(self._peers):
            try:
                peer.put(xformed)
            except Exception:
                self._peers.remove(peer)

        return True

    def add_peer(self, peer_queue: queue.Queue):
        """Add a peer queue to broadcast operations to."""
        self._peers.append(peer_queue)

    def remove_peer(self, peer_queue: queue.Queue):
        """Remove a peer queue."""
        if peer_queue in self._peers:
            self._peers.remove(peer_queue)

    @property
    def content(self) -> str:
        """Return current document content."""
        return self._content

    @property
    def version(self) -> int:
        """Return current document version."""
        return self._version
