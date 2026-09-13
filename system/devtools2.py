"""
PyOS NOVA — Developer Tools v2  (v0.0100)
==========================================
HTTP client, benchmark framework, lint/type tools, tutorial, notifications.
All missing from the Phase 2 roadmap.

Shell commands:
    http GET|POST|PUT|DELETE <url>
    bench "<expr>" [--n N]
    lint [path]
    type [path]
    tutorial [step|next]
    notify "<msg>" [--title T]
"""

from __future__ import annotations

import os, sys, json, time, subprocess, textwrap, urllib.request, urllib.error
from typing import Dict, List, Optional, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from kernel.nova import NovaKernel
    from store.sos import SemanticObjectStore

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)


# ── HTTP Client ───────────────────────────────────────────────────────────────

class HTTPClient:
    """HTTP client with JSON display and SOS response caching."""

    DEFAULT_HEADERS = {"User-Agent": "PyOS-NOVA/0.0100", "Accept": "application/json, */*"}

    def __init__(self, sos: "SemanticObjectStore"):
        """Initialise the HTTP client."""
        self._sos = sos
        self._history: List[dict] = []

    def request(self, method: str, url: str, headers: Dict[str,str]=None,
                 body: bytes=None, timeout: float=30.0) -> dict:
        """Make an HTTP request and return a result dict."""
        all_headers = {**self.DEFAULT_HEADERS, **(headers or {})}
        req = urllib.request.Request(url, data=body, method=method.upper(), headers=all_headers)
        t0  = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw   = resp.read(4*1024*1024)
                status = resp.status
                hdrs   = dict(resp.headers)
        except urllib.error.HTTPError as e:
            raw, status, hdrs = e.read(65536), e.code, {}
        except Exception as e:
            return {"error": str(e), "url": url, "status": 0}
        elapsed = (time.perf_counter()-t0)*1000
        body_str = raw.decode("utf-8", errors="replace")
        parsed   = None
        try: parsed = json.loads(body_str)
        except Exception: pass
        result = {"status": status, "headers": hdrs, "body": body_str[:10000],
                   "json": parsed, "elapsed_ms": round(elapsed,1), "url": url, "method": method.upper()}
        self._history.append({"ts": time.time(), "method": method, "url": url, "status": status})
        return result

    def get(self, url: str, **kw) -> dict:
        """HTTP GET."""
        return self.request("GET", url, **kw)

    def post(self, url: str, json_body: Any=None, data: bytes=None, **kw) -> dict:
        """HTTP POST."""
        headers = kw.pop("headers", {})
        if json_body is not None:
            data = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        return self.request("POST", url, headers=headers, body=data, **kw)

    def put(self, url: str, json_body: Any=None, **kw) -> dict:
        """HTTP PUT."""
        headers = kw.pop("headers", {})
        body    = None
        if json_body is not None:
            body = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        return self.request("PUT", url, headers=headers, body=body, **kw)

    def delete(self, url: str, **kw) -> dict:
        """HTTP DELETE."""
        return self.request("DELETE", url, **kw)

    def format_response(self, result: dict, verbose: bool=False) -> str:
        """Format an HTTP response for terminal display."""
        if "error" in result:
            return f"\033[31m  Error: {result['error']}\033[0m"
        s   = result["status"]
        col = "\033[32m" if 200<=s<300 else ("\033[33m" if 300<=s<400 else "\033[31m")
        lines = [f"  {col}{s}\033[0m  {result['method']} {result['url']}  ({result['elapsed_ms']} ms)"]
        if verbose:
            for k,v in list(result.get("headers",{}).items())[:5]:
                lines.append(f"  \033[2m{k}: {v}\033[0m")
        body = result.get("json") or result.get("body","")
        if isinstance(body,(dict,list)):
            formatted = json.dumps(body, indent=2, ensure_ascii=False)
            lines.extend(f"  {l}" for l in formatted[:2000].splitlines())
        elif body:
            lines.extend(f"  {l}" for l in str(body)[:500].splitlines())
        return "\n".join(lines)

    @property
    def history(self) -> List[dict]:
        """Return recent request history."""
        return self._history[-20:]


# ── Benchmark ─────────────────────────────────────────────────────────────────

class BenchmarkResult:
    """Results from one micro-benchmark run."""

    def __init__(self, expr: str, times: List[float]):
        """Initialise benchmark result."""
        self.expr = expr; self.times = times
        n = len(times) or 1
        self.mean   = sum(times)/n
        self.median = sorted(times)[n//2]
        self.min    = min(times) if times else 0
        self.max    = max(times) if times else 0
        self.stddev = (sum((t-self.mean)**2 for t in times)/max(n-1,1))**0.5

    def __str__(self) -> str:
        """Return human-readable benchmark summary."""
        if self.mean < 1e-6:   unit, div = "ns",  1e-9
        elif self.mean < 1e-3: unit, div = "µs",  1e-6
        elif self.mean < 1.0:  unit, div = "ms",  1e-3
        else:                  unit, div = "s",   1.0
        return (f"  {self.expr[:50]}\n"
                f"  n={len(self.times)}  mean={self.mean/div:.2f}{unit}  "
                f"min={self.min/div:.2f}{unit}  max={self.max/div:.2f}{unit}  "
                f"σ={self.stddev/div:.2f}{unit}")


class Benchmarker:
    """Micro-benchmark framework: time any Python expression N times."""

    def run(self, expr: str, n: int=1000, warmup: int=10, ns: dict=None) -> BenchmarkResult:
        """
        Benchmark an expression.

        Args:
            expr:   Python expression to time.
            n:      Timed iterations.
            warmup: Untimed warmup iterations.
            ns:     Eval namespace.

        Returns:
            BenchmarkResult with timing statistics.
        """
        namespace = ns or {}
        compiled  = compile(expr, "<bench>", "eval")
        for _ in range(warmup):
            try: eval(compiled, namespace)
            except Exception: break
        times = []
        for _ in range(n):
            t0 = time.perf_counter()
            try: eval(compiled, namespace)
            except Exception: pass
            times.append(time.perf_counter()-t0)
        return BenchmarkResult(expr, times)

    def compare(self, *exprs: str, n: int=1000, ns: dict=None) -> str:
        """Compare multiple expressions side by side."""
        results = sorted([self.run(e, n=n, ns=ns) for e in exprs], key=lambda r: r.mean)
        fastest = results[0].mean or 1e-15
        lines   = [f"  Comparison (n={n}):"]
        for r in results:
            ratio = r.mean/fastest
            if r.mean < 1e-3: unit,div = "µs",1e-6
            else:              unit,div = "ms",1e-3
            bar = "█"*min(30,int(ratio*4))
            lines.append(f"  {r.expr[:35]:<36} {r.mean/div:>8.2f}{unit}  {ratio:.1f}×  {bar}")
        return "\n".join(lines)


# ── Lint / type-check ─────────────────────────────────────────────────────────

class DevLinter:
    """Wraps ruff and mypy for in-NOVA code quality checking."""

    def lint(self, path: str=".") -> dict:
        """Run ruff on a file or directory."""
        try:
            r = subprocess.run([sys.executable,"-m","ruff","check",path],
                               capture_output=True, text=True, timeout=60)
            issues = sum(1 for l in r.stdout.splitlines() if l and not l.startswith("Found"))
            return {"tool":"ruff","path":path,"issues":issues,
                    "output":r.stdout[:3000],"returncode":r.returncode}
        except FileNotFoundError:
            return {"error":"ruff not installed — pip install ruff"}

    def typecheck(self, path: str=".") -> dict:
        """Run mypy on a file or directory."""
        try:
            r = subprocess.run([sys.executable,"-m","mypy",path,
                                "--ignore-missing-imports","--no-error-summary"],
                               capture_output=True, text=True, timeout=120)
            return {"tool":"mypy","path":path,
                    "errors":r.stdout.count(": error:"),
                    "warnings":r.stdout.count(": warning:"),
                    "output":r.stdout[:3000],"returncode":r.returncode}
        except FileNotFoundError:
            return {"error":"mypy not installed — pip install mypy"}

    def suggest_types(self, path: str, kernel: "NovaKernel") -> str:
        """Use AI to suggest type annotations for a file."""
        try:
            src  = kernel.sos.read(path)[:2000]
            return kernel.ai.ask(
                f"Add Python type annotations to these functions. "
                f"Return only the annotated signatures.\n\n{src}", max_tokens=400)
        except Exception as e:
            return f"AI annotation failed: {e}"


# ── Tutorial ──────────────────────────────────────────────────────────────────

TUTORIAL_STEPS = [
    ("Welcome to PyOS NOVA",
     "NOVA is a Python OS — Python is PID 1. Everything lives in the SOS.\n"
     "Try: ls /home/root   pwd   echo 'Hello NOVA'"),
    ("The Semantic Object Store",
     "SOS stores files as versioned content-addressed objects.\n"
     "Try: write /docs/hi.txt 'Hello'   cat /docs/hi.txt   history /docs/hi.txt"),
    ("Tags and Search",
     "Objects have tags. FTS5 + vector search index all content.\n"
     "Try: tag /docs/hi.txt important   find important   nl 'show text files'"),
    ("The AI Engine",
     "Local LLMs via llama.cpp. Ask, code, autonomously execute tasks.\n"
     "Try: ai 'What is asyncio?'   agent run 'Create a hello-world script'"),
    ("Security and Capabilities",
     "Capability tokens, ZK auth (password never stored/transmitted).\n"
     "Try: zk init myuser   cap grant /docs read,write   audit log"),
    ("HELIX Editor",
     "Terminal editor: syntax highlight, undo tree, LSP, AI assist.\n"
     "Try: helix /docs/hi.txt   (Ctrl+S save, Ctrl+Q quit, Ctrl+A AI)"),
    ("Shell Scripting",
     "Functions, aliases, loops, cron in .nova scripts.\n"
     "Try: alias ll 'ls -l'   write /s/greet.nova 'def greet(n): echo Hello {n}'"),
    ("Networking",
     "Zero-conf node discovery, CRDT sync, SSH server.\n"
     "Try: discover list   sshd 2222   http GET https://api.github.com"),
    ("Packages and Plugins",
     "pip wrapper with CVE audit. Plugins run sandboxed.\n"
     "Try: pkg list   pkg audit   plugin list   plugin search nova-git"),
    ("Monitoring and Deploy",
     "Prometheus metrics, tracing, K8s/Docker/Terraform manifests.\n"
     "Try: metrics   watchdog status   deploy k8s   lang set fr"),
]


class Tutorial:
    """Interactive NOVA tutorial system with 10 steps."""

    def __init__(self, kernel: "NovaKernel"):
        """Initialise the tutorial."""
        self.kernel   = kernel
        self._current = self._load_progress()

    def _load_progress(self) -> int:
        """Load progress from SOS."""
        try: return int(self.kernel.sos.read("/system/tutorial_progress"))
        except Exception: return 0

    def _save_progress(self, step: int):
        """Save progress to SOS."""
        try: self.kernel.sos.write("/system/tutorial_progress", str(step))
        except Exception: pass

    def show(self, step: int=None) -> str:
        """Display one tutorial step."""
        idx   = max(0, min(step if step is not None else self._current, len(TUTORIAL_STEPS)-1))
        title, text = TUTORIAL_STEPS[idx]
        fill  = "█"*(idx+1) + "░"*(len(TUTORIAL_STEPS)-idx-1)
        bar   = f"Step {idx+1}/{len(TUTORIAL_STEPS)}"
        lines = [f"\n  \033[36m{'─'*55}\033[0m",
                  f"  \033[1m{title}\033[0m   {bar}  [{fill}]",
                  f"  \033[36m{'─'*55}\033[0m"]
        for l in text.splitlines():
            if l.startswith("Try:"):
                lines.append(f"  \033[33m{l}\033[0m")
            else:
                lines.append(f"  {l}")
        lines.append("\n  \033[2mRun 'tutorial next' to continue.\033[0m")
        return "\n".join(lines)

    def next(self) -> str:
        """Advance to next step."""
        self._current = min(self._current+1, len(TUTORIAL_STEPS)-1)
        self._save_progress(self._current)
        return self.show()

    def jump(self, step: int) -> str:
        """Jump to step N."""
        self._current = max(0, min(step-1, len(TUTORIAL_STEPS)-1))
        self._save_progress(self._current)
        return self.show()


# ── Desktop Notifications ─────────────────────────────────────────────────────

class Notifier:
    """Cross-platform desktop notifications: notify-send, osascript, or terminal."""

    def notify(self, message: str, title: str="PyOS NOVA",
                urgency: str="normal", timeout_ms: int=5000) -> bool:
        """
        Send a desktop notification.

        Tries notify-send (Linux), osascript (macOS), then terminal fallback.

        Args:
            message:    Notification body.
            title:      Notification title.
            urgency:    low | normal | critical.
            timeout_ms: Display duration.

        Returns:
            True if notification was displayed.
        """
        return (self._notify_send(message, title, urgency, timeout_ms)
                or self._macos(message, title)
                or self._terminal(message, title, urgency))

    def _notify_send(self, msg, title, urgency, timeout_ms) -> bool:
        """Try libnotify notify-send (Linux/freedesktop)."""
        try:
            subprocess.run(["notify-send","--urgency",urgency,
                             "--expire-time",str(timeout_ms),title,msg],
                            timeout=3, check=True, capture_output=True)
            return True
        except Exception: return False

    def _macos(self, msg, title) -> bool:
        """Try macOS osascript."""
        try:
            subprocess.run(["osascript","-e",
                             f'display notification "{msg}" with title "{title}"'],
                            timeout=3, check=True, capture_output=True)
            return True
        except Exception: return False

    def _terminal(self, msg, title, urgency) -> bool:
        """Terminal bell + print fallback."""
        col = "\033[31m" if urgency=="critical" else ("\033[33m" if urgency=="normal" else "\033[2m")
        print(f"\n  {col}🔔 {title}: {msg}\033[0m\n")
        return True
