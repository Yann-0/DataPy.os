"""
PyOS NOVA — Multiplexer (nova-mux)
=====================================
Split-screen terminal multiplexer. Multiple shell sessions side by side.
Written entirely in Python curses.

Keys:
  Ctrl+B %   — split vertically
  Ctrl+B "   — split horizontally
  Ctrl+B arrows — move between panes
  Ctrl+B x   — close current pane
  Ctrl+B z   — zoom (fullscreen) current pane
  Ctrl+B c   — new window
  Ctrl+B n/p — next/prev window
  Ctrl+B ?   — help
  Ctrl+B d   — detach (exit mux, leave sessions running)
"""

import os, sys, curses, threading, queue, time, io
from typing import List, Optional, Tuple, TYPE_CHECKING

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

PREFIX = 2   # Ctrl+B = ASCII 2


class PaneBuffer:
    """Scrollback buffer for one pane."""
    SCROLLBACK = 500

    def __init__(self, h, w):
        """Initialise the instance."""
        self.h        = h
        self.w        = w
        self.lines: List[str] = [""] * h
        self.cursor_y = 0
        self.cursor_x = 0
        self._scroll  = 0
        self._lock    = threading.Lock()

    def resize(self, h, w):
        """Resize the operation.

            Args:
            h: H.
            w: W.
            """
        self.h = h; self.w = w

    def write(self, text: str):
        """Write the operation.

            Args:
            text (str): Text.
            """
        with self._lock:
            for ch in text:
                if ch == '\n':
                    self.cursor_y += 1
                    self.cursor_x  = 0
                    if self.cursor_y >= self.h:
                        self.lines.append("")
                        if len(self.lines) > self.SCROLLBACK:
                            self.lines.pop(0)
                        self.cursor_y = self.h - 1
                elif ch == '\r':
                    self.cursor_x = 0
                elif ch == '\b':
                    self.cursor_x = max(0, self.cursor_x - 1)
                else:
                    # Ensure line exists
                    while len(self.lines) <= self.cursor_y:
                        self.lines.append("")
                    line = self.lines[self.cursor_y]
                    # Pad
                    if self.cursor_x > len(line):
                        line = line + " " * (self.cursor_x - len(line))
                    # Insert
                    self.lines[self.cursor_y] = (line[:self.cursor_x] + ch +
                                                  line[self.cursor_x+1:])
                    self.cursor_x = min(self.cursor_x + 1, self.w - 1)

    def visible_lines(self) -> List[str]:
        """Visible lines.


            Returns:
                List[str]: Result.
            """
        with self._lock:
            start = max(0, len(self.lines) - self.h)
            raw   = self.lines[start:start+self.h]
            # pad to h
            while len(raw) < self.h:
                raw.append("")
            return raw


class PaneShell:
    """Runs a NovaShell inside a pane, capturing output."""

    def __init__(self, kernel, buf: PaneBuffer):
        """Initialise the instance."""
        self.kernel  = kernel
        self.buf     = buf
        self.inp     = queue.Queue()    # keystrokes go in
        self.running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """Start the operation."""
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        """Run the operation."""
        import io as _io
        # Monkey-patch sys.stdout to capture output
        class _Writer:
            """Writer."""
            def __init__(self, buf, orig):
                """Initialise the instance."""
                self._buf  = buf
                self._orig = orig
                self.encoding = "utf-8"
            def write(self, text):
                """Write the operation.

                    Args:
                    text: Text.
                    """
                self._buf.write(text)
            """Flush."""
            def flush(self): pass
            """Fileno."""
            def fileno(self): return self._orig.fileno()
            """Isatty."""
            def isatty(self): return True

        old_out = sys.stdout
        old_err = sys.stderr
        sys.stdout = _Writer(self.buf, old_out)
        sys.stderr = _Writer(self.buf, old_err)

        try:
            from shell.nova_shell import NovaShell
            shell = NovaShell(self.kernel)

            # Override input to read from our queue
            def _input(prompt=""):
                sys.stdout.write(prompt)
                chars = []
                while True:
                    ch = self.inp.get()
                    if ch is None:
                        raise EOFError
                    if ch == '\n':
                        sys.stdout.write('\n')
                        return "".join(chars)
                    elif ch == '\x7f':
                        if chars:
                            chars.pop()
                            sys.stdout.write('\b \b')
                    else:
                        chars.append(ch)
                        sys.stdout.write(ch)

            import builtins
            builtins.input = _input
            shell.run()
        except Exception as e:
            self.buf.write(f"\r\n[pane error: {e}]\r\n")
        finally:
            sys.stdout = old_out
            sys.stderr = old_err
            self.running = False

    def send_key(self, ch: str):
        """Send key.

            Args:
            ch (str): Ch.
            """
        self.inp.put(ch)

    def stop(self):
        """Stop the operation."""
        self.inp.put(None)
        self.running = False


class Pane:
    """Pane."""
    def __init__(self, y, x, h, w, kernel):
        """Initialise the instance."""
        self.y      = y
        self.x      = x
        self.h      = h
        self.w      = w
        self.buf    = PaneBuffer(h-2, w-2)
        self.shell  = PaneShell(kernel, self.buf)
        self.shell.start()
        self.active = False

    def resize(self, y, x, h, w):
        """Resize the operation.

            Args:
            y: Y.
            x: X.
            h: H.
            w: W.
            """
        self.y = y; self.x = x; self.h = h; self.w = w
        self.buf.resize(h-2, w-2)

    def draw(self, stdscr, active=False):
        """Draw the operation to the screen.

            Args:
            stdscr: Stdscr.
            active: Active, defaults to False.
            """
        border_attr = curses.color_pair(2) if active else curses.color_pair(1)
        try:
            for row in range(self.h):
                if row == 0 or row == self.h-1:
                    ch = '─'
                    stdscr.addstr(self.y+row, self.x, ('╔' if row==0 else '╚') +
                                  ch*(self.w-2) +
                                  ('╗' if row==0 else '╝'), border_attr)
                else:
                    stdscr.addstr(self.y+row, self.x, '║', border_attr)
                    stdscr.addstr(self.y+row, self.x+self.w-1, '║', border_attr)

            lines = self.buf.visible_lines()
            for i, line in enumerate(lines[:self.h-2]):
                display = (line + " "*(self.w-2))[:self.w-2]
                stdscr.addstr(self.y+1+i, self.x+1, display)

            # Cursor
            cy = self.buf.cursor_y - max(0, len(self.buf.lines)-self.h+2)
            if 0 <= cy < self.h-2:
                try:
                    stdscr.move(self.y+1+cy, self.x+1+self.buf.cursor_x)
                except curses.error:
                    pass
        except curses.error:
            pass

    def stop(self):
        """Stop the operation."""
        self.shell.stop()


class Mux:
    """The multiplexer window manager."""

    def __init__(self, kernel):
        """Initialise the instance."""
        self.kernel  = kernel
        self.panes:  List[Pane] = []
        self.active  = 0
        self.zoomed  = False
        self._prefix = False   # waiting for Ctrl+B command

    def _add_pane(self, y, x, h, w):
        """Add pane.

            Args:
            y: Y.
            x: X.
            h: H.
            w: W.
            """
        p = Pane(y, x, h, w, self.kernel)
        self.panes.append(p)
        return p

    def _init_panes(self, H, W):
        """Initialise panes.

            Args:
            H: H.
            W: W.
            """
        self.panes = []
        self._add_pane(0, 0, H-1, W)
        self.active = 0

    def _split_v(self, H, W):
        """Split active pane vertically."""
        if len(self.panes) >= 4:
            return
        p  = self.panes[self.active]
        p.stop()
        hw = p.w // 2
        self.panes[self.active] = Pane(p.y, p.x, p.h, hw, self.kernel)
        new = Pane(p.y, p.x+hw, p.h, p.w-hw, self.kernel)
        self.panes.insert(self.active+1, new)
        self.active = self.active+1

    def _split_h(self, H, W):
        """Split active pane horizontally."""
        if len(self.panes) >= 4:
            return
        p  = self.panes[self.active]
        p.stop()
        hh = p.h // 2
        self.panes[self.active] = Pane(p.y, p.x, hh, p.w, self.kernel)
        new = Pane(p.y+hh, p.x, p.h-hh, p.w, self.kernel)
        self.panes.insert(self.active+1, new)
        self.active = self.active+1

    def _close_pane(self):
        """Close pane and release resources."""
        if len(self.panes) <= 1:
            return False
        self.panes[self.active].stop()
        self.panes.pop(self.active)
        self.active = min(self.active, len(self.panes)-1)
        return True

    def run(self, stdscr):
        """Run the operation.

            Args:
            stdscr: Stdscr.
            """
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN,  -1)
        curses.init_pair(2, curses.COLOR_GREEN, -1)
        curses.init_pair(3, curses.COLOR_WHITE, curses.COLOR_BLUE)
        curses.curs_set(1)
        stdscr.timeout(50)

        H, W = stdscr.getmaxyx()
        self._init_panes(H, W)

        while True:
            try:
                H, W = stdscr.getmaxyx()
                stdscr.erase()

                # Status bar
                active_p = self.panes[self.active] if self.panes else None
                status   = f" nova-mux  {len(self.panes)} pane(s)  ^B=prefix  ^B?=help "
                if self._prefix:
                    status = " PREFIX — % split-v  \" split-h  x close  z zoom  arrows move  ? help"
                try:
                    stdscr.addstr(H-1, 0, (status+" "*W)[:W], curses.color_pair(3))
                except curses.error:
                    pass

                for i, p in enumerate(self.panes):
                    p.draw(stdscr, active=(i==self.active))

                stdscr.refresh()
                ch = stdscr.getch()
                if ch == -1:
                    continue

                if self._prefix:
                    self._prefix = False
                    if   ch == ord('%'): self._split_v(H, W)
                    elif ch == ord('"'): self._split_h(H, W)
                    elif ch == ord('x'): 
                        if not self._close_pane():
                            return  # last pane closed → exit
                    elif ch == ord('z'): self.zoomed = not self.zoomed
                    elif ch == ord('?'): self._show_help(stdscr)
                    elif ch == ord('d'): return
                    elif ch == curses.KEY_RIGHT:
                        self.active = (self.active+1) % len(self.panes)
                    elif ch == curses.KEY_LEFT:
                        self.active = (self.active-1) % len(self.panes)
                    elif ch == PREFIX:
                        # Ctrl+B Ctrl+B sends literal Ctrl+B
                        if self.panes:
                            self.panes[self.active].shell.send_key(chr(PREFIX))
                else:
                    if ch == PREFIX:
                        self._prefix = True
                    elif self.panes:
                        p = self.panes[self.active]
                        if ch == curses.KEY_BACKSPACE or ch == 127:
                            p.shell.send_key('\x7f')
                        elif ch in (curses.KEY_ENTER, 10, 13):
                            p.shell.send_key('\n')
                        elif 0 <= ch < 256:
                            p.shell.send_key(chr(ch))
            except (curses.error, KeyboardInterrupt):
                break

        for p in self.panes:
            p.stop()

    def _show_help(self, stdscr):
        """Show help.

            Args:
            stdscr: Stdscr.
            """
        H, W = stdscr.getmaxyx()
        lines = [
            " nova-mux keybindings ",
            "",
            " Ctrl+B %     split vertically",
            " Ctrl+B \"    split horizontally",
            " Ctrl+B x     close current pane",
            " Ctrl+B z     zoom / unzoom",
            " Ctrl+B ←→   switch pane",
            " Ctrl+B d     detach",
            " Ctrl+B ?     this help",
            "",
            " Press any key to close",
        ]
        bw = 40; bh = len(lines)+2
        by = (H-bh)//2; bx = (W-bw)//2
        try:
            win = curses.newwin(bh, bw, by, bx)
            win.border()
            for i, l in enumerate(lines, 1):
                win.addstr(i, 1, l[:bw-2])
            win.refresh()
            stdscr.getch()
            win.erase(); win.refresh()
        except curses.error:
            pass


def run_mux(kernel=None):
    """Run mux.

        Args:
        kernel: Kernel, defaults to None.
        """
    mux = Mux(kernel)
    try:
        curses.wrapper(mux.run)
    except KeyboardInterrupt:
        pass
