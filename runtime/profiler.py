"""
PyOS NOVA — Profiler & Session Recorder
==========================================

Part 1: Flame Graph Profiler
------------------------------
Profile any NOVA command or script. Generates a live SVG flame graph
stored in SOS and viewable in HELIX or any browser.

Shell commands:
  profile run <script.py> [args]  — profile a script
  profile cmd <command>            — profile a shell command
  profile top                      — live top-like profiler view
  profile report <id>              — show a saved profile
  profile list                     — list saved profiles

Part 2: Session Recorder (nova-cast)
--------------------------------------
Record shell sessions — every keystroke and terminal output — with
precise timing. Replay, share, and export as SVG animation.

Shell commands:
  rec                 — start recording
  rec stop            — stop and save
  play <session>      — replay a session
  cast list           — list recorded sessions
  cast export <id>    — export as SVG animation
"""

from __future__ import annotations
import os, sys, time, cProfile, pstats, io, json, threading, gzip
from typing import List, Dict, Optional, Tuple, Any, TYPE_CHECKING
from dataclasses import dataclass, field

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from kernel.nova import NovaKernel

PROFILES_BASE = "/system/profiles"
CASTS_BASE    = "/system/casts"


# ─────────────────────────────────────────────────── Flame Graph Profiler

def _profile_to_svg(stats_text: str, title: str = "NOVA Profile") -> str:
    """
    Convert pstats text output to a simplified SVG flame graph.

    Parses cumulative time per function and renders as a horizontal
    bar chart sorted by time (simplified flame graph).

    Args:
        stats_text (str): Raw pstats text output.
        title (str): SVG title.

    Returns:
        str: SVG document string.
    """
    import re
    functions: List[Tuple[float, str]] = []
    for line in stats_text.splitlines():
        m = re.match(r"\s+\d+\s+[\d.]+\s+[\d.]+\s+([\d.]+)\s+[\d.]+\s+(.+)", line)
        if m:
            cumtime = float(m.group(1))
            fname   = m.group(2).strip()
            if cumtime > 0.001:
                functions.append((cumtime, fname))

    functions.sort(reverse=True)
    functions = functions[:30]
    if not functions:
        return f'<svg><text x="10" y="20">No profile data</text></svg>'

    max_t   = functions[0][0]
    bar_h   = 20
    padding = 5
    w       = 700
    label_w = 280
    bar_w   = w - label_w - 60
    height  = len(functions) * (bar_h + padding) + 60

    lines = [
        f'<svg width="{w}" height="{height}" xmlns="http://www.w3.org/2000/svg">',
        f'<rect width="{w}" height="{height}" fill="#0d1117"/>',
        f'<text x="10" y="20" fill="#c9d1d9" font-size="13" font-weight="bold">{title}</text>',
    ]

    colors = ["#58a6ff","#3fb950","#d2a8ff","#f0883e","#79c0ff"]
    for i, (cumtime, fname) in enumerate(functions):
        y    = 35 + i * (bar_h + padding)
        frac = cumtime / max_t
        bw   = int(frac * bar_w)
        col  = colors[i % len(colors)]
        # Shorten function name
        short = fname[-45:] if len(fname) > 45 else fname
        pct   = f"{cumtime*1000:.1f}ms"
        lines.extend([
            f'<rect x="{label_w}" y="{y}" width="{bw}" height="{bar_h}" fill="{col}" opacity="0.8"/>',
            f'<text x="{label_w-4}" y="{y+14}" fill="#8b949e" font-size="10" text-anchor="end">{short}</text>',
            f'<text x="{label_w+bw+4}" y="{y+14}" fill="{col}" font-size="10">{pct}</text>',
        ])

    lines.append("</svg>")
    return "\n".join(lines)


class FlameProfiler:
    """Profiles NOVA scripts and commands, saves SVG flame graphs to SOS."""

    def __init__(self, kernel: "NovaKernel"):
        """Initialise the flame profiler."""
        self.kernel = kernel
        self._ensure_dirs()

    def _ensure_dirs(self):
        """Create profile storage directory."""
        if not self.kernel.sos.exists(PROFILES_BASE):
            self.kernel.sos.mkdir(PROFILES_BASE, parents=True)

    def profile_script(self, script_path: str,
                        args: List[str] = None) -> dict:
        """
        Profile a Python script from the SOS.

        Args:
            script_path (str): SOS path to the script.
            args (List[str]): Script arguments.

        Returns:
            dict: {profile_id, svg_path, top_functions, duration_s}
        """
        try:
            code_src = self.kernel.sos.read(script_path)
        except Exception as e:
            return {"error": str(e)}

        pr = cProfile.Profile()
        ns = {"kernel": self.kernel, "sos": self.kernel.sos}
        if args:
            sys.argv = [script_path] + args

        t = time.perf_counter()
        try:
            pr.enable()
            exec(compile(code_src, script_path, "exec"), ns)
            pr.disable()
        except Exception:
            pr.disable()
        duration = time.perf_counter() - t

        return self._save_profile(pr, script_path, duration)

    def profile_fn(self, fn, *args, **kwargs) -> dict:
        """
        Profile a Python function call.

        Args:
            fn: The callable to profile.

        Returns:
            dict: Profile results.
        """
        pr = cProfile.Profile()
        t  = time.perf_counter()
        pr.enable()
        try:
            fn(*args, **kwargs)
        finally:
            pr.disable()
        duration = time.perf_counter() - t
        return self._save_profile(pr, fn.__name__, duration)

    def _save_profile(self, pr: cProfile.Profile,
                       name: str, duration: float) -> dict:
        """Save profile data and generate SVG."""
        buf   = io.StringIO()
        stats = pstats.Stats(pr, stream=buf).sort_stats("cumulative")
        stats.print_stats(30)
        raw   = buf.getvalue()

        profile_id = f"prof_{int(time.time())}"
        svg        = _profile_to_svg(raw, title=f"Profile: {name}")

        svg_path  = f"{PROFILES_BASE}/{profile_id}.svg"
        data_path = f"{PROFILES_BASE}/{profile_id}.json"
        self.kernel.sos.write(svg_path, svg, tags=["flame-graph"])
        self.kernel.sos.write(data_path, json.dumps({
            "id": profile_id, "name": name,
            "duration_s": round(duration, 3),
            "raw": raw[:4000],
        }), tags=["profile-data"])

        # Extract top functions
        top = []
        import re
        for line in raw.splitlines():
            m = re.match(r"\s+\d+\s+[\d.]+\s+[\d.]+\s+([\d.]+)\s+[\d.]+\s+(.+)", line)
            if m:
                top.append({"ms": round(float(m.group(1))*1000,1),
                             "fn": m.group(2).strip()[-60:]})
                if len(top) >= 8:
                    break

        return {
            "profile_id": profile_id,
            "svg_path":   svg_path,
            "top":        top,
            "duration_s": round(duration, 3),
        }

    def list_profiles(self) -> List[dict]:
        """Return saved profiles."""
        results = []
        for name in self.kernel.sos.listdir(PROFILES_BASE):
            if not name.endswith(".json"):
                continue
            try:
                data = json.loads(self.kernel.sos.read(
                    f"{PROFILES_BASE}/{name}"))
                results.append({
                    "id":   data["id"],
                    "name": data["name"],
                    "ms":   round(data["duration_s"] * 1000),
                })
            except Exception:
                pass
        return sorted(results, key=lambda r: r["id"], reverse=True)


# ─────────────────────────────────────────────────── Session Recorder

@dataclass
class CastFrame:
    """One frame in a session recording."""
    ts:      float   # timestamp (seconds since session start)
    kind:    str     # "o" (output) or "i" (input)
    data:    str     # terminal data


class SessionRecorder:
    """
    Records shell sessions with precise timing (asciinema-compatible format).
    Saves to SOS, supports replay and SVG export.
    """

    def __init__(self, kernel: "NovaKernel"):
        """Initialise the session recorder."""
        self.kernel     = kernel
        self._frames:   List[CastFrame] = []
        self._recording = False
        self._start_ts  = 0.0
        self._session_id: Optional[str] = None
        self._ensure_dirs()

    def _ensure_dirs(self):
        """Create cast storage directory."""
        if not self.kernel.sos.exists(CASTS_BASE):
            self.kernel.sos.mkdir(CASTS_BASE, parents=True)

    def start(self) -> str:
        """
        Start recording.

        Returns:
            str: Session ID.
        """
        self._frames    = []
        self._recording = True
        self._start_ts  = time.time()
        self._session_id = f"cast_{int(self._start_ts)}"
        return self._session_id

    def record_output(self, data: str):
        """Record terminal output data."""
        if self._recording:
            ts = time.time() - self._start_ts
            self._frames.append(CastFrame(ts, "o", data))

    def record_input(self, data: str):
        """Record terminal input data."""
        if self._recording:
            ts = time.time() - self._start_ts
            self._frames.append(CastFrame(ts, "i", data))

    def stop(self) -> str:
        """
        Stop recording and save to SOS.

        Returns:
            str: SOS path of the saved session.
        """
        self._recording = False
        if not self._session_id:
            return ""

        # Asciinema v2 format
        header = json.dumps({
            "version": 2,
            "width":   80,
            "height":  24,
            "timestamp": int(self._start_ts),
            "title": f"NOVA session {self._session_id}",
        })
        lines = [header]
        for frame in self._frames:
            lines.append(json.dumps([frame.ts, frame.kind, frame.data]))

        content = "\n".join(lines)
        path    = f"{CASTS_BASE}/{self._session_id}.cast"
        self.kernel.sos.write(path, content, tags=["cast", "session"])
        return path

    def replay(self, session_id: str, speed: float = 1.0):
        """
        Replay a recorded session to the terminal.

        Args:
            session_id (str): Session to replay.
            speed (float): Playback speed multiplier.
        """
        path = f"{CASTS_BASE}/{session_id}.cast"
        try:
            content = self.kernel.sos.read(path)
        except Exception as e:
            print(f"  Session not found: {e}")
            return

        lines  = content.splitlines()
        prev_ts = 0.0
        for line in lines[1:]:  # skip header
            try:
                ts, kind, data = json.loads(line)
                delay = (ts - prev_ts) / speed
                if delay > 0:
                    time.sleep(min(delay, 2.0))   # cap at 2s
                if kind == "o":
                    sys.stdout.write(data)
                    sys.stdout.flush()
                prev_ts = ts
            except Exception:
                pass
        print()

    def export_svg(self, session_id: str) -> str:
        """
        Export a session as an SVG animation.

        Args:
            session_id (str): Session to export.

        Returns:
            str: SOS path of the SVG file.
        """
        path = f"{CASTS_BASE}/{session_id}.cast"
        try:
            content = self.kernel.sos.read(path)
        except Exception as e:
            return ""

        lines = content.splitlines()
        output_text = ""
        for line in lines[1:]:
            try:
                _, kind, data = json.loads(line)
                if kind == "o":
                    output_text += data
            except Exception:
                pass

        # Simple SVG representation
        display_lines = output_text.replace("\r\n", "\n").replace("\r", "\n")
        display_lines = display_lines[-2000:].splitlines()[-24:]  # last 24 lines
        svg_lines = [
            '<svg width="680" height="420" xmlns="http://www.w3.org/2000/svg">',
            '<rect width="680" height="420" fill="#0d1117" rx="8"/>',
            '<text x="10" y="20" fill="#58a6ff" font-size="11" font-family="monospace">',
            f'  PyOS NOVA — session {session_id}',
            '</text>',
        ]
        for i, txt in enumerate(display_lines):
            escaped = txt[:95].replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
            svg_lines.append(
                f'<text x="10" y="{35+i*16}" fill="#c9d1d9" '
                f'font-size="11" font-family="monospace">{escaped}</text>'
            )
        svg_lines.append("</svg>")

        svg_path = f"{CASTS_BASE}/{session_id}.svg"
        self.kernel.sos.write(svg_path, "\n".join(svg_lines),
                               tags=["cast-svg"])
        return svg_path

    def list_sessions(self) -> List[dict]:
        """Return saved sessions."""
        sessions = []
        for name in self.kernel.sos.listdir(CASTS_BASE):
            if not name.endswith(".cast"):
                continue
            sid = name[:-5]
            sessions.append({"id": sid, "path": f"{CASTS_BASE}/{name}"})
        return sorted(sessions, reverse=True)

    @property
    def is_recording(self) -> bool:
        """Return True if currently recording."""
        return self._recording
