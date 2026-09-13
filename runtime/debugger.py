"""
PyOS NOVA — Interactive Debugger
===================================
pdb wrapped into NOVA shell with full SOS state inspection.
Set breakpoints in any SOS .py file, inspect kernel/sos/ai live state.

Commands:
  debug <script.py>          — debug a SOS script
  debug --break <path>:<n>   — set a breakpoint at line n
  debug --break <func>       — break on function entry
  breakpoints                — list active breakpoints
  breakpoints clear          — clear all breakpoints

Debugger REPL commands (while paused):
  n / next     — next line
  s / step     — step into
  c / continue — continue
  r / return   — run to function return
  p <expr>     — print expression (sos, kernel, ai available as globals)
  l / list     — list source
  w / where    — stack trace
  b <n>        — set breakpoint at current file line n
  q / quit     — quit debugger
  sos <path>   — inspect SOS object at path
  k <attr>     — inspect kernel attribute
"""

from __future__ import annotations
import os, sys, pdb, bdb, dis, linecache, threading, code, io, time
from typing import Optional, List, Dict, Tuple, TYPE_CHECKING

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from kernel.nova import NovaKernel

BREAKPOINTS_PATH = "/system/breakpoints.json"


class NOVADebugger(pdb.Pdb):
    """
    Extended pdb with NOVA-specific commands and SOS state access.

    Adds:
      sos <path>  — inspect SOS object
      k <attr>    — inspect kernel attribute
      ai <prompt> — ask AI while paused
    """

    def __init__(self, kernel: "NovaKernel" = None, **kw):
        """Initialise the NOVA debugger."""
        super().__init__(**kw)
        self.kernel   = kernel
        self.prompt   = "\033[36m(nova-dbg)\033[0m "
        self._session: List[str] = []

    def do_sos(self, arg: str):
        """sos <path> — inspect a SOS object while paused."""
        if not arg:
            print("  Usage: sos <path>")
            return
        if not self.kernel:
            print("  No kernel available")
            return
        try:
            path    = arg.strip()
            content = self.kernel.sos.read(path)
            stat    = self.kernel.sos.stat(path)
            print(f"  Path:    {path}")
            print(f"  OID:     {stat.get('oid','?')[:16]}...")
            print(f"  Version: {stat.get('version','?')}")
            print(f"  Size:    {stat.get('size','?')} bytes")
            print(f"  Tags:    {stat.get('tags','[]')}")
            print(f"  Content: {content[:300]}")
        except Exception as e:
            print(f"  Error: {e}")

    def do_k(self, arg: str):
        """k <attr> — inspect a kernel attribute while paused."""
        if not arg:
            print("  Usage: k <attribute>")
            return
        if not self.kernel:
            print("  No kernel available")
            return
        try:
            parts = arg.strip().split(".")
            obj   = self.kernel
            for part in parts:
                obj = getattr(obj, part)
            if hasattr(obj, "status"):
                print(f"  {arg}: {obj.status()}")
            elif hasattr(obj, "stats"):
                print(f"  {arg}: {obj.stats()}")
            else:
                print(f"  {arg}: {obj!r}")
        except AttributeError as e:
            attrs = [a for a in dir(self.kernel) if not a.startswith("_")]
            print(f"  Error: {e}")
            print(f"  Available: {', '.join(attrs[:10])}")

    def do_ai(self, arg: str):
        """ai <prompt> — ask AI a question while paused (uses kernel AI)."""
        if not arg:
            print("  Usage: ai <question>")
            return
        if not self.kernel:
            print("  No kernel available")
            return
        try:
            answer = self.kernel.ai.ask(arg.strip(), max_tokens=200)
            print(f"  {answer}")
        except Exception as e:
            print(f"  Error: {e}")

    def do_wt(self, arg: str):
        """wt — show all active threads."""
        for t in threading.enumerate():
            print(f"  [{t.ident}] {t.name}  alive={t.is_alive()}")

    def default(self, line: str):
        """Override: make kernel, sos, ai available in eval context."""
        # Inject NOVA globals
        if self.kernel:
            self.curframe.f_globals.setdefault("kernel", self.kernel)
            self.curframe.f_globals.setdefault("sos",    self.kernel.sos)
            self.curframe.f_globals.setdefault("ai",     self.kernel.ai)
        return super().default(line)


class DebugManager:
    """
    Manages breakpoints and debug sessions for NOVA.
    """

    def __init__(self, kernel: "NovaKernel"):
        """Initialise the debug manager."""
        self.kernel      = kernel
        self._breakpoints: List[Dict] = []
        self._active_dbg: Optional[NOVADebugger] = None
        self._load_breakpoints()

    def _load_breakpoints(self):
        """Load saved breakpoints from SOS."""
        import json
        try:
            data = json.loads(self.kernel.sos.read(BREAKPOINTS_PATH))
            self._breakpoints = data
        except Exception:
            self._breakpoints = []

    def _save_breakpoints(self):
        """Persist breakpoints to SOS."""
        import json
        self.kernel.sos.write(
            BREAKPOINTS_PATH,
            json.dumps(self._breakpoints),
            tags=["debugger"],
        )

    def add_breakpoint(self, location: str) -> Dict:
        """
        Add a breakpoint at a location.

        Args:
            location (str): 'path:line' or 'function_name'.

        Returns:
            dict: Breakpoint record.
        """
        bp = {"id": len(self._breakpoints) + 1, "location": location,
              "hits": 0, "enabled": True}
        self._breakpoints.append(bp)
        self._save_breakpoints()
        return bp

    def remove_breakpoint(self, bp_id: int) -> bool:
        """Remove a breakpoint by ID."""
        before = len(self._breakpoints)
        self._breakpoints = [b for b in self._breakpoints if b["id"] != bp_id]
        self._save_breakpoints()
        return len(self._breakpoints) < before

    def clear_all(self):
        """Remove all breakpoints."""
        self._breakpoints = []
        self._save_breakpoints()

    def list_breakpoints(self) -> List[Dict]:
        """Return all current breakpoints."""
        return list(self._breakpoints)

    def debug_script(self, script_path: str,
                      args: List[str] = None) -> int:
        """
        Debug a Python script from the SOS.

        Loads the script, sets any active breakpoints, and runs it
        under the NOVA debugger.

        Args:
            script_path (str): SOS path to the Python script.
            args (List[str]): Script arguments.

        Returns:
            int: Exit code.
        """
        # Load script from SOS
        try:
            code_src = self.kernel.sos.read(script_path)
        except Exception as e:
            print(f"  Cannot read: {e}")
            return 1

        # Write to temp file (pdb needs a real file path for source display)
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py",
                                          delete=False, prefix="nova_dbg_") as f:
            f.write(code_src)
            tmp_path = f.name

        # Build execution namespace with NOVA globals
        ns = {
            "__name__": "__main__",
            "__file__": script_path,
            "kernel":   self.kernel,
            "sos":      self.kernel.sos,
            "ai":       self.kernel.ai,
        }
        if args:
            sys.argv = [script_path] + args

        dbg = NOVADebugger(kernel=self.kernel)
        self._active_dbg = dbg

        # Set active breakpoints
        for bp in self._breakpoints:
            if bp["enabled"]:
                loc = bp["location"]
                if ":" in loc:
                    parts = loc.split(":")
                    filename, lineno = parts[0], int(parts[1])
                    dbg.set_break(tmp_path if filename in script_path else filename,
                                   lineno)
                else:
                    dbg.set_break(tmp_path, 1)  # function name not supported in basic pdb

        try:
            dbg.run(
                f"exec(open({tmp_path!r}).read(), {{}}, {{}})",
                globals=ns
            )
            return 0
        except SystemExit as e:
            return e.code if isinstance(e.code, int) else 0
        except Exception as e:
            print(f"  Debugger error: {e}")
            return 1
        finally:
            os.unlink(tmp_path)
            self._active_dbg = None

    def debug_inline(self, code: str) -> int:
        """
        Debug an inline code snippet.

        Args:
            code (str): Python source to debug.

        Returns:
            int: Exit code.
        """
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py",
                                          delete=False) as f:
            f.write(code)
            tmp = f.name
        try:
            ns  = {"kernel": self.kernel, "sos": self.kernel.sos}
            dbg = NOVADebugger(kernel=self.kernel)
            dbg.run(open(tmp).read(), globals=ns)
            return 0
        except Exception as e:
            print(f"  {e}")
            return 1
        finally:
            os.unlink(tmp)
