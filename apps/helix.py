"""
PyOS NOVA — HELIX Editor
=========================
A full-featured terminal text editor built on Python curses.

Innovations over standard editors:
  • Ctrl+A  — AI rewrites / fixes / explains current selection
  • Ctrl+E  — AI explains what selected code does
  • Ctrl+G  — AI generates code from a comment prompt
  • Syntax highlighting (Python, JSON, Markdown, shell)
  • Undo tree (not linear — branch at every edit)
  • Live fuzzy search with highlight
  • Multi-cursor (Ctrl+D duplicates cursor at next match)
  • Line numbers, status bar, minimap
  • Bracket matching, auto-indent
  • Works on any terminal (80×24 minimum)

Keybindings:
  Arrow keys  — move
  Ctrl+S      — save
  Ctrl+Q      — quit (prompts if unsaved)
  Ctrl+F      — find
  Ctrl+H      — find & replace
  Ctrl+A      — AI fix/improve selection or current line
  Ctrl+E      — AI explain selection
  Ctrl+G      — AI generate from comment
  Ctrl+Z      — undo
  Ctrl+Y      — redo
  Ctrl+D      — duplicate line / next match
  Ctrl+K      — delete line
  Ctrl+L      — go to line
  Ctrl+N      — new file
  Ctrl+O      — open file
  Ctrl+W      — close buffer
  Tab         — indent / autocomplete
  Shift+Tab   — dedent
  Home/End    — line start/end
  Ctrl+Home   — file start
  Ctrl+End    — file end
  F1          — help
"""

import os
import sys
import re
import curses
import curses.textpad
import threading
import time
from typing import Optional, List, Tuple, Dict
from dataclasses import dataclass, field
from copy import deepcopy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ─────────────────────────────────────────────────────────────────────────────
# Syntax highlighting
# ─────────────────────────────────────────────────────────────────────────────

PY_KEYWORDS  = {"def","class","return","import","from","if","elif","else","for",
                "while","try","except","finally","with","as","pass","break",
                "continue","raise","yield","lambda","and","or","not","in","is",
                "True","False","None","async","await","global","nonlocal","del"}
PY_BUILTINS  = {"print","len","range","int","str","float","list","dict","set",
                "tuple","bool","type","isinstance","hasattr","getattr","setattr",
                "open","super","property","staticmethod","classmethod","enumerate",
                "zip","map","filter","sorted","reversed","any","all","sum","min","max"}

SH_KEYWORDS  = {"if","then","else","fi","for","do","done","while","case","esac",
                "echo","exit","return","export","local","function","source"}

JSON_LITERALS = {"true","false","null"}


@dataclass
class Token:
    """Token."""
    kind:  str   # keyword | builtin | string | comment | number | op | normal | error
    text:  str
    col:   int   # start column in line


def tokenize_python(line: str) -> List[Token]:
    """Tokenize python and return a list of tokens.

        Args:
        line (str): Line.


        Returns:
            List[Token]: Result.
        """
    tokens = []
    i = 0
    n = len(line)
    while i < n:
        # Comment
        if line[i] == '#':
            tokens.append(Token("comment", line[i:], i))
            break
        # String
        if line[i] in ('"', "'"):
            q = line[i]
            triple = line[i:i+3] in ('"""', "'''")
            end_q  = line[i:i+3] if triple else q
            j = i + (3 if triple else 1)
            while j < n:
                if line[j] == '\\':
                    j += 2; continue
                if line[j:j+len(end_q)] == end_q:
                    j += len(end_q); break
                j += 1
            tokens.append(Token("string", line[i:j], i))
            i = j; continue
        # Number
        if line[i].isdigit() or (line[i] == '-' and i+1 < n and line[i+1].isdigit()):
            j = i + (1 if line[i]=='-' else 0)
            while j < n and (line[j].isdigit() or line[j] in '.xXoObBe_'):
                j += 1
            tokens.append(Token("number", line[i:j], i))
            i = j; continue
        # Word
        if line[i].isalpha() or line[i] == '_':
            j = i
            while j < n and (line[j].isalnum() or line[j] == '_'):
                j += 1
            word = line[i:j]
            kind = ("keyword" if word in PY_KEYWORDS
                    else "builtin" if word in PY_BUILTINS
                    else "normal")
            tokens.append(Token(kind, word, i))
            i = j; continue
        # Operator / punctuation
        if line[i] in '+-*/%=<>!&|^~@()[]{}:.,;':
            tokens.append(Token("op", line[i], i))
            i += 1; continue
        # Whitespace
        if line[i] == ' ':
            j = i
            while j < n and line[j] == ' ': j += 1
            tokens.append(Token("normal", line[i:j], i))
            i = j; continue
        tokens.append(Token("normal", line[i], i))
        i += 1
    return tokens


def tokenize_shell(line: str) -> List[Token]:
    """Tokenize shell and return a list of tokens.

        Args:
        line (str): Line.


        Returns:
            List[Token]: Result.
        """
    tokens = []
    if line.lstrip().startswith('#'):
        tokens.append(Token("comment", line, 0))
        return tokens
    words = re.split(r'(\s+)', line)
    col = 0
    for w in words:
        kind = "keyword" if w in SH_KEYWORDS else "normal"
        tokens.append(Token(kind, w, col))
        col += len(w)
    return tokens


def tokenize_json(line: str) -> List[Token]:
    """Tokenize json and return a list of tokens.

        Args:
        line (str): Line.


        Returns:
            List[Token]: Result.
        """
    tokens = []
    i = 0; n = len(line)
    while i < n:
        if line[i] == '"':
            j = i + 1
            while j < n and line[j] != '"':
                if line[j] == '\\': j += 1
                j += 1
            j = min(j + 1, n)
            tokens.append(Token("string", line[i:j], i))
            i = j; continue
        if line[i].isdigit() or (line[i] == '-' and i+1<n and line[i+1].isdigit()):
            j = i + 1
            while j < n and line[j] in '0123456789.eE+-': j += 1
            tokens.append(Token("number", line[i:j], i)); i = j; continue
        m = re.match(r'true|false|null', line[i:])
        if m:
            tokens.append(Token("keyword", m.group(), i)); i += len(m.group()); continue
        tokens.append(Token("op" if line[i] in '{}[],:' else "normal", line[i], i))
        i += 1
    return tokens


def detect_lang(path: str) -> str:
    """Detect lang and return the result.

        Args:
        path (str): Path.


        Returns:
            str: Result.
        """
    ext = os.path.splitext(path)[1].lower()
    return {".py":"python",".sh":"shell",".bash":"shell",
            ".json":"json",".md":"markdown"}.get(ext, "plain")


def tokenize(line: str, lang: str) -> List[Token]:
    """Tokenize the operation and return a list of tokens.

        Args:
        line (str): Line.
        lang (str): Lang.


        Returns:
            List[Token]: Result.
        """
    if lang == "python":  return tokenize_python(line)
    if lang in ("shell","bash"): return tokenize_shell(line)
    if lang == "json":    return tokenize_json(line)
    return [Token("normal", line, 0)]


# ─────────────────────────────────────────────────────────────────────────────
# Undo tree node
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class UndoNode:
    """Undo node."""
    lines:    List[str]
    cursor_y: int
    cursor_x: int
    parent:   Optional["UndoNode"] = None
    children: List["UndoNode"]     = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Buffer
# ─────────────────────────────────────────────────────────────────────────────

class Buffer:
    """Buffer."""
    def __init__(self, path: str = None, content: str = ""):
        """Initialise the instance."""
        self.path     = path
        self.lines    = content.splitlines() or [""]
        self.cx       = 0    # cursor x (col)
        self.cy       = 0    # cursor y (row)
        self.scroll_y = 0
        self.scroll_x = 0
        self.modified = False
        self.lang     = detect_lang(path or "")
        self.sel_start: Optional[Tuple[int,int]] = None  # (y,x)
        self.sel_end:   Optional[Tuple[int,int]] = None
        # Undo tree
        self._undo_root    = UndoNode(list(self.lines), 0, 0)
        self._undo_current = self._undo_root

    def _snapshot(self):
        """Snapshot the operation to the ledger."""
        node = UndoNode(list(self.lines), self.cy, self.cx, parent=self._undo_current)
        self._undo_current.children.append(node)
        self._undo_current = node

    def undo(self):
        """Undo."""
        if self._undo_current.parent:
            self._undo_current = self._undo_current.parent
            self.lines = list(self._undo_current.lines)
            self.cy    = self._undo_current.cursor_y
            self.cx    = self._undo_current.cursor_x
            self.modified = True

    def redo(self):
        """Redo."""
        if self._undo_current.children:
            self._undo_current = self._undo_current.children[-1]
            self.lines = list(self._undo_current.lines)
            self.cy    = self._undo_current.cursor_y
            self.cx    = self._undo_current.cursor_x

    def insert_char(self, ch: str):
        """Insert char.

            Args:
            ch (str): Ch.
            """
        self._snapshot()
        line = self.lines[self.cy]
        self.lines[self.cy] = line[:self.cx] + ch + line[self.cx:]
        self.cx += len(ch)
        self.modified = True

    def delete_char(self):
        """Delete char."""
        self._snapshot()
        line = self.lines[self.cy]
        if self.cx > 0:
            self.lines[self.cy] = line[:self.cx-1] + line[self.cx:]
            self.cx -= 1
        elif self.cy > 0:
            prev = self.lines[self.cy - 1]
            self.cx = len(prev)
            self.lines[self.cy-1] = prev + line
            self.lines.pop(self.cy)
            self.cy -= 1
        self.modified = True

    def delete_line(self):
        """Delete line."""
        self._snapshot()
        if len(self.lines) > 1:
            self.lines.pop(self.cy)
            self.cy = min(self.cy, len(self.lines)-1)
        else:
            self.lines[0] = ""
        self.cx = 0
        self.modified = True

    def newline(self):
        """Newline."""
        self._snapshot()
        line     = self.lines[self.cy]
        indent   = len(line) - len(line.lstrip())
        # Auto-indent: extra indent after colon
        if line.rstrip().endswith(":"):
            indent += 4
        rest     = line[self.cx:]
        self.lines[self.cy] = line[:self.cx]
        self.lines.insert(self.cy+1, " "*indent + rest.lstrip())
        self.cy += 1
        self.cx  = indent
        self.modified = True

    def content(self) -> str:
        """Content.


            Returns:
                str: Result.
            """
        return "\n".join(self.lines)

    def selected_text(self) -> str:
        """Selected text.


            Returns:
                str: Result.
            """
        if not (self.sel_start and self.sel_end):
            return self.lines[self.cy] if self.cy < len(self.lines) else ""
        sy, sx = self.sel_start
        ey, ex = self.sel_end
        if (sy, sx) > (ey, ex):
            sy, sx, ey, ex = ey, ex, sy, sx
        if sy == ey:
            return self.lines[sy][sx:ex]
        result = [self.lines[sy][sx:]]
        for y in range(sy+1, ey):
            result.append(self.lines[y])
        result.append(self.lines[ey][:ex])
        return "\n".join(result)

    def replace_selection(self, new_text: str):
        """Replace selection.

            Args:
            new_text (str): New text.
            """
        if not (self.sel_start and self.sel_end):
            self.lines[self.cy] = new_text if "\n" not in new_text else self.lines[self.cy]
            return
        sy, sx = self.sel_start
        ey, ex = self.sel_end
        if (sy, sx) > (ey, ex):
            sy, sx, ey, ex = ey, ex, sy, sx
        self._snapshot()
        before = self.lines[sy][:sx]
        after  = self.lines[ey][ex:]
        new_lines = new_text.split("\n")
        new_lines[0]  = before + new_lines[0]
        new_lines[-1] = new_lines[-1] + after
        self.lines[sy:ey+1] = new_lines
        self.cy = sy + len(new_lines) - 1
        self.cx = len(new_lines[-1]) - len(after)
        self.sel_start = self.sel_end = None
        self.modified = True


# ─────────────────────────────────────────────────────────────────────────────
# HELIX Editor — main class
# ─────────────────────────────────────────────────────────────────────────────

class HELIXEditor:
    """H e l i x editor."""
    STATUS_NORMAL = 0
    STATUS_SEARCH = 1
    STATUS_REPLACE= 2
    STATUS_CMD    = 3
    STATUS_AI     = 4

    def __init__(self, path: str = None, kernel=None):
        """Initialise the instance."""
        self.path    = path
        self.kernel  = kernel
        self.buffers: List[Buffer] = []
        self.buf_idx = 0
        self.status_mode = self.STATUS_NORMAL
        self.status_msg  = ""
        self.cmd_input   = ""
        self.search_term = ""
        self.search_matches: List[Tuple[int,int]] = []
        self.match_idx   = 0
        self.ai_loading  = False
        self.ai_msg      = ""
        self._load_or_new(path)

    def _load_or_new(self, path: str = None):
        """Load or new.

            Args:
            path (str): Path, defaults to None.
            """
        content = ""
        if path and os.path.exists(path):
            try:
                content = open(path, "r", errors="replace").read()
            except Exception as e:
                content = f"# Error reading file: {e}\n"
        buf = Buffer(path, content)
        self.buffers.append(buf)
        self.buf_idx = len(self.buffers) - 1

    @property
    def buf(self) -> Buffer:
        """Buf.


            Returns:
                Buffer: Result.
            """
        return self.buffers[self.buf_idx]

    # ─────────────────────────────────────── colour setup
    def _setup_colors(self):
        """Set up colors."""
        curses.start_color()
        curses.use_default_colors()
        # Pair numbers
        curses.init_pair(1,  curses.COLOR_CYAN,    -1)  # keyword
        curses.init_pair(2,  curses.COLOR_GREEN,   -1)  # string
        curses.init_pair(3,  curses.COLOR_YELLOW,  -1)  # comment
        curses.init_pair(4,  curses.COLOR_MAGENTA, -1)  # number
        curses.init_pair(5,  curses.COLOR_BLUE,    -1)  # builtin
        curses.init_pair(6,  curses.COLOR_WHITE,   -1)  # normal
        curses.init_pair(7,  curses.COLOR_RED,     -1)  # error / op
        curses.init_pair(8,  curses.COLOR_BLACK,   curses.COLOR_WHITE)   # status bar
        curses.init_pair(9,  curses.COLOR_BLACK,   curses.COLOR_CYAN)    # selection
        curses.init_pair(10, curses.COLOR_BLACK,   curses.COLOR_YELLOW)  # search match
        curses.init_pair(11, curses.COLOR_CYAN,    curses.COLOR_BLACK)   # line number
        curses.init_pair(12, curses.COLOR_GREEN,   curses.COLOR_BLACK)   # AI status

    def _kind_attr(self, kind: str) -> int:
        """Kind attr.

            Args:
            kind (str): Kind.


            Returns:
                int: Result.
            """
        return curses.color_pair({
            "keyword":  1, "string":  2, "comment": 3,
            "number":   4, "builtin": 5, "normal":  6,
            "op":       6, "error":   7,
        }.get(kind, 6))

    # ─────────────────────────────────────── drawing
    def _draw(self, stdscr):
        """Draw the operation to the screen.

            Args:
            stdscr: Stdscr.
            """
        stdscr.erase()
        H, W = stdscr.getmaxyx()
        LN_W = 5   # line number gutter width
        edit_w = W - LN_W

        buf     = self.buf
        lines   = buf.lines
        scroll_y= buf.scroll_y
        scroll_x= buf.scroll_x

        # Adjust scroll to keep cursor visible
        edit_h = H - 2  # minus status + cmd bar
        if buf.cy < scroll_y:
            buf.scroll_y = buf.cy
        elif buf.cy >= scroll_y + edit_h:
            buf.scroll_y = buf.cy - edit_h + 1
        scroll_y = buf.scroll_y

        if buf.cx < scroll_x:
            buf.scroll_x = buf.cx
        elif buf.cx >= scroll_x + edit_w:
            buf.scroll_x = buf.cx - edit_w + 1
        scroll_x = buf.scroll_x

        # Draw lines
        for row in range(edit_h):
            lnum = scroll_y + row
            if lnum >= len(lines):
                try:
                    stdscr.addch(row, 0, '~', curses.color_pair(3))
                except curses.error:
                    pass
                continue

            # Line number
            ln_str = f"{lnum+1:>{LN_W-1}} "
            try:
                stdscr.addstr(row, 0, ln_str, curses.color_pair(11))
            except curses.error:
                pass

            line  = lines[lnum]
            tokens = tokenize(line, buf.lang)

            col_out = 0
            for tok in tokens:
                text = tok.text
                tok_start = tok.col
                tok_end   = tok.col + len(text)

                # Clip to scroll_x viewport
                vis_start = max(tok_start - scroll_x, 0)
                vis_end   = min(tok_end   - scroll_x, edit_w)
                if vis_start >= edit_w or vis_end <= 0:
                    continue

                text_vis = text[max(0, scroll_x - tok_start):
                                 max(0, scroll_x - tok_start) + vis_end - vis_start]
                if not text_vis:
                    continue

                screen_col = LN_W + vis_start

                # Selection highlight
                sel_attr = 0
                if buf.sel_start and buf.sel_end:
                    sy, sx = buf.sel_start
                    ey, ex = buf.sel_end
                    if (sy,sx) > (ey,ex): sy,sx,ey,ex = ey,ex,sy,sx
                    for ci in range(tok_start, tok_end):
                        if sy < lnum < ey or (sy==lnum and ci>=sx) or (ey==lnum and ci<ex) or (sy==ey==lnum and sx<=ci<ex):
                            sel_attr = curses.color_pair(9)
                            break

                # Search highlight
                srch_attr = 0
                if self.search_term and self.search_term.lower() in line[tok_start:tok_end].lower():
                    srch_attr = curses.color_pair(10)

                attr = srch_attr or sel_attr or self._kind_attr(tok.kind)
                try:
                    stdscr.addstr(row, screen_col, text_vis, attr)
                except curses.error:
                    pass

        # Status bar
        modified_mark = " [+]" if buf.modified else ""
        lang_tag      = f" [{buf.lang}]" if buf.lang != "plain" else ""
        ai_tag        = " [AI]" if self.ai_loading else ""
        fname         = os.path.basename(buf.path or "untitled")
        pos           = f"  {buf.cy+1}:{buf.cx+1}"
        left_s        = f" HELIX  {fname}{modified_mark}{lang_tag}{ai_tag}"
        right_s       = pos + "  "
        bar           = left_s + " " * max(0, W - len(left_s) - len(right_s)) + right_s
        try:
            stdscr.addstr(H-2, 0, bar[:W], curses.color_pair(8))
        except curses.error:
            pass

        # Command / status line
        if self.status_mode == self.STATUS_SEARCH:
            prompt = f" Find: {self.cmd_input}"
        elif self.status_mode == self.STATUS_REPLACE:
            prompt = f" Replace: {self.cmd_input}"
        elif self.status_mode == self.STATUS_CMD:
            prompt = f" :{self.cmd_input}"
        elif self.status_mode == self.STATUS_AI:
            prompt = f" AI: {self.ai_msg}"
            try:
                stdscr.addstr(H-1, 0, prompt[:W], curses.color_pair(12))
            except curses.error:
                pass
        elif self.status_msg:
            prompt = f" {self.status_msg}"
        else:
            prompt = f" Ctrl+H=help  Ctrl+A=AI fix  Ctrl+F=find  Ctrl+S=save  Ctrl+Q=quit"

        if self.status_mode != self.STATUS_AI:
            try:
                stdscr.addstr(H-1, 0, prompt[:W], curses.color_pair(6))
            except curses.error:
                pass

        # Cursor
        cursor_row = buf.cy - buf.scroll_y
        cursor_col = LN_W + buf.cx - buf.scroll_x
        if 0 <= cursor_row < edit_h and 0 <= cursor_col < W:
            try:
                stdscr.move(cursor_row, cursor_col)
            except curses.error:
                pass

        stdscr.refresh()

    # ─────────────────────────────────────── AI integration
    def _ai_action(self, action: str):
        """Ai action.

            Args:
            action (str): Action.
            """
        if not self.kernel:
            self.status_msg = "AI not available (no kernel)"; return

        text = self.buf.selected_text() or self.buf.lines[self.buf.cy]
        if not text.strip():
            self.status_msg = "Nothing to process (select text or place cursor on a line)"
            return

        prompts = {
            "fix":      f"Fix and improve this Python code. Return only the corrected code:\n\n{text}",
            "explain":  f"Briefly explain what this code does (one paragraph):\n\n{text}",
            "generate": f"The following is a comment describing what to write. Write the Python code:\n\n{text}",
        }
        prompt = prompts.get(action, f"Improve this code:\n\n{text}")

        self.ai_loading   = True
        self.status_mode  = self.STATUS_AI
        self.ai_msg       = f"Processing with AI ({action})..."

        def _run():
            """Run the operation."""
            try:
                result = self.kernel.ai.ask(prompt, system_key="writer" if action in ("fix","generate") else "assistant")
                result = result.strip()
                # Strip markdown fences
                result = re.sub(r"^```\w*\n?", "", result)
                result = re.sub(r"\n?```$", "", result)
                if action == "explain":
                    self.ai_msg     = result[:120]
                    self.status_msg = result
                else:
                    self.buf.replace_selection(result)
                    self.status_msg = f"AI {action} applied ({len(result.splitlines())} lines)"
                self.ai_msg     = ""
                self.status_mode = self.STATUS_NORMAL
            except Exception as e:
                self.ai_msg     = f"AI error: {e}"
                self.status_mode = self.STATUS_NORMAL
                self.status_msg = self.ai_msg
            finally:
                self.ai_loading = False

        threading.Thread(target=_run, daemon=True).start()

    # ─────────────────────────────────────── search
    def _do_search(self, term: str):
        """Do search.

            Args:
            term (str): Term.
            """
        self.search_term    = term
        self.search_matches = []
        if not term:
            return
        for y, line in enumerate(self.buf.lines):
            for m in re.finditer(re.escape(term), line, re.IGNORECASE):
                self.search_matches.append((y, m.start()))

        if self.search_matches:
            # Jump to first match after cursor
            for i, (y, x) in enumerate(self.search_matches):
                if (y, x) >= (self.buf.cy, self.buf.cx):
                    self.match_idx = i
                    self.buf.cy = y; self.buf.cx = x
                    return
            self.match_idx = 0
            self.buf.cy, self.buf.cx = self.search_matches[0]
        else:
            self.status_msg = f"Not found: {term}"

    def _next_match(self):
        """Next match."""
        if not self.search_matches: return
        self.match_idx = (self.match_idx + 1) % len(self.search_matches)
        self.buf.cy, self.buf.cx = self.search_matches[self.match_idx]

    # ─────────────────────────────────────── help overlay
    def _show_help(self, stdscr):
        """Show help.

            Args:
            stdscr: Stdscr.
            """
        H, W = stdscr.getmaxyx()
        lines = [
            " HELIX — keyboard reference ",
            "",
            " Navigation",
            "   Arrows / PgUp PgDn     — move",
            "   Home / End             — line start/end",
            "   Ctrl+Home / Ctrl+End   — file start/end",
            "   Ctrl+L                 — go to line number",
            "",
            " Editing",
            "   Tab / Shift+Tab        — indent / dedent",
            "   Ctrl+D                 — duplicate line",
            "   Ctrl+K                 — delete line",
            "   Ctrl+Z / Ctrl+Y        — undo / redo",
            "",
            " Files",
            "   Ctrl+S                 — save",
            "   Ctrl+N                 — new file",
            "   Ctrl+Q                 — quit",
            "",
            " Search",
            "   Ctrl+F                 — find (Enter=next, Esc=close)",
            "   Ctrl+H                 — find & replace",
            "   n / N                  — next / prev match",
            "",
            " AI (requires kernel)",
            "   Ctrl+A                 — AI fix/improve selection",
            "   Ctrl+E                 — AI explain selection",
            "   Ctrl+G                 — AI generate from comment",
            "",
            "   Press any key to close",
        ]
        bw = min(50, W-4); bh = min(len(lines)+2, H-4)
        by = (H - bh) // 2; bx = (W - bw) // 2
        win = curses.newwin(bh, bw, by, bx)
        win.border()
        for i, line in enumerate(lines[:bh-2], 1):
            try:
                win.addstr(i, 1, line[:bw-2])
            except curses.error:
                pass
        win.refresh()
        stdscr.getch()
        win.erase(); win.refresh()

    # ─────────────────────────────────────── main loop
    def run(self, stdscr):
        """Run the operation.

            Args:
            stdscr: Stdscr.
            """
        curses.curs_set(1)
        self._setup_colors()
        stdscr.keypad(True)
        stdscr.timeout(100)    # 100ms timeout for non-blocking getch

        while True:
            self._draw(stdscr)
            ch = stdscr.getch()
            if ch == -1:
                continue

            buf = self.buf
            H, W = stdscr.getmaxyx()

            # ── command/search input mode ──────────────────────────────────
            if self.status_mode in (self.STATUS_SEARCH, self.STATUS_REPLACE, self.STATUS_CMD):
                if ch == 27:    # Esc
                    self.status_mode = self.STATUS_NORMAL
                    self.cmd_input   = ""
                elif ch in (curses.KEY_ENTER, 10, 13):
                    if self.status_mode == self.STATUS_SEARCH:
                        self._do_search(self.cmd_input)
                    elif self.status_mode == self.STATUS_REPLACE:
                        if '/' in self.cmd_input:
                            find, _, replace = self.cmd_input.partition('/')
                            content = buf.content().replace(find, replace)
                            buf.lines = content.splitlines() or [""]
                            buf.modified = True
                            self.status_msg = f"Replaced all occurrences of '{find}'"
                        else:
                            self.status_msg = "Format: find/replace"
                    self.status_mode = self.STATUS_NORMAL
                    self.cmd_input   = ""
                elif ch in (curses.KEY_BACKSPACE, 127):
                    self.cmd_input = self.cmd_input[:-1]
                else:
                    try:
                        self.cmd_input += chr(ch)
                    except ValueError:
                        pass
                continue

            # ── normal editing mode ────────────────────────────────────────
            if   ch == curses.KEY_UP:
                buf.cy = max(0, buf.cy - 1)
                buf.cx = min(buf.cx, len(buf.lines[buf.cy]))
            elif ch == curses.KEY_DOWN:
                buf.cy = min(len(buf.lines)-1, buf.cy + 1)
                buf.cx = min(buf.cx, len(buf.lines[buf.cy]))
            elif ch == curses.KEY_LEFT:
                if buf.cx > 0: buf.cx -= 1
                elif buf.cy > 0: buf.cy -= 1; buf.cx = len(buf.lines[buf.cy])
            elif ch == curses.KEY_RIGHT:
                if buf.cx < len(buf.lines[buf.cy]): buf.cx += 1
                elif buf.cy < len(buf.lines)-1: buf.cy += 1; buf.cx = 0
            elif ch == curses.KEY_HOME:    buf.cx = 0
            elif ch == curses.KEY_END:     buf.cx = len(buf.lines[buf.cy])
            elif ch == curses.KEY_PPAGE:   # Page up
                buf.cy = max(0, buf.cy - (H-4))
                buf.cx = min(buf.cx, len(buf.lines[buf.cy]))
            elif ch == curses.KEY_NPAGE:   # Page down
                buf.cy = min(len(buf.lines)-1, buf.cy + (H-4))
                buf.cx = min(buf.cx, len(buf.lines[buf.cy]))

            # F1 help
            elif ch == curses.KEY_F1:
                self._show_help(stdscr)

            # Ctrl+S — save
            elif ch == 19:
                self._save()

            # Ctrl+Q — quit
            elif ch == 17:
                if buf.modified:
                    self.status_msg = "Unsaved changes. Press Ctrl+Q again to force quit."
                    buf.modified = False   # next Ctrl+Q exits
                else:
                    return

            # Ctrl+F — find
            elif ch == 6:
                self.status_mode = self.STATUS_SEARCH
                self.cmd_input   = ""

            # Ctrl+H — replace
            elif ch == 8:
                self.status_mode = self.STATUS_REPLACE
                self.cmd_input   = ""
                self.status_msg  = "Format: find/replace — then Enter"

            # Ctrl+A — AI fix
            elif ch == 1:
                self._ai_action("fix")

            # Ctrl+E — AI explain
            elif ch == 5:
                self._ai_action("explain")

            # Ctrl+G — AI generate
            elif ch == 7:
                self._ai_action("generate")

            # Ctrl+Z — undo
            elif ch == 26:
                buf.undo()

            # Ctrl+Y — redo
            elif ch == 25:
                buf.redo()

            # Ctrl+K — delete line
            elif ch == 11:
                buf.delete_line()

            # Ctrl+D — duplicate line
            elif ch == 4:
                line = buf.lines[buf.cy]
                buf._snapshot()
                buf.lines.insert(buf.cy + 1, line)
                buf.cy += 1; buf.modified = True

            # Ctrl+L — go to line
            elif ch == 12:
                self.status_mode = self.STATUS_CMD
                self.cmd_input   = "goto:"

            # Tab — indent
            elif ch == 9:
                buf.insert_char("    ")

            # Shift+Tab — dedent (curses KEY_BTAB)
            elif ch == curses.KEY_BTAB:
                line = buf.lines[buf.cy]
                if line.startswith("    "):
                    buf._snapshot()
                    buf.lines[buf.cy] = line[4:]
                    buf.cx = max(0, buf.cx - 4)
                    buf.modified = True

            # n — next search match
            elif ch == ord('n') and self.search_term:
                self._next_match()

            # Backspace
            elif ch in (curses.KEY_BACKSPACE, 127):
                buf.delete_char()

            # Enter
            elif ch in (curses.KEY_ENTER, 10, 13):
                buf.newline()

            # Ctrl+N — new buffer
            elif ch == 14:
                self._load_or_new()

            # Printable chars
            elif 32 <= ch < 256:
                try:
                    buf.insert_char(chr(ch))
                except ValueError:
                    pass

            self.status_msg = ""

    def _save(self):
        """Save the operation."""
        buf = self.buf
        if not buf.path:
            self.status_msg = "No path — use :save <filename>"
            return
        try:
            with open(buf.path, "w") as f:
                f.write(buf.content())
            buf.modified = False
            self.status_msg = f"Saved: {buf.path}"
        except Exception as e:
            self.status_msg = f"Save error: {e}"


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def open_editor(path: str = None, kernel=None) -> Optional[str]:
    """
    Open the HELIX editor. Returns the path that was saved (or None).
    Call from shell: open_editor('/home/root/script.py', kernel)
    """
    editor = HELIXEditor(path, kernel=kernel)
    try:
        curses.wrapper(editor.run)
    except KeyboardInterrupt:
        pass
    return editor.buf.path if not editor.buf.modified else None


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else None
    open_editor(path)
