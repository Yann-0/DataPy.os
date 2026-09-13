"""
PyOS NOVA — Setup Wizard
=========================
Full curses TUI for first-boot and reconfiguration.

Screens:
  1. Welcome
  2. Language / locale
  3. Keyboard layout
  4. Hostname
  5. Network (DHCP or static IP)
  6. Timezone
  7. Root password
  8. AI model selection
  9. Summary → Apply

Run manually: setup
Run on first boot: automatically if /data/nova/.setup_done absent
"""

import os, sys, curses, json, subprocess, time, socket
from typing import List, Tuple, Optional

ROOT      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR  = os.environ.get("NOVA_DATA", os.path.expanduser("~/.nova"))
CFG_PATH  = os.path.join(DATA_DIR, "config.json")
DONE_PATH = os.path.join(DATA_DIR, ".setup_done")

sys.path.insert(0, ROOT)

# ─────────────────────────────────────────────────────────────────────────────
# Config schema
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_CFG = {
    "language":      "en_US",
    "keyboard":      "us",
    "hostname":      "nova",
    "timezone":      "UTC",
    "network_mode":  "dhcp",
    "ip_address":    "",
    "netmask":       "255.255.255.0",
    "gateway":       "",
    "dns":           "8.8.8.8",
    "ai_model":      "rag",
    "root_password": "",
    "setup_done":    False,
}

LANGUAGES = [
    ("en_US", "English (United States)"),
    ("en_GB", "English (United Kingdom)"),
    ("fr_FR", "Français (France)"),
    ("de_DE", "Deutsch (Deutschland)"),
    ("es_ES", "Español (España)"),
    ("it_IT", "Italiano (Italia)"),
    ("pt_BR", "Português (Brasil)"),
    ("ja_JP", "日本語"),
    ("zh_CN", "中文 (简体)"),
    ("ko_KR", "한국어"),
    ("ru_RU", "Русский"),
    ("ar_SA", "العربية"),
]

KEYBOARDS = [
    ("us",     "US English (QWERTY)"),
    ("gb",     "UK English"),
    ("fr",     "French (AZERTY)"),
    ("de",     "German (QWERTZ)"),
    ("es",     "Spanish"),
    ("it",     "Italian"),
    ("pt",     "Portuguese"),
    ("jp",     "Japanese"),
    ("ru",     "Russian"),
    ("dvorak", "Dvorak"),
    ("colemak","Colemak"),
]

TIMEZONES = [
    ("UTC",                    "UTC (Universal Coordinated Time)"),
    ("America/New_York",       "Eastern Time (US & Canada)"),
    ("America/Chicago",        "Central Time (US & Canada)"),
    ("America/Denver",         "Mountain Time (US & Canada)"),
    ("America/Los_Angeles",    "Pacific Time (US & Canada)"),
    ("America/Sao_Paulo",      "Brasília Time (Brazil)"),
    ("Europe/London",          "London (GMT/BST)"),
    ("Europe/Paris",           "Paris (CET/CEST)"),
    ("Europe/Berlin",          "Berlin (CET/CEST)"),
    ("Europe/Moscow",          "Moscow Time"),
    ("Asia/Dubai",             "Dubai (GST)"),
    ("Asia/Kolkata",           "India Standard Time"),
    ("Asia/Shanghai",          "China Standard Time"),
    ("Asia/Tokyo",             "Japan Standard Time"),
    ("Asia/Seoul",             "Korea Standard Time"),
    ("Australia/Sydney",       "Sydney (AEST/AEDT)"),
    ("Pacific/Auckland",       "Auckland (NZST/NZDT)"),
]

AI_MODELS = [
    ("rag",       "RAG knowledge base (no download, always works)",   0),
    ("tinyllama", "TinyLlama 1.1B — fast, 637 MB download",          637),
    ("phi3",      "Phi-3 Mini 4K — best quality, 2.3 GB download",  2350),
    ("mistral",   "Mistral 7B — most capable, 4.1 GB download",     4200),
]


# ─────────────────────────────────────────────────────────────────────────────
# TUI helpers
# ─────────────────────────────────────────────────────────────────────────────

def _setup_colors():
    """Set up colors."""
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_WHITE,   curses.COLOR_BLUE)    # header
    curses.init_pair(2, curses.COLOR_BLACK,   curses.COLOR_CYAN)    # selected item
    curses.init_pair(3, curses.COLOR_CYAN,    -1)                   # title
    curses.init_pair(4, curses.COLOR_YELLOW,  -1)                   # prompt
    curses.init_pair(5, curses.COLOR_GREEN,   -1)                   # ok/success
    curses.init_pair(6, curses.COLOR_RED,     -1)                   # error
    curses.init_pair(7, curses.COLOR_WHITE,   -1)                   # normal
    curses.init_pair(8, curses.COLOR_BLACK,   curses.COLOR_WHITE)   # button normal
    curses.init_pair(9, curses.COLOR_WHITE,   curses.COLOR_BLUE)    # button active
    curses.init_pair(10,curses.COLOR_BLACK,   curses.COLOR_GREEN)   # button OK


def _draw_header(stdscr, H, W):
    """Draw header to the screen.

        Args:
        stdscr: Stdscr.
        H: H.
        W: W.
        """
    header = " PyOS NOVA — Setup Wizard "
    stdscr.addstr(0, 0, " " * W, curses.color_pair(1))
    stdscr.addstr(0, max(0, (W - len(header)) // 2), header, curses.color_pair(1) | curses.A_BOLD)


def _draw_footer(stdscr, H, W, hint="  ↑↓ Navigate   Enter Select   Tab Next   Esc Back"):
    """Draw footer to the screen.

        Args:
        stdscr: Stdscr.
        H: H.
        W: W.
        hint: Hint, defaults to '  ↑↓ Navigate   Enter Select   Tab Next   Esc Back'.
        """
    stdscr.addstr(H-1, 0, " " * W, curses.color_pair(1))
    stdscr.addstr(H-1, 0, hint[:W], curses.color_pair(1))


def _draw_box(stdscr, y, x, h, w, title=""):
    """Draw box to the screen.

        Args:
        stdscr: Stdscr.
        y: Y.
        x: X.
        h: H.
        w: W.
        title: Title, defaults to ''.
        """
    try:
        win = curses.newwin(h, w, y, x)
        win.border()
        if title:
            win.addstr(0, 2, f" {title} ", curses.color_pair(3) | curses.A_BOLD)
        return win
    except curses.error:
        return None


def _center_text(stdscr, y, text, attr=0):
    """Center text.

        Args:
        stdscr: Stdscr.
        y: Y.
        text: Text.
        attr: Attr, defaults to 0.
        """
    H, W = stdscr.getmaxyx()
    x = max(0, (W - len(text)) // 2)
    try:
        stdscr.addstr(y, x, text[:W-x], attr)
    except curses.error:
        pass


def _input_field(stdscr, y, x, w, prompt, initial=""):
    """Draw an input field and return the entered string."""
    try:
        stdscr.addstr(y, x, prompt, curses.color_pair(4))
    except curses.error:
        pass
    field_x = x + len(prompt)
    field_w = w - len(prompt)
    value   = list(initial)
    cursor  = len(value)

    while True:
        # Draw field
        display = "".join(value)
        field_str = (display + " " * field_w)[:field_w]
        try:
            stdscr.addstr(y, field_x, field_str, curses.color_pair(8))
            stdscr.move(y, field_x + min(cursor, field_w-1))
        except curses.error:
            pass
        stdscr.refresh()
        ch = stdscr.getch()

        if ch in (curses.KEY_ENTER, 10, 13):
            return "".join(value)
        elif ch == 27:
            return None
        elif ch == 9:   # Tab — accept and move on
            return "".join(value)
        elif ch in (curses.KEY_BACKSPACE, 127):
            if cursor > 0:
                value.pop(cursor-1)
                cursor -= 1
        elif ch == curses.KEY_LEFT:
            cursor = max(0, cursor-1)
        elif ch == curses.KEY_RIGHT:
            cursor = min(len(value), cursor+1)
        elif 32 <= ch < 256:
            try:
                value.insert(cursor, chr(ch))
                cursor += 1
            except ValueError:
                pass


def _select_list(stdscr, y, x, h, w, items: List[Tuple[str,str]], current: int = 0) -> int:
    """
    Scrollable selection list. Returns selected index.
    items: list of (value, label) tuples
    """
    scroll = 0
    sel    = current
    visible = h - 2

    while True:
        # Adjust scroll
        if sel < scroll:        scroll = sel
        if sel >= scroll + visible: scroll = sel - visible + 1

        for i in range(visible):
            idx = scroll + i
            if idx >= len(items):
                try:
                    stdscr.addstr(y + i, x, " " * (w-2))
                except curses.error:
                    pass
                continue
            val, label = items[idx]
            marker = ">" if idx == sel else " "
            text   = f" {marker} {label}"
            text   = (text + " " * w)[:w]
            attr   = curses.color_pair(2) | curses.A_BOLD if idx == sel else curses.color_pair(7)
            try:
                stdscr.addstr(y + i, x, text, attr)
            except curses.error:
                pass

        # Scroll indicators
        if scroll > 0:
            try: stdscr.addstr(y, x + w - 3, " ↑ ", curses.color_pair(4))
            except: pass
        if scroll + visible < len(items):
            try: stdscr.addstr(y + visible - 1, x + w - 3, " ↓ ", curses.color_pair(4))
            except: pass

        stdscr.refresh()
        ch = stdscr.getch()

        if ch == curses.KEY_UP:     sel = max(0, sel-1)
        elif ch == curses.KEY_DOWN: sel = min(len(items)-1, sel+1)
        elif ch in (curses.KEY_ENTER, 10, 13, 9): return sel
        elif ch == 27:              return current


# ─────────────────────────────────────────────────────────────────────────────
# Wizard screens
# ─────────────────────────────────────────────────────────────────────────────

class SetupWizard:
    """Setup wizard."""
    def __init__(self):
        """Initialise the instance."""
        self.cfg = dict(DEFAULT_CFG)
        self._load_existing()
        self.step    = 0
        self.screens = [
            self._screen_welcome,
            self._screen_language,
            self._screen_keyboard,
            self._screen_hostname,
            self._screen_network,
            self._screen_timezone,
            self._screen_password,
            self._screen_ai_model,
            self._screen_summary,
        ]

    def _load_existing(self):
        """Load existing."""
        if os.path.exists(CFG_PATH):
            try:
                saved = json.load(open(CFG_PATH))
                self.cfg.update(saved)
            except Exception:
                pass

    # ──────────────────────────────── screens

    def _screen_welcome(self, stdscr, H, W) -> bool:
        """Screen welcome.

            Args:
            stdscr: Stdscr.
            H: H.
            W: W.


            Returns:
                bool: Result.
            """
        _draw_header(stdscr, H, W)
        _center_text(stdscr, 4,  "Welcome to PyOS NOVA", curses.color_pair(3) | curses.A_BOLD)
        _center_text(stdscr, 6,  "This wizard will configure your system.")
        _center_text(stdscr, 7,  "You can change these settings later by running 'setup'.")
        _center_text(stdscr, 9,  "What will be configured:", curses.color_pair(4))
        items = ["Language & keyboard", "Hostname", "Network (DHCP/static IP)",
                 "Timezone", "Root password", "AI model & download"]
        for i, item in enumerate(items):
            _center_text(stdscr, 11+i, f"  •  {item}", curses.color_pair(7))
        _center_text(stdscr, H-3, "Press Enter to begin  or  Esc to exit", curses.color_pair(4))
        _draw_footer(stdscr, H, W, "  Enter to start   Esc to exit")
        stdscr.refresh()
        while True:
            ch = stdscr.getch()
            if ch in (curses.KEY_ENTER, 10, 13): return True
            if ch == 27: return False
            if ch == 9:  return True

    def _screen_language(self, stdscr, H, W) -> bool:
        """Screen language.

            Args:
            stdscr: Stdscr.
            H: H.
            W: W.


            Returns:
                bool: Result.
            """
        stdscr.erase()
        _draw_header(stdscr, H, W)
        _center_text(stdscr, 2, "Select Language", curses.color_pair(3) | curses.A_BOLD)
        stdscr.addstr(4, 2, "Choose the system language:", curses.color_pair(4))
        current = next((i for i,(v,_) in enumerate(LANGUAGES) if v==self.cfg["language"]), 0)
        list_h  = min(len(LANGUAGES)+2, H-8)
        sel = _select_list(stdscr, 5, 2, list_h, W-4, LANGUAGES, current)
        self.cfg["language"] = LANGUAGES[sel][0]
        _draw_footer(stdscr, H, W)
        return True

    def _screen_keyboard(self, stdscr, H, W) -> bool:
        """Screen keyboard.

            Args:
            stdscr: Stdscr.
            H: H.
            W: W.


            Returns:
                bool: Result.
            """
        stdscr.erase()
        _draw_header(stdscr, H, W)
        _center_text(stdscr, 2, "Keyboard Layout", curses.color_pair(3) | curses.A_BOLD)
        stdscr.addstr(4, 2, "Choose keyboard layout:", curses.color_pair(4))
        current = next((i for i,(v,_) in enumerate(KEYBOARDS) if v==self.cfg["keyboard"]), 0)
        list_h  = min(len(KEYBOARDS)+2, H-8)
        sel = _select_list(stdscr, 5, 2, list_h, W-4, KEYBOARDS, current)
        self.cfg["keyboard"] = KEYBOARDS[sel][0]
        return True

    def _screen_hostname(self, stdscr, H, W) -> bool:
        """Screen hostname.

            Args:
            stdscr: Stdscr.
            H: H.
            W: W.


            Returns:
                bool: Result.
            """
        stdscr.erase()
        _draw_header(stdscr, H, W)
        _center_text(stdscr, 2, "Hostname", curses.color_pair(3) | curses.A_BOLD)
        stdscr.addstr(4, 2, "The hostname identifies this machine on the network.", curses.color_pair(7))
        stdscr.addstr(5, 2, "Use only letters, numbers and hyphens. Max 63 chars.", curses.color_pair(7))
        stdscr.addstr(7, 2, "Current: " + self.cfg["hostname"], curses.color_pair(4))
        result = _input_field(stdscr, 9, 2, W-4, "Hostname: ", self.cfg["hostname"])
        if result is not None:
            h = re.sub(r'[^a-zA-Z0-9\-]', '', result) or "nova"
            self.cfg["hostname"] = h[:63]
        return True

    def _screen_network(self, stdscr, H, W) -> bool:
        """Screen network.

            Args:
            stdscr: Stdscr.
            H: H.
            W: W.


            Returns:
                bool: Result.
            """
        stdscr.erase()
        _draw_header(stdscr, H, W)
        _center_text(stdscr, 2, "Network Configuration", curses.color_pair(3) | curses.A_BOLD)

        # Detect current IP
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            current_ip = s.getsockname()[0]
            s.close()
        except Exception:
            current_ip = "not detected"

        stdscr.addstr(4, 2, f"Current IP: {current_ip}", curses.color_pair(5))
        stdscr.addstr(6, 2, "Network mode:", curses.color_pair(4))
        modes = [("dhcp", "DHCP — automatic (recommended)"),
                 ("static", "Static IP — manual configuration")]
        current_mode = 0 if self.cfg["network_mode"] == "dhcp" else 1
        sel = _select_list(stdscr, 7, 2, 5, W-4, modes, current_mode)
        self.cfg["network_mode"] = modes[sel][0]

        if self.cfg["network_mode"] == "static":
            stdscr.erase()
            _draw_header(stdscr, H, W)
            _center_text(stdscr, 2, "Static IP Configuration", curses.color_pair(3) | curses.A_BOLD)
            fields = [
                ("ip_address", "IP Address  : ", "192.168.1.100"),
                ("netmask",    "Netmask     : ", "255.255.255.0"),
                ("gateway",    "Gateway     : ", "192.168.1.1"),
                ("dns",        "DNS Server  : ", "8.8.8.8"),
            ]
            for i, (key, prompt, default) in enumerate(fields):
                val = self.cfg.get(key) or default
                result = _input_field(stdscr, 4 + i*2, 2, W-4, prompt, val)
                if result is not None:
                    self.cfg[key] = result
        return True

    def _screen_timezone(self, stdscr, H, W) -> bool:
        """Screen timezone.

            Args:
            stdscr: Stdscr.
            H: H.
            W: W.


            Returns:
                bool: Result.
            """
        stdscr.erase()
        _draw_header(stdscr, H, W)
        _center_text(stdscr, 2, "Timezone", curses.color_pair(3) | curses.A_BOLD)
        stdscr.addstr(4, 2, "Select your timezone:", curses.color_pair(4))
        current = next((i for i,(v,_) in enumerate(TIMEZONES) if v==self.cfg["timezone"]), 0)
        list_h  = min(len(TIMEZONES)+2, H-8)
        sel = _select_list(stdscr, 5, 2, list_h, W-4, TIMEZONES, current)
        self.cfg["timezone"] = TIMEZONES[sel][0]
        return True

    def _screen_password(self, stdscr, H, W) -> bool:
        """Screen password.

            Args:
            stdscr: Stdscr.
            H: H.
            W: W.


            Returns:
                bool: Result.
            """
        stdscr.erase()
        _draw_header(stdscr, H, W)
        _center_text(stdscr, 2, "Root Password", curses.color_pair(3) | curses.A_BOLD)
        stdscr.addstr(4, 2, "Set a password for the root account.", curses.color_pair(7))
        stdscr.addstr(5, 2, "Leave blank to disable password authentication.", curses.color_pair(7))
        pw1 = _input_field(stdscr, 8,  2, W-4, "Password        : ")
        pw2 = _input_field(stdscr, 10, 2, W-4, "Confirm password: ")
        if pw1 is not None and pw2 is not None:
            if pw1 == pw2:
                self.cfg["root_password"] = pw1
                if pw1:
                    stdscr.addstr(12, 2, "Password set.", curses.color_pair(5))
                else:
                    stdscr.addstr(12, 2, "No password set (passwordless login).", curses.color_pair(4))
            else:
                stdscr.addstr(12, 2, "Passwords don't match — skipped.", curses.color_pair(6))
            stdscr.refresh(); time.sleep(1)
        return True

    def _screen_ai_model(self, stdscr, H, W) -> bool:
        """Screen ai model.

            Args:
            stdscr: Stdscr.
            H: H.
            W: W.


            Returns:
                bool: Result.
            """
        stdscr.erase()
        _draw_header(stdscr, H, W)
        _center_text(stdscr, 2, "AI Model", curses.color_pair(3) | curses.A_BOLD)
        stdscr.addstr(4, 2, "Choose the AI model for NOVA's chat, agent, and advisor.", curses.color_pair(7))
        stdscr.addstr(5, 2, "Larger models are more capable but need more storage and RAM.", curses.color_pair(7))

        items = [(v, f"{label}") for v, label, _ in AI_MODELS]
        current = next((i for i,(v,_,__) in enumerate(AI_MODELS) if v==self.cfg["ai_model"]), 0)
        list_h  = len(AI_MODELS) + 2

        sel = _select_list(stdscr, 7, 2, list_h, W-4, items, current)
        chosen = AI_MODELS[sel]
        self.cfg["ai_model"] = chosen[0]

        if chosen[2] > 0:
            stdscr.erase()
            _draw_header(stdscr, H, W)
            _center_text(stdscr, 3, "AI Model Download", curses.color_pair(3) | curses.A_BOLD)
            stdscr.addstr(5, 2, f"Selected: {chosen[1]}", curses.color_pair(4))
            stdscr.addstr(6, 2, f"Download size: ~{chosen[2]} MB", curses.color_pair(7))
            stdscr.addstr(8, 2, "Download now?", curses.color_pair(4))
            btn_items = [("yes","Yes — download now (needs internet)"),
                         ("no", "No  — download later with: llm download " + chosen[0])]
            dl_sel = _select_list(stdscr, 9, 2, 5, W-4, btn_items, 1)
            if dl_sel == 0:
                self._download_model(stdscr, H, W, chosen[0])
        return True

    def _download_model(self, stdscr, H, W, model: str):
        """Download model to local storage.

            Args:
            stdscr: Stdscr.
            H: H.
            W: W.
            model (str): Model.
            """
        stdscr.erase()
        _draw_header(stdscr, H, W)
        _center_text(stdscr, 3, "Downloading AI Model", curses.color_pair(3) | curses.A_BOLD)
        stdscr.addstr(5, 2, f"Downloading {model}...", curses.color_pair(4))
        stdscr.addstr(6, 2, "This may take several minutes.", curses.color_pair(7))
        stdscr.addstr(8, 2, "Progress:", curses.color_pair(7))
        stdscr.refresh()

        import urllib.request
        URLS = {
            "tinyllama": "https://huggingface.co/TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF/resolve/main/tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf",
            "phi3":      "https://huggingface.co/microsoft/Phi-3-mini-4k-instruct-gguf/resolve/main/Phi-3-mini-4k-instruct-q4.gguf",
            "mistral":   "https://huggingface.co/TheBloke/Mistral-7B-Instruct-v0.2-GGUF/resolve/main/mistral-7b-instruct-v0.2.Q4_K_M.gguf",
        }
        url  = URLS.get(model, URLS["tinyllama"])
        dest = os.path.join(DATA_DIR, "models", f"{model}.gguf")
        os.makedirs(os.path.dirname(dest), exist_ok=True)

        bar_w = W - 10

        def progress(block, bs, total):
            """Progress.

                Args:
                block: Block.
                bs: Bs.
                total: Total.
                """
            if total > 0:
                pct  = min(block * bs / total, 1.0)
                fill = int(pct * bar_w)
                bar  = "█" * fill + "░" * (bar_w - fill)
                mb   = block * bs / 1024 / 1024
                try:
                    stdscr.addstr(9, 2, f"[{bar}]", curses.color_pair(5))
                    stdscr.addstr(10, 2, f"{pct*100:.0f}%  {mb:.0f} MB         ")
                    stdscr.refresh()
                except curses.error:
                    pass

        try:
            urllib.request.urlretrieve(url, dest, reporthook=progress)
            stdscr.addstr(12, 2, f"Download complete: {dest}", curses.color_pair(5))
        except Exception as e:
            stdscr.addstr(12, 2, f"Download failed: {e}", curses.color_pair(6))
        stdscr.refresh()
        time.sleep(2)

    def _screen_summary(self, stdscr, H, W) -> bool:
        """Screen summary.

            Args:
            stdscr: Stdscr.
            H: H.
            W: W.


            Returns:
                bool: Result.
            """
        stdscr.erase()
        _draw_header(stdscr, H, W)
        _center_text(stdscr, 2, "Configuration Summary", curses.color_pair(3) | curses.A_BOLD)
        stdscr.addstr(4, 2, "Review your settings before applying:", curses.color_pair(4))

        rows = [
            ("Language",    self.cfg["language"]),
            ("Keyboard",    self.cfg["keyboard"]),
            ("Hostname",    self.cfg["hostname"]),
            ("Timezone",    self.cfg["timezone"]),
            ("Network",     self.cfg["network_mode"] + (f"  {self.cfg['ip_address']}" if self.cfg['network_mode']=='static' else "")),
            ("AI model",    self.cfg["ai_model"]),
            ("Password",    "set" if self.cfg.get("root_password") else "none (passwordless)"),
        ]
        for i, (label, value) in enumerate(rows):
            try:
                stdscr.addstr(6+i, 4, f"{label:<14}", curses.color_pair(4))
                stdscr.addstr(6+i, 18, value[:W-20], curses.color_pair(7))
            except curses.error:
                pass

        stdscr.addstr(6+len(rows)+1, 2, "Apply these settings?", curses.color_pair(4))
        btns = [("yes", "Apply and continue"), ("no", "Go back and edit"), ("cancel", "Exit without saving")]
        sel  = _select_list(stdscr, 6+len(rows)+2, 2, 6, W-4, btns, 0)
        if sel == 0:
            self._apply(stdscr, H, W)
            return True
        elif sel == 1:
            self.step = max(0, self.step - 2)
            return False
        else:
            return False

    # ──────────────────────────────────────── apply config
    def _apply(self, stdscr, H, W):
        """Apply the operation.

            Args:
            stdscr: Stdscr.
            H: H.
            W: W.
            """
        stdscr.erase()
        _draw_header(stdscr, H, W)
        _center_text(stdscr, 2, "Applying Configuration", curses.color_pair(3) | curses.A_BOLD)

        steps = [
            ("Saving configuration file",    self._save_config),
            ("Setting hostname",             self._apply_hostname),
            ("Configuring network",          self._apply_network),
            ("Setting timezone",             self._apply_timezone),
            ("Setting keyboard layout",      self._apply_keyboard),
        ]

        for i, (label, fn) in enumerate(steps):
            stdscr.addstr(5+i, 2, f"  {label}...", curses.color_pair(7))
            stdscr.refresh()
            try:
                fn()
                stdscr.addstr(5+i, 2, f"  {label}", curses.color_pair(5))
                stdscr.addstr(5+i, 2+len(label)+2, "  OK", curses.color_pair(5) | curses.A_BOLD)
            except Exception as e:
                stdscr.addstr(5+i, 2, f"  {label}", curses.color_pair(6))
                stdscr.addstr(5+i, W-20, str(e)[:18], curses.color_pair(6))
            stdscr.refresh()
            time.sleep(0.1)

        self.cfg["setup_done"] = True
        self._save_config()
        open(DONE_PATH, "w").write("done\n")

        _center_text(stdscr, 5+len(steps)+2, "Configuration complete!", curses.color_pair(5) | curses.A_BOLD)
        _center_text(stdscr, 5+len(steps)+3, "Changes take effect immediately.", curses.color_pair(7))
        _center_text(stdscr, 5+len(steps)+5, "Press any key to return to shell", curses.color_pair(4))
        _draw_footer(stdscr, H, W, "  Press any key to continue")
        stdscr.refresh()
        stdscr.getch()

    def _save_config(self):
        """Save config."""
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CFG_PATH, "w") as f:
            json.dump(self.cfg, f, indent=2)

    def _apply_hostname(self):
        """Apply hostname."""
        hostname = self.cfg["hostname"]
        try:
            with open("/etc/hostname", "w") as f: f.write(hostname + "\n")
        except PermissionError:
            pass
        try:
            subprocess.run(["hostname", hostname], capture_output=True, timeout=3)
        except Exception:
            pass

    def _apply_network(self):
        """Apply network."""
        mode = self.cfg["network_mode"]
        if mode == "dhcp":
            for iface in ["eth0", "enp0s3", "wlan0"]:
                try:
                    subprocess.run(["dhclient", iface], capture_output=True, timeout=5)
                    break
                except Exception:
                    pass
        elif mode == "static":
            ip  = self.cfg.get("ip_address", "")
            gw  = self.cfg.get("gateway", "")
            dns = self.cfg.get("dns", "8.8.8.8")
            if ip:
                for iface in ["eth0", "enp0s3"]:
                    try:
                        subprocess.run(["ip", "addr", "add", ip, "dev", iface], capture_output=True)
                        break
                    except Exception:
                        pass
                if gw:
                    try:
                        subprocess.run(["ip", "route", "add", "default", "via", gw], capture_output=True)
                    except Exception:
                        pass

    def _apply_timezone(self):
        """Apply timezone."""
        tz = self.cfg["timezone"]
        try:
            tz_file = f"/usr/share/zoneinfo/{tz}"
            if os.path.exists(tz_file):
                subprocess.run(["ln", "-sf", tz_file, "/etc/localtime"], capture_output=True)
            os.environ["TZ"] = tz
        except Exception:
            pass

    def _apply_keyboard(self):
        """Apply keyboard."""
        kb = self.cfg["keyboard"]
        try:
            subprocess.run(["loadkeys", kb], capture_output=True, timeout=3)
        except Exception:
            pass
        try:
            if os.path.exists("/etc/vconsole.conf"):
                with open("/etc/vconsole.conf", "w") as f:
                    f.write(f'KEYMAP="{kb}"\n')
        except Exception:
            pass

    # ──────────────────────────────────────── main run loop
    def run(self, stdscr):
        """Run the operation.

            Args:
            stdscr: Stdscr.
            """
        curses.curs_set(0)
        _setup_colors()
        stdscr.keypad(True)
        H, W = stdscr.getmaxyx()

        while self.step < len(self.screens):
            stdscr.erase()
            H, W = stdscr.getmaxyx()
            result = self.screens[self.step](stdscr, H, W)
            if result:
                self.step += 1
            else:
                if self.step > 0:
                    self.step -= 1
                else:
                    return   # exit wizard


def run_setup(kernel=None):
    """Launch the setup wizard."""
    wizard = SetupWizard()
    try:
        curses.wrapper(wizard.run)
    except KeyboardInterrupt:
        pass


def is_first_boot() -> bool:
    """Return True if first boot.


        Returns:
            bool: Result.
        """
    return not os.path.exists(DONE_PATH)


def load_config() -> dict:
    """Load config.


        Returns:
            dict: Result.
        """
    if os.path.exists(CFG_PATH):
        try:
            return {**DEFAULT_CFG, **json.load(open(CFG_PATH))}
        except Exception:
            pass
    return dict(DEFAULT_CFG)


# Regex import needed inside screens
import re

if __name__ == "__main__":
    run_setup()
