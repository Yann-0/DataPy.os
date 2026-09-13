"""
PyOS NOVA — Dashboard
=======================
Full-screen curses system monitor. Updates every second.
Shows: CPU per-core, RAM timeline, top processes, disk I/O,
network stats, SOS object store stats, AI status.

Usage: dashboard   (from shell)
       Ctrl+C to exit
"""

import os, sys, time, curses, threading, collections
from typing import List, Dict, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False


# ── history buffers ────────────────────────────────────────────────────────
HISTORY = 60   # seconds of history

class _Ring:
    """Fixed-size ring buffer."""
    def __init__(self, size=HISTORY):
        """Initialise the instance."""
        self._data = collections.deque([0.0]*size, maxlen=size)
    """Push.

        Args:
        v: V.
        """
    def push(self, v): self._data.append(float(v))
    """Last."""
    def last(self): return self._data[-1] if self._data else 0.0
    """Values."""
    def values(self): return list(self._data)
    """Avg."""
    def avg(self): return sum(self._data)/len(self._data) if self._data else 0


class Metrics:
    """Metrics."""
    def __init__(self):
        """Initialise the instance."""
        self.cpu_total  = _Ring()
        self.cpu_cores: List[_Ring] = []
        self.ram        = _Ring()
        self.swap       = _Ring()
        self.net_in     = _Ring()
        self.net_out    = _Ring()
        self.disk_read  = _Ring()
        self.disk_write = _Ring()
        self._prev_net  = (0, 0)
        self._prev_disk = (0, 0)
        self._lock      = threading.Lock()

    def collect(self):
        """Collect and return the operation."""
        if not HAS_PSUTIL:
            return
        with self._lock:
            cpu = psutil.cpu_percent(interval=0)
            self.cpu_total.push(cpu)

            per = psutil.cpu_percent(percpu=True, interval=0)
            while len(self.cpu_cores) < len(per):
                self.cpu_cores.append(_Ring())
            for i, v in enumerate(per):
                self.cpu_cores[i].push(v)

            vm = psutil.virtual_memory()
            self.ram.push(vm.percent)
            sw = psutil.swap_memory()
            self.swap.push(sw.percent)

            try:
                nio = psutil.net_io_counters()
                pi, po = self._prev_net
                self.net_in.push((nio.bytes_recv - pi) / 1024)
                self.net_out.push((nio.bytes_sent - po) / 1024)
                self._prev_net = (nio.bytes_recv, nio.bytes_sent)
            except Exception:
                pass

            try:
                dio = psutil.disk_io_counters()
                pr, pw = self._prev_disk
                self.disk_read.push((dio.read_bytes - pr) / 1024)
                self.disk_write.push((dio.write_bytes - pw) / 1024)
                self._prev_disk = (dio.read_bytes, dio.write_bytes)
            except Exception:
                pass

    def processes(self, n=10):
        """Processes.

            Args:
            n: N, defaults to 10.
            """
        if not HAS_PSUTIL: return []
        try:
            procs = []
            for p in psutil.process_iter(['pid','name','cpu_percent','memory_percent','status']):
                try: procs.append(p.info)
                except: pass
            return sorted(procs, key=lambda p: p.get('cpu_percent',0), reverse=True)[:n]
        except Exception:
            return []

    def mem_info(self):
        """Mem info."""
        if not HAS_PSUTIL:
            return {"total":0,"used":0,"percent":0}
        vm = psutil.virtual_memory()
        return {"total":vm.total//1024//1024,"used":vm.used//1024//1024,"percent":vm.percent}

    def disk_info(self):
        """Disk info."""
        if not HAS_PSUTIL:
            return []
        parts = []
        for p in psutil.disk_partitions():
            try:
                u = psutil.disk_usage(p.mountpoint)
                parts.append({"dev":p.device,"mp":p.mountpoint,"pct":u.percent,
                              "used":u.used//1024//1024,"total":u.total//1024//1024})
            except: pass
        return parts[:3]


def _sparkline(values: List[float], width: int, max_val: float = 100.0) -> str:
    """Draw a mini sparkline using block chars."""
    blocks = " ▁▂▃▄▅▆▇█"
    w      = max(1, width)
    chunk  = max(1, len(values)//w)
    bars   = []
    for i in range(w):
        seg = values[i*chunk:(i+1)*chunk]
        v   = sum(seg)/len(seg) if seg else 0
        idx = int(min(v/max(max_val,1), 1.0) * (len(blocks)-1))
        bars.append(blocks[idx])
    return "".join(bars)


def _bar(pct: float, width: int) -> str:
    """Bar.

        Args:
        pct (float): Pct.
        width (int): Width.


        Returns:
            str: Result.
        """
    filled = int(min(pct/100.0, 1.0) * width)
    return "█" * filled + "░" * (width - filled)


def _colour_for_pct(pct: float) -> int:
    """Colour for pct.

        Args:
        pct (float): Pct.


        Returns:
            int: Result.
        """
    if pct > 85: return curses.color_pair(3)   # red
    if pct > 60: return curses.color_pair(4)   # yellow
    return curses.color_pair(2)                # green


class Dashboard:
    """Dashboard."""
    def __init__(self, kernel=None):
        """Initialise the instance."""
        self.kernel  = kernel
        self.metrics = Metrics()
        self._running = False

    def _setup_colors(self):
        """Set up colors."""
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN,   -1)
        curses.init_pair(2, curses.COLOR_GREEN,  -1)
        curses.init_pair(3, curses.COLOR_RED,    -1)
        curses.init_pair(4, curses.COLOR_YELLOW, -1)
        curses.init_pair(5, curses.COLOR_WHITE,  curses.COLOR_BLUE)
        curses.init_pair(6, curses.COLOR_BLACK,  curses.COLOR_WHITE)
        curses.init_pair(7, curses.COLOR_MAGENTA,-1)

    def _header(self, win, H, W):
        """Header.

            Args:
            win: Win.
            H: H.
            W: W.
            """
        try:
            now    = time.strftime("%H:%M:%S")
            uptime = ""
            if HAS_PSUTIL:
                import datetime
                bt = psutil.boot_time()
                up = datetime.timedelta(seconds=int(time.time()-bt))
                uptime = f"  up {up}"
            title = f" PyOS NOVA — Dashboard{uptime}  {now} "
            win.addstr(0, 0, " "*W, curses.color_pair(5))
            win.addstr(0, max(0,(W-len(title))//2), title[:W], curses.color_pair(5)|curses.A_BOLD)
            win.addstr(1, 0, " "*W, curses.color_pair(6))
            hint = "  q quit   p processes   d disk   n network   s sos   r refresh"
            win.addstr(1, 0, hint[:W], curses.color_pair(6))
        except curses.error: pass

    def _draw_cpu(self, win, y, x, w):
        """Draw cpu to the screen.

            Args:
            win: Win.
            y: Y.
            x: X.
            w: W.
            """
        try:
            cpu = self.metrics.cpu_total.last()
            spark = _sparkline(self.metrics.cpu_total.values(), w-20)
            bar   = _bar(cpu, 10)
            win.addstr(y, x, "CPU ", curses.color_pair(1))
            win.addstr(y, x+4, f"{cpu:5.1f}% ", _colour_for_pct(cpu))
            win.addstr(y, x+11, bar[:10], _colour_for_pct(cpu))
            win.addstr(y, x+21, f" {spark}", curses.color_pair(7))

            cores = self.metrics.cpu_cores
            for i, core in enumerate(cores[:4]):
                cx = x + (i * (w//4))
                cv = core.last()
                cb = _bar(cv, 8)
                win.addstr(y+1, cx, f"C{i} ", curses.color_pair(1))
                win.addstr(y+1, cx+3, cb, _colour_for_pct(cv))
        except curses.error: pass

    def _draw_ram(self, win, y, x, w):
        """Draw ram to the screen.

            Args:
            win: Win.
            y: Y.
            x: X.
            w: W.
            """
        try:
            info  = self.metrics.mem_info()
            pct   = info["percent"]
            spark = _sparkline(self.metrics.ram.values(), w-20)
            bar   = _bar(pct, 10)
            win.addstr(y, x, "RAM ", curses.color_pair(1))
            win.addstr(y, x+4, f"{pct:5.1f}% ", _colour_for_pct(pct))
            win.addstr(y, x+11, bar[:10], _colour_for_pct(pct))
            win.addstr(y, x+21, f" {spark}", curses.color_pair(7))
            win.addstr(y+1, x, f"     {info['used']:>6}MB / {info['total']:>6}MB",
                       curses.color_pair(4))
        except curses.error: pass

    def _draw_net(self, win, y, x, w):
        """Draw net to the screen.

            Args:
            win: Win.
            y: Y.
            x: X.
            w: W.
            """
        try:
            ni   = self.metrics.net_in.last()
            no   = self.metrics.net_out.last()
            spki = _sparkline(self.metrics.net_in.values(), (w-4)//2, max_val=max(max(self.metrics.net_in.values()),1))
            spko = _sparkline(self.metrics.net_out.values(), (w-4)//2, max_val=max(max(self.metrics.net_out.values()),1))
            win.addstr(y, x, f"NET  ↓{ni:7.1f}KB/s  ↑{no:7.1f}KB/s", curses.color_pair(1))
            win.addstr(y+1, x, f"     {spki}│{spko}", curses.color_pair(7))
        except curses.error: pass

    def _draw_disk(self, win, y, x, w):
        """Draw disk to the screen.

            Args:
            win: Win.
            y: Y.
            x: X.
            w: W.
            """
        try:
            win.addstr(y, x, "DISK", curses.color_pair(1))
            parts = self.metrics.disk_info()
            for i, p in enumerate(parts[:2]):
                bar = _bar(p["pct"], 12)
                win.addstr(y+i, x+5, f"{p['mp']:<10} {bar} {p['pct']:4.0f}%",
                           _colour_for_pct(p["pct"]))
        except curses.error: pass

    def _draw_processes(self, win, y, x, w, h):
        """Draw processes to the screen.

            Args:
            win: Win.
            y: Y.
            x: X.
            w: W.
            h: H.
            """
        try:
            win.addstr(y, x, f"{'PID':>6} {'NAME':<16} {'CPU%':>5} {'MEM%':>5} STATUS",
                       curses.color_pair(1)|curses.A_BOLD)
            procs = self.metrics.processes(h-1)
            for i, p in enumerate(procs):
                if i >= h-1: break
                pid   = p.get("pid",0)
                name  = (p.get("name","?") or "?")[:16]
                cpu   = p.get("cpu_percent",0) or 0
                mem   = p.get("memory_percent",0) or 0
                stat  = (p.get("status","?") or "?")[:8]
                col   = curses.color_pair(3) if cpu>50 else curses.color_pair(2) if cpu>10 else curses.color_pair(4)
                win.addstr(y+1+i, x, f"{pid:>6} {name:<16} ", curses.color_pair(4))
                win.addstr(y+1+i, x+24, f"{cpu:5.1f}", col)
                win.addstr(y+1+i, x+30, f" {mem:5.1f} {stat}", curses.color_pair(4))
        except curses.error: pass

    def _draw_sos(self, win, y, x):
        """Draw sos to the screen.

            Args:
            win: Win.
            y: Y.
            x: X.
            """
        try:
            if self.kernel:
                win.addstr(y, x, "SOS", curses.color_pair(1)|curses.A_BOLD)
                conn = self.kernel.sos._pool.get()
                row  = conn.execute(
                    "SELECT (SELECT COUNT(*) FROM objects) n_obj,"
                    "(SELECT COUNT(*) FROM aliases) n_alias,"
                    "(SELECT COALESCE(SUM(size),0) FROM objects) total_bytes"
                ).fetchone()
                win.addstr(y+1, x, f" Objects : {row['n_obj']}", curses.color_pair(4))
                win.addstr(y+2, x, f" Aliases : {row['n_alias']}", curses.color_pair(4))
                win.addstr(y+3, x, f" Size    : {row['total_bytes']//1024} KB", curses.color_pair(4))
                ai_st = self.kernel.ai.status()
                win.addstr(y+4, x, f" AI tier : {ai_st['tier']}", curses.color_pair(2))
        except curses.error: pass

    def run(self, stdscr):
        """Run the operation.

            Args:
            stdscr: Stdscr.
            """
        self._setup_colors()
        curses.curs_set(0)
        stdscr.timeout(1000)

        # Initial collection
        if HAS_PSUTIL:
            psutil.cpu_percent(percpu=True, interval=0)
            psutil.cpu_percent(interval=0)
            time.sleep(0.1)

        self._running = True

        def collector():
            """Collector."""
            while self._running:
                self.metrics.collect()
                time.sleep(1)

        t = threading.Thread(target=collector, daemon=True)
        t.start()
        self.metrics.collect()

        while True:
            try:
                H, W = stdscr.getmaxyx()
                stdscr.erase()
                self._header(stdscr, H, W)

                half = W // 2
                # CPU + RAM left column
                self._draw_cpu(stdscr, 3, 0, half-2)
                self._draw_ram(stdscr, 6, 0, half-2)
                # Net + Disk right column
                self._draw_net(stdscr, 3, half, W-half-1)
                self._draw_disk(stdscr, 6, half, W-half-1)

                # Divider
                for row in range(9, H-1):
                    try: stdscr.addch(row, half-1, '│', curses.color_pair(4))
                    except: pass

                # Processes left
                self._draw_processes(stdscr, 9, 0, half-2, H-11)
                # SOS right
                self._draw_sos(stdscr, 9, half)

                stdscr.addstr(H-1, 0, " q=quit  Ctrl+C=exit ", curses.color_pair(6))
                stdscr.refresh()

                ch = stdscr.getch()
                if ch in (ord('q'), ord('Q'), 27): break
            except (curses.error, KeyboardInterrupt):
                break

        self._running = False


def run_dashboard(kernel=None):
    """Run dashboard.

        Args:
        kernel: Kernel, defaults to None.
        """
    d = Dashboard(kernel=kernel)
    try:
        curses.wrapper(d.run)
    except KeyboardInterrupt:
        pass
