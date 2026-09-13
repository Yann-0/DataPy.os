"""
PyOS NOVA — Rich REPL  (v0.0100)
==================================
An enhanced Python REPL that integrates with the NOVA kernel.

Features:
  · Persistent history across sessions (stored in SOS)
  · Auto-import: `sos`, `ai`, `kernel` pre-injected into namespace
  · Table rendering: returning a list-of-dicts prints a formatted table
  · ASCII plots: `plot([1,2,3])` renders inline sparklines
  · Magic commands: %time, %profile, %sos, %hist, %clear
  · Syntax highlighting via pygments (optional)
  · Multi-line input with `...` continuation

Shell command:
  repl              — launch the rich REPL
  repl --quiet      — no banner
"""

from __future__ import annotations

import os
import sys
import time
import code
import readline
import rlcompleter
import traceback
import textwrap
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from kernel.nova import NovaKernel

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

HISTORY_SOS_PATH = "/system/repl_history"
HISTORY_SIZE     = 500


# ── Sparkline / ASCII plot ────────────────────────────────────────────────────

_SPARK_CHARS = "▁▂▃▄▅▆▇█"


def sparkline(values: List[float], width: int = 40) -> str:
    """
    Render a list of numbers as a terminal sparkline.

    Args:
        values: Numeric data points.
        width:  Maximum character width.

    Returns:
        Single-line sparkline string.
    """
    if not values:
        return "(empty)"
    lo, hi = min(values), max(values)
    span   = hi - lo or 1
    chars  = _SPARK_CHARS
    result = []
    step   = max(1, len(values) // width)
    sampled = values[::step][:width]
    for v in sampled:
        idx = int((v - lo) / span * (len(chars) - 1))
        result.append(chars[idx])
    return "".join(result)


def bar_chart(data: Dict[str, float], width: int = 40) -> str:
    """
    Render a dict as a horizontal bar chart.

    Args:
        data:  Label → value mapping.
        width: Maximum bar width in characters.

    Returns:
        Multi-line bar chart string.
    """
    if not data:
        return "(empty)"
    max_val = max(data.values()) or 1
    max_lbl = max(len(k) for k in data) if data else 1
    lines   = []
    for label, value in data.items():
        bar_len = int(value / max_val * width)
        bar     = "█" * bar_len
        lines.append(f"  {label:<{max_lbl}}  {bar}  {value:.2g}")
    return "\n".join(lines)


# ── Table renderer ────────────────────────────────────────────────────────────

def render_table(rows: List[Dict[str, Any]],
                  max_col_width: int = 30) -> str:
    """
    Render a list of dicts as a Unicode box-drawing table.

    Args:
        rows:         List of record dicts (all same keys).
        max_col_width: Maximum column width before truncation.

    Returns:
        Multi-line table string.
    """
    if not rows:
        return "  (empty result set)"
    columns = list(rows[0].keys())
    widths  = {col: min(max_col_width,
                         max(len(str(col)),
                             max(len(str(r.get(col, ""))) for r in rows)))
                for col in columns}

    def _row(vals: List[str]) -> str:
        cells = [f" {str(v)[:widths[c]]:<{widths[c]}} " for c, v in zip(columns, vals)]
        return "│" + "│".join(cells) + "│"

    sep_line = "├" + "┼".join("─" * (w + 2) for w in widths.values()) + "┤"
    top_line = "┌" + "┬".join("─" * (w + 2) for w in widths.values()) + "┐"
    bot_line = "└" + "┴".join("─" * (w + 2) for w in widths.values()) + "┘"

    lines = [top_line, _row(columns), sep_line]
    for row in rows:
        lines.append(_row([row.get(c, "") for c in columns]))
    lines.append(bot_line)
    return "\n".join(lines)


# ── Magic command handler ─────────────────────────────────────────────────────

class MagicCommands:
    """Handles %magic commands in the REPL."""

    def __init__(self, kernel: "NovaKernel", history: List[str]):
        """Initialise magic commands."""
        self.kernel  = kernel
        self.history = history

    def handle(self, line: str) -> Optional[str]:
        """
        Process a %magic line.

        Args:
            line: Input line starting with %.

        Returns:
            Output string, or None if not a magic command.
        """
        if not line.startswith("%"):
            return None
        parts = line[1:].split(None, 1)
        cmd   = parts[0].lower()
        arg   = parts[1] if len(parts) > 1 else ""

        if cmd == "time":
            return self._time(arg)
        elif cmd == "profile":
            return self._profile(arg)
        elif cmd == "sos":
            return self._sos(arg)
        elif cmd in ("hist", "history"):
            return self._history()
        elif cmd == "clear":
            os.system("clear" if os.name != "nt" else "cls")
            return ""
        elif cmd == "help":
            return (
                "  Magic commands:\n"
                "  %time   <expr>   — measure execution time\n"
                "  %profile <expr>  — cProfile a statement\n"
                "  %sos    <path>   — inspect SOS object\n"
                "  %hist            — show command history\n"
                "  %clear           — clear terminal"
            )
        return f"  Unknown magic: %{cmd}  (try %help)"

    def _time(self, expr: str) -> str:
        """Time an expression."""
        if not expr:
            return "  Usage: %time <expression>"
        t = time.perf_counter()
        try:
            exec(expr, {"kernel": self.kernel, "sos": self.kernel.sos})
            elapsed = time.perf_counter() - t
            return f"  Wall time: {elapsed * 1000:.2f} ms"
        except Exception as exc:
            return f"  Error: {exc}"

    def _profile(self, expr: str) -> str:
        """Profile an expression with cProfile."""
        if not expr:
            return "  Usage: %profile <expression>"
        import cProfile, pstats, io
        pr  = cProfile.Profile()
        pr.enable()
        try:
            exec(expr, {"kernel": self.kernel, "sos": self.kernel.sos})
        except Exception:
            pass
        pr.disable()
        buf   = io.StringIO()
        stats = pstats.Stats(pr, stream=buf).sort_stats("cumulative")
        stats.print_stats(8)
        return buf.getvalue()

    def _sos(self, path: str) -> str:
        """Show SOS object details."""
        if not path:
            return "  Usage: %sos <path>"
        try:
            content = self.kernel.sos.read(path.strip())
            return f"  {path}:\n  {content[:300]}"
        except Exception as exc:
            return f"  {exc}"

    def _history(self) -> str:
        """Show command history."""
        lines = [f"  {i+1:>4}  {cmd}"
                  for i, cmd in enumerate(self.history[-20:])]
        return "\n".join(lines) if lines else "  (no history)"


# ── REPL ──────────────────────────────────────────────────────────────────────

class RichREPL:
    """
    Enhanced Python REPL integrated with the NOVA kernel.

    Provides persistent history, table rendering, sparklines,
    magic commands, and auto-imported kernel globals.
    """

    BANNER = """
\033[36m ╔══════════════════════════════════════════╗
 ║   PyOS NOVA Rich REPL  v0.0100          ║
 ║   Globals: kernel, sos, ai, i18n        ║
 ║   Magic:   %time %profile %sos %hist    ║
 ║   Type exit() or Ctrl-D to quit         ║
 ╚══════════════════════════════════════════╝\033[0m"""

    def __init__(self, kernel: "NovaKernel"):
        """Initialise the rich REPL."""
        self.kernel  = kernel
        self.history: List[str] = []
        self._magic  = MagicCommands(kernel, self.history)
        self._ns     = self._build_namespace()
        self._load_history()
        self._setup_readline()

    def _build_namespace(self) -> dict:
        """Build the initial REPL namespace with NOVA globals."""
        ns = {
            "__name__":  "__repl__",
            "kernel":    self.kernel,
            "sos":       self.kernel.sos,
            "ai":        self.kernel.ai,
            "i18n":      getattr(self.kernel, "i18n", None),
            "table":     render_table,
            "plot":      sparkline,
            "bar":       bar_chart,
            "t":         lambda text: getattr(self.kernel, "i18n",
                                               type("I", (), {"t": lambda s, x: x})()).t(text),
        }
        return ns

    def _setup_readline(self):
        """Configure readline with tab-completion and history."""
        readline.set_completer(rlcompleter.Completer(self._ns).complete)
        readline.parse_and_bind("tab: complete")
        readline.set_history_length(HISTORY_SIZE)

    def _load_history(self):
        """Load REPL history from SOS."""
        try:
            raw = self.kernel.sos.read(HISTORY_SOS_PATH)
            self.history = [l for l in raw.splitlines() if l.strip()]
            for cmd in self.history[-50:]:
                readline.add_history(cmd)
        except Exception:
            pass

    def _save_history(self):
        """Persist REPL history to SOS."""
        try:
            self.kernel.sos.write(
                HISTORY_SOS_PATH,
                "\n".join(self.history[-HISTORY_SIZE:]),
                tags=["repl-history"],
            )
        except Exception:
            pass

    def _format_result(self, result: Any) -> Optional[str]:
        """Format a result value for display."""
        if result is None:
            return None
        if isinstance(result, list) and result and isinstance(result[0], dict):
            return render_table(result)
        if isinstance(result, dict) and all(
                isinstance(v, (int, float)) for v in result.values()):
            return bar_chart(result)
        if isinstance(result, (list, tuple)) and all(
                isinstance(v, (int, float)) for v in result):
            return f"  {sparkline(list(result))}  [{min(result):.2g} … {max(result):.2g}]"
        return repr(result)

    def run(self, quiet: bool = False):
        """
        Start the interactive REPL loop.

        Args:
            quiet: If True, suppress the banner.
        """
        if not quiet:
            print(self.BANNER)

        buf      = []       # multi-line input buffer
        ps1, ps2 = ">>> ", "... "

        while True:
            try:
                prompt = ps2 if buf else ps1
                line   = input(prompt)
            except EOFError:
                print("\nexit")
                break
            except KeyboardInterrupt:
                buf = []
                print()
                continue

            # Magic commands
            stripped = line.strip()
            if stripped.startswith("%") and not buf:
                out = self._magic.handle(stripped)
                if out:
                    print(out)
                continue

            buf.append(line)
            source = "\n".join(buf)

            # Check if input is complete
            try:
                import codeop
                compiled = codeop.compile_command(source, "<repl>", "single")
            except SyntaxError as exc:
                print(f"  \033[31mSyntaxError: {exc}\033[0m")
                buf = []
                continue

            if compiled is None:
                # Incomplete — wait for more input
                continue

            # Execute complete source
            buf = []
            if not source.strip():
                continue

            self.history.append(source.replace("\n", "\\n"))
            try:
                result = eval(compile(source, "<repl>", "eval"), self._ns)
                formatted = self._format_result(result)
                if formatted:
                    print(formatted)
            except SyntaxError:
                try:
                    exec(compile(source, "<repl>", "exec"), self._ns)
                except SystemExit:
                    break
                except Exception as exc:
                    print(f"  \033[31m{type(exc).__name__}: {exc}\033[0m")
            except Exception as exc:
                print(f"  \033[31m{type(exc).__name__}: {exc}\033[0m")

        self._save_history()
