"""
PyOS NOVA Shell
All commands, including SOS-aware operations.
"""

import os, re, sys, time, shlex, subprocess
from typing import TYPE_CHECKING

try:
    import readline
except ImportError:
    readline = None

if TYPE_CHECKING:
    from kernel.nova import NovaKernel

C = {
    "reset": "\033[0m", "dim": "\033[2m", "bold": "\033[1m",
    "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
    "blue": "\033[34m", "purple": "\033[35m", "cyan": "\033[36m", "white": "\033[37m",
}
col = lambda t, c: f"{C.get(c,'')}{t}{C['reset']}"


class CommandResult:
    """Result of a non-interactive shell command."""

    def __init__(self, ok: bool, command: str, error: str = "") -> None:
        self.ok = ok
        self.command = command
        self.error = error
        self.exit_code = 0 if ok else 1


class NovaShell:
    """Nova shell."""
    def __init__(self, kernel: "NovaKernel"):
        """Initialise the instance."""
        self.kernel   = kernel
        self.sos      = kernel.sos
        self.cwd      = "@"
        self.user     = "root"
        self.env      = {"HOME": "@", "USER": "root", "HOSTNAME": "datapy",
                         "SHELL": "nova-shell", "DATAPY_VERSION": "0.0008"}
        self.history  = []
        self._exit    = False
        self._reboot  = False
        self._chat_h  = []
        self._setup_readline()
        self._cmds_cache = self._build_cmds()
        self.kernel.procs.spawn("nova-shell",
         pid=5, user="root", cmd="[nova-shell]", state="R")

    def _setup_readline(self):
        """Set up readline when the host provides it."""
        if readline is None:
            return
        readline.parse_and_bind("tab: complete")
        readline.set_completer(self._complete)
        readline.set_completer_delims(" \t\n;|")

    def _complete(self, text, state):
        """Complete.

            Args:
            text: Text.
            state: State.
            """
        opts = []
        if readline is None:
            return None
        line = readline.get_line_buffer().split()
        if not line or (len(line)==1 and not readline.get_line_buffer().endswith(" ")):
            opts = [c for c in self._cmds_cache if c.startswith(text)]
        else:
            try:
                base = self._resolve(text) if text else self.cwd
                par  = base.rsplit("/", 1)[0] or "/"
                pre  = base.rsplit("/", 1)[-1]
                opts = [c for c in self.sos.listdir(par) if c.startswith(pre)]
            except: pass
        return opts[state] if state < len(opts) else None

    def _resolve(self, path=""):
        """Resolve.

            Args:
            path: Path, defaults to ''.
            """
        if not path or path == "~": return self.env.get("HOME","/home/root")
        if path.startswith("~/"): path = self.env["HOME"] + path[1:]
        return self.sos.resolve_path(path, self.cwd)

    def _prompt(self):
        """Prompt."""
        disp = self.cwd.replace(self.env.get("HOME",""), "~")
        pend = self.kernel.advisor.pending()
        badge = col(f" [{pend}!]", "yellow") if pend else ""
        uc = "red" if self.user=="root" else "green"
        return f"{col(self.user,uc)}{col('@','dim')}{col('nova','cyan')}{col(':','dim')}{col(disp,'blue')}{badge}{col('$ ','white')}"

    """Err.

        Args:
        m: M.
        """
    def _err(self, m): print(col(f"  {m}", "red"), file=sys.stderr)
    """Ok.

        Args:
        m: M.
        """
    def _ok(self,  m): print(col(f"  {m}", "green"))

    # ─── run loop ─────────────────────────────────────────────────────────────
    def run(self):
        """Run the operation."""
        try:
            motd = self.sos.read("/etc/motd")
            print(motd)
        except: pass

        print(col(f"  AI: {self.kernel.ai.model_name}", "dim"))
        print(col(f"  SOS: {len(self.sos.listdir('/'))} top-level objects", "dim"))
        print()

        while not self._exit:
            try:
                raw = input(self._prompt())
            except (KeyboardInterrupt, EOFError):
                print(); continue
            line = raw.strip()
            if not line or line.startswith("#"): continue
            self.history.append(line)
            self._exec_line(line)

        return "reboot" if self._reboot else "halt"

    def execute(self, line: str) -> CommandResult:
        """Run one command and return a success/failure contract."""
        try:
            code = self._run_cmd(line)
            if code:
                return CommandResult(False, line)
            return CommandResult(True, line)
        except Exception as exc:
            self._err(str(exc))
            return CommandResult(False, line, str(exc))

    def _exec_line(self, line):
        """Exec line.

            Args:
            line: Line.
            """
        segs = [s.strip() for s in line.split("|")]
        if len(segs) == 1:
            self._run_cmd(segs[0]); return
        import io
        stdin = ""
        for seg in segs:
            buf = io.StringIO(); old = sys.stdout; sys.stdout = buf
            try: self._run_cmd(seg)
            finally: sys.stdout = old
            stdin = buf.getvalue()
        print(stdin, end="")

    def _run_cmd(self, cmdline):
        """Run cmd.

            Args:
            cmdline: Cmdline.
            """
        cmdline = re.sub(r'\$(\w+)', lambda m: self.env.get(m.group(1),""), cmdline)
        redir_app, redir_file = False, None
        if ">>" in cmdline:
            cmdline, redir_file = cmdline.rsplit(">>",1); redir_file=redir_file.strip(); redir_app=True
        elif ">" in cmdline:
            cmdline, redir_file = cmdline.rsplit(">",1); redir_file=redir_file.strip()
        try:
            tokens = shlex.split(cmdline)
        except Exception:
            self._err(f"parse error: {cmdline}")
            return 1
        if not tokens:
            return 0
        cmd, args = tokens[0], tokens[1:]
        fn = self._cmds_cache.get(cmd)
        if not fn:
            self._err(f"{cmd}: command not found  (type 'help')")
            return 1
        try:
            if redir_file:
                import io
                buf = io.StringIO()
                old = sys.stdout
                sys.stdout = buf
                try:
                    fn(args)
                finally:
                    sys.stdout = old
                rp = self._resolve(redir_file)
                content = (
                    (self.sos.read(rp) if (redir_app and self.sos.exists(rp)) else "")
                    + buf.getvalue()
                )
                self.sos.write(rp, content)
            else:
                fn(args)
            return 0
        except Exception as exc:
            self._err(str(exc))
            return 1

    # ─── commands registry ────────────────────────────────────────────────────
    def _build_cmds(self):
        """Build and return cmds."""
        return {
            # Data plane — primary surface (flat handles, tags, links)
            "data": self._data_cmd, "d": self._data_cmd,
            "put": lambda a: self._data_cmd(["put"] + a),
            "get": lambda a: self._data_cmd(["get"] + a),
            # FS (legacy path aliases)
            "ls": self._ls, "ll": lambda a: self._ls(["-l"]+a),
            "cd": self._cd, "pwd": self._pwd,
            "mkdir": self._mkdir, "rm": self._rm,
            "cp": self._cp, "mv": self._mv,
            "cat": self._cat, "echo": self._echo,
            "touch": self._touch, "nano": self._nano,
            "find": self._find, "grep": self._grep,
            "wc": self._wc, "head": self._head, "tail": self._tail,
            # SOS extras
            "sos": self._sos,
            # System
            "ps": self._ps, "kill": self._kill, "top": self._top,
            "df": self._df, "free": self._free,
            "uname": self._uname, "whoami": self._whoami,
            "date": self._date, "uptime": self._uptime,
            # Shell
            "env": self._env, "export": self._export,
            "history": self._history, "clear": self._clear,
            "exit": self._exit_cmd, "reboot": self._reboot_cmd, "halt": self._exit_cmd,
            # AI
            "chat":   self._chat, "ask": self._ask,
            "agent":  self._agent, "advice": self._advice,
            "write":  self._write,
            # LLM management
            "llm": self._llm,
            # Search + tags
            "search":  self._search,
            "tag":     self._tag, "untag": self._untag,
            "tags":    self._tags, "findtag": self._findtag,
            # Packages
            "pip": self._pip,
            # Python
            "python3": self._python3, "python": self._python3, "run": self._python3,
            # Misc
            "calc": self._calc, "cowsay": self._cowsay,
            "help": self._help, "man": self._man,
            "helix": self._helix, "edit": self._helix,
            "setup": self._setup, "reboot": self._reboot_cmd, "halt": self._halt_cmd,
            "shutdown": self._halt_cmd,
            "gpu": self._gpu_cmd, "vbox": self._vbox_cmd,
            "dashboard": self._dashboard, "mux": self._mux,
            "review": self._review, "memory": self._memory_cmd,
            "agents": self._agents_cmd, "net": self._net_cmd,
            "crypto": self._crypto_cmd, "branch": self._branch_cmd,
            "audit": self._audit_cmd, "app": self._app_cmd,
            "fix": self._fix_cmd,
            "cap": self._cap_cmd, "capability": self._cap_cmd,
            "zk": self._zk_cmd,
            "lineage": self._lineage_cmd,
            "classify": self._classify_cmd,
            "timelock": self._timelock_cmd, "tl": self._timelock_cmd,
            "privacy": self._privacy_cmd,
            "find": self._nl_find_cmd, "nl": self._nl_find_cmd,
            "prefetch": self._prefetch_cmd,
            "immutable": self._immutable_cmd,
            "crdt": self._crdt_cmd,
            "trust": self._trust_cmd,
            "sandbox": self._sandbox_cmd,
            "reload": self._reload_cmd,
            "waq": self._waq_cmd,
            "workers": self._workers_cmd,
            "watch": self._watch_cmd,
            "on": self._on_cmd,
            "events": self._events_cmd,
            "compress": self._compress_cmd,
            "spec": self._spec_cmd,
            "kvcache": self._kvcache_cmd,
            "replay": self._replay_cmd,
            "wasm": self._wasm_cmd,
            "federated": self._federated_cmd,
            "debug": self._debug_cmd,
            "breakpoints": self._breakpoints_cmd,
            "make": self._make_cmd,
            "profile": self._profile_cmd,
            "rec": self._rec_cmd,
            "cast": self._cast_cmd,
            "play": self._play_cmd,
            "git": self._git_bridge_cmd,
            "sshd": self._sshd_cmd,
            "resilience": self._resilience_cmd,
            "trace": self._trace_cmd,
            "chaos": self._chaos_cmd,
            "view": self._view_cmd,
            "mvcc": self._mvcc_cmd,
            "pipeline": self._pipeline_cmd,
            "eventsrc": self._eventsrc_cmd,
            "schema": self._schema_cmd,
            "bloom": self._bloom_cmd,
            "lang": self._lang_cmd,
            "translate": self._translate_cmd,
            "watchdog": self._watchdog_cmd,
            "plugin": self._plugin_cmd,
            "pkg": self._pkg_cmd,
            "discover": self._discover_cmd,
            "gateway": self._gateway_cmd,
            "dns": self._dns_cmd,
            "model": self._model_cmd,
            "finetune": self._finetune_cmd,
            "tenant": self._tenant_cmd,
            "rbac": self._rbac_cmd,
            "ha": self._ha_cmd,
            "gdpr": self._gdpr_cmd,
            "metrics": self._metrics_cmd,
            "deploy": self._deploy_cmd,
            "repl": self._repl_cmd,
            "http": self._http_cmd,
            "bench": self._bench_cmd,
            "lint": self._lint_cmd,
            "type": self._type_cmd,
            "tutorial": self._tutorial_cmd,
            "notify": self._notify_cmd,
            "vpn": self._vpn_cmd,
            "mesh": self._mesh_cmd,
            "mail": self._mail_cmd,
            "stream": self._stream_cmd,
            "loadtest": self._loadtest_cmd,
            "source": self._source_cmd,
            "scriptcheck": self._scriptcheck_cmd,
            "http": self._http_cmd,
            "health": self._health_cmd,
            "vpn": self._vpn_cmd,
            "replicate": self._replicate_cmd,
            "ci": self._ci_cmd,
            "bench": self._bench_cmd,
            "context": self._context_cmd,
            "translate": self._translate_cmd,
            "bloom": self._bloom_cmd,
            "lang": self._lang_cmd,
            "translate": self._translate_cmd,
            "watchdog": self._watchdog_cmd,
            "plugin": self._plugin_cmd,
            "pkg": self._pkg_cmd,
            "discover": self._discover_cmd,
            "gateway": self._gateway_cmd,
            "dns": self._dns_cmd,
            "model": self._model_cmd,
            "finetune": self._finetune_cmd,
            "tenant": self._tenant_cmd,
            "rbac": self._rbac_cmd,
            "ha": self._ha_cmd,
            "gdpr": self._gdpr_cmd,
            "metrics": self._metrics_cmd,
            "deploy": self._deploy_cmd,
            "repl": self._repl_cmd,
            "http": self._http_cmd,
            "bench": self._bench_cmd,
            "lint": self._lint_cmd,
            "type": self._type_cmd,
            "tutorial": self._tutorial_cmd,
            "notify": self._notify_cmd,
            "vpn": self._vpn_cmd,
            "mesh": self._mesh_cmd,
            "mail": self._mail_cmd,
            "stream": self._stream_cmd,
            "loadtest": self._loadtest_cmd,
            "source": self._source_cmd,
            "scriptcheck": self._scriptcheck_cmd,
            "http": self._http_cmd,
            "health": self._health_cmd,
            "vpn": self._vpn_cmd,
            "replicate": self._replicate_cmd,
            "ci": self._ci_cmd,
            "bench": self._bench_cmd,
            "context": self._context_cmd,
            "translate": self._translate_cmd,
            "schema": self._schema_cmd,
            "stream": self._stream_cmd,
            "streams": self._stream_cmd,
            "eventsource": self._eventsource_cmd,
            "es": self._eventsource_cmd,
            "view": self._view_cmd,
            "mvcc": self._mvcc_cmd,
            "circuit": self._circuit_cmd,
            "ratelimit": self._ratelimit_cmd,
            "traces": self._trace_cmd,
            "ssh-server": self._sshserver_cmd,
            "git-bridge": self._gitbridge_cmd,
            "chaos": self._chaos_cmd,
            "dns": self._dns_cmd,
            "debug": self._debug_cmd,
            "make": self._make_cmd,
            "profile": self._profile_cmd,
            "rec": self._rec_cmd,
            "record": self._rec_cmd,
            "sql": self._sql_cmd, "alias": self._alias_cmd,
            "cron": self._cron_cmd, "voice": self._voice_cmd,
            "serve": lambda a: self._net_cmd(["serve"]+a),
            "api": lambda a: self._net_cmd(["api"]+a),
            "sync": lambda a: self._net_cmd(["sync"]+a),
            "discover": lambda a: self._net_cmd(["discover"]),
            "task": lambda a: self._agents_cmd(["task"]+a),

            "syscalls": self._syscalls,
        }

    # ════ FILESYSTEM ═════════════════════════════════════════════════════════
    def _ls(self, args):
        """Ls.

            Args:
            args: Args.
            """
        flags=""; paths=[]
        for a in args:
            if a.startswith("-"): flags+=a[1:]
            else: paths.append(a)
        if not paths: paths=[self.cwd]
        for path in paths:
            rp = self._resolve(path)
            try:
                if self.sos.is_file(rp):
                    print(rp.split("/")[-1]); continue
                children = self.sos.listdir(rp)
                if "a" not in flags: children = [c for c in children if not c.startswith(".")]
                children.sort()
                if "l" in flags:
                    print(f"total {len(children)}")
                    for c in children:
                        fp   = rp.rstrip("/")+"/"+c
                        node = self.sos._inodes.get(fp)
                        if not node: continue
                        perm = "drwxr-xr-x" if node.is_dir() else "-rw-r--r--"
                        tags = self.sos.get_tags(fp)
                        tstr = " "+" ".join(col(f"#{t}","purple") for t in tags) if tags else ""
                        nm   = col(c+"/","cyan") if node.is_dir() else c
                        print(f"{perm} {self.user:8} {node.size:6} {nm}{tstr}")
                else:
                    row=""
                    for c in children:
                        fp  = rp.rstrip("/")+"/"+c
                        nd  = self.sos._inodes.get(fp)
                        nm  = col(c+"/","cyan") if (nd and nd.is_dir()) else c
                        row += f"{nm:<25}"
                    print(row)
            except Exception as e: self._err(str(e))

    def _cd(self, args):
        """Cd.

            Args:
            args: Args.
            """
        t  = args[0] if args else self.env.get("HOME","/")
        rp = self._resolve(t)
        if not self.sos.exists(rp): self._err(f"cd: {t}: No such object"); return
        if not self.sos.is_dir(rp): self._err(f"cd: {t}: Not a directory-object"); return
        self.cwd = rp

    """Pwd.

        Args:
        _:  .
        """
    def _pwd(self, _): print(self.cwd)

    def _mkdir(self, args):
        """Mkdir.

            Args:
            args: Args.
            """
        parents = "-p" in args
        for a in [x for x in args if not x.startswith("-")]:
            try: self.sos.mkdir(self._resolve(a), parents=parents)
            except Exception as e: self._err(str(e))

    def _rm(self, args):
        """Rm.

            Args:
            args: Args.
            """
        rec = any(a in ("-r","-rf","-fr","-R") for a in args)
        for a in [x for x in args if not x.startswith("-")]:
            try:
                rp = self._resolve(a)
                self.sos.remove(rp, recursive=rec)
            except Exception as e: self._err(str(e))

    def _cp(self, args):
        """Cp.

            Args:
            args: Args.
            """
        if len(args)<2: self._err("cp: missing destination"); return
        try: self.sos.copy(self._resolve(args[-2]), self._resolve(args[-1]))
        except Exception as e: self._err(str(e))

    def _mv(self, args):
        """Mv.

            Args:
            args: Args.
            """
        if len(args)<2: self._err("mv: missing destination"); return
        src=self._resolve(args[-2]); dst=self._resolve(args[-1])
        try: self.sos.move(src, dst); self.kernel.search.index_now(dst)
        except Exception as e: self._err(str(e))

    def _cat(self, args):
        """Cat.

            Args:
            args: Args.
            """
        if not args: self._err("cat: missing operand"); return
        for a in args:
            try: print(self.sos.read(self._resolve(a)))
            except Exception as e: self._err(str(e))

    """Echo.

        Args:
        args: Args.
        """
    def _echo(self, args): print(" ".join(args))

    def _touch(self, args):
        """Touch.

            Args:
            args: Args.
            """
        for a in args:
            rp = self._resolve(a)
            if not self.sos.exists(rp):
                self.sos.write(rp, "")
                self.kernel.search.index_now(rp)

    def _nano(self, args):
        """Nano.

            Args:
            args: Args.
            """
        if not args: self._err("nano: no path"); return
        rp = self._resolve(args[0])
        content = ""
        if self.sos.exists(rp) and self.sos.is_file(rp):
            content = self.sos.read(rp)
        print(col(f"\n  [ nano — {args[0]} ]  (enter blank line to finish)", "yellow"))
        for i, ln in enumerate(content.splitlines(), 1): print(f"  {col(str(i),'dim')}  {ln}")
        try:
            lines = []
            while True:
                ln = input("  > ")
                if ln == "" and lines: break
                lines.append(ln)
            if lines:
                self.sos.write(rp, "\n".join(lines)+"\n")
                self.kernel.search.index_now(rp)
                self._ok(f"Saved: {args[0]}")
        except (KeyboardInterrupt, EOFError):
            print(col("  Cancelled.", "dim"))

    def _find(self, args):
        """Find and return the operation.

            Args:
            args: Args.
            """
        base = self._resolve(args[0]) if args else self.cwd
        nf   = None
        if "-name" in args:
            i = args.index("-name")
            if i+1<len(args): nf = args[i+1].replace("*","")
        def walk(p):
            """Walk.

                Args:
                p: P.
                """
            try:
                for c in self.sos.listdir(p):
                    fp = p.rstrip("/")+"/"+c
                    if nf is None or nf in c: print(fp)
                    if self.sos.is_dir(fp): walk(fp)
            except: pass
        walk(base)

    def _grep(self, args):
        """Grep.

            Args:
            args: Args.
            """
        if len(args)<2: self._err("grep: usage: grep <pattern> <file>"); return
        pat = args[0]
        for f in args[1:]:
            try:
                for i, ln in enumerate(self.sos.read(self._resolve(f)).splitlines(), 1):
                    if pat.lower() in ln.lower():
                        print(f"{col(f,'cyan')}:{col(str(i),'yellow')}:{ln}")
            except Exception as e: self._err(str(e))

    def _wc(self, args):
        """Wc.

            Args:
            args: Args.
            """
        for a in args:
            try:
                c = self.sos.read(self._resolve(a))
                print(f"  {c.count(chr(10)):6} {len(c.split()):6} {len(c):6} {a}")
            except Exception as e: self._err(str(e))

    def _head(self, args):
        """Head.

            Args:
            args: Args.
            """
        n=10; files=[]
        i=0
        while i<len(args):
            if args[i]=="-n" and i+1<len(args): n=int(args[i+1]); i+=2
            else: files.append(args[i]); i+=1
        for f in files:
            try: print("\n".join(self.sos.read(self._resolve(f)).splitlines()[:n]))
            except Exception as e: self._err(str(e))

    def _tail(self, args):
        """Tail.

            Args:
            args: Args.
            """
        n=10; files=[]
        i=0
        while i<len(args):
            if args[i]=="-n" and i+1<len(args): n=int(args[i+1]); i+=2
            else: files.append(args[i]); i+=1
        for f in files:
            try: print("\n".join(self.sos.read(self._resolve(f)).splitlines()[-n:]))
            except Exception as e: self._err(str(e))

    # ════ SOS EXTRAS ══════════════════════════════════════════════════════════
    def _sos(self, args):
        """Sos.

            Args:
            args: Args.
            """
        sub = args[0] if args else "stats"
        if sub == "stats":     print(self.sos.df())
        elif sub == "history" and len(args)>1:
            chain = self.sos.history(self._resolve(args[1]))
            for obj in chain:
                print(f"  v{obj.version}  {col(obj.oid[:12],'dim')}  {obj.size}B  {time.strftime('%Y-%m-%d %H:%M',time.localtime(obj.created_at))}")
        elif sub == "info" and len(args)>1:
            st = self.sos.stat(self._resolve(args[1]))
            for k,v in st.items(): print(f"  {k:<12} {v}")
        elif sub == "checkout" and len(args)>2:
            obj = self.sos.checkout(self._resolve(args[1]), int(args[2]))
            if obj: print(obj.text)
            else: self._err("Version not found")
        elif sub == "relate" and len(args)>2:
            self.sos.relate(self._resolve(args[1]), self._resolve(args[2]),
                            args[3] if len(args)>3 else "related")
            self._ok("Linked")
        elif sub == "related" and len(args)>1:
            for path, rel in self.sos.related(self._resolve(args[1])):
                print(f"  {col(rel,'dim'):<16} {col(path,'cyan')}")
        else:
            print("  sos stats|history <p>|info <p>|checkout <p> <v>|relate <p1> <p2> [rel]|related <p>")

    # ════ SYSTEM ═════════════════════════════════════════════════════════════
    def _ps(self, _):
        """Ps.

            Args:
            _:  .
            """
        print(f"  {'PID':>5}  {'USER':<10} STAT  {'CPU%':>5}  {'MEM%':>5}  COMMAND")
        for p in self.kernel.procs.all():
            sc = "green" if p.state=="R" else "dim"
            print(f"  {p.pid:>5}  {p.user:<10} {col(p.state,sc)}     {p.cpu:>5.1f}  {p.mem:>5.1f}  {p.cmd}")

    def _kill(self, args):
        """Kill.

            Args:
            args: Args.
            """
        if not args: self._err("kill: usage: kill <pid>"); return
        for a in args:
            try: self.kernel.procs.kill(int(a)); self._ok(f"[{a}] Terminated")
            except Exception as e: self._err(str(e))

    def _top(self, _):
        """Top.

            Args:
            _:  .
            """
        print("\033[2J\033[H", end="")
        procs = self.kernel.procs.all()
        mm    = self.kernel.memory.stats()
        print(col("  NOVA top", "cyan"))
        print(f"  Tasks: {len(procs)}  Mem: {mm['total_mb']}MB/{mm['used_mb']}MB used")
        print(f"\n  {'PID':>5}  {'USER':<8} {'CPU%':>5}  {'MEM%':>5}  COMMAND")
        for p in sorted(procs, key=lambda p: p.cpu, reverse=True):
            print(f"  {p.pid:>5}  {p.user:<8} {p.cpu:>5.1f}  {p.mem:>5.1f}  {p.cmd}")
        print(col("\n  (Ctrl+C to exit)", "dim"))
        try:
            while True: time.sleep(1)
        except KeyboardInterrupt: print()

    """Df.

        Args:
        _:  .
        """
    def _df(self, _):     print(self.sos.df())
    def _free(self, _):
        """Free.

            Args:
            _:  .
            """
        s = self.kernel.memory.stats()
        print(f"  Total: {s['total_mb']}MB  Used: {s['used_mb']}MB  Free: {s['free_mb']}MB")
    """Uname.

        Args:
        a: A.
        """
    def _uname(self, a):  print("PyOS NOVA 1.0 (Python PID 1)" if "-a" in a else "NOVA")
    """Whoami.

        Args:
        _:  .
        """
    def _whoami(self, _): print(self.user)
    """Date.

        Args:
        _:  .
        """
    def _date(self, _):   print(time.strftime("%a %b %d %H:%M:%S UTC %Y"))
    def _uptime(self, _):
        """Uptime.

            Args:
            _:  .
            """
        t=int(time.time()%86400); print(f" {time.strftime('%H:%M:%S')}  up {t//3600}:{(t%3600)//60:02d}")
    def _env(self, _):
        """Env.

            Args:
            _:  .
            """
        for k,v in sorted(self.env.items()): print(f"{k}={v}")
    def _export(self, args):
        """Export.

            Args:
            args: Args.
            """
        for a in args:
            if "=" in a: k,v=a.split("=",1); self.env[k]=v
    def _history(self, _):
        """History.

            Args:
            _:  .
            """
        for i,c in enumerate(self.history,1): print(f"  {i:4}  {c}")
    """Clear the operation.

        Args:
        _:  .
        """
    def _clear(self, _):  print("\033[2J\033[H", end="")
    """Exit cmd.

        Args:
        _:  .
        """
    def _exit_cmd(self, _):  self._exit = True
    """Reboot cmd.

        Args:
        _:  .
        """
    def _reboot_cmd(self, _): self._reboot = True; self._exit = True

    # ════ AI ═════════════════════════════════════════════════════════════════
    def _chat(self, args):
        """Chat.

            Args:
            args: Args.
            """
        if args:
            self._chat_h.append({"role":"user","content":" ".join(args)})
            print(col("\n  AI: ","cyan"), end="", flush=True)
            reply=""
            for ch in self.kernel.ai.chat(self._chat_h): print(ch,end="",flush=True); reply+=ch
            print("\n")
            self._chat_h.append({"role":"assistant","content":reply})
            return
        print(col("\n  NOVA Chat — 'exit' to leave, 'reset' to clear history","cyan"))
        print(col(f"  Model: {self.kernel.ai.model_name}","dim"))
        print()
        while True:
            try: inp = input(col("  You: ","blue"))
            except (EOFError, KeyboardInterrupt): break
            if inp.strip().lower() in ("exit","quit","/exit"): break
            if inp.strip().lower() in ("reset","/reset"):
                self._chat_h=[]; print(col("  Reset.","dim")); continue
            if not inp.strip(): continue
            self._chat_h.append({"role":"user","content":inp})
            print(col("  AI:  ","cyan"), end="", flush=True)
            reply=""
            for ch in self.kernel.ai.chat(self._chat_h): print(ch,end="",flush=True); reply+=ch
            print("\n")
            self._chat_h.append({"role":"assistant","content":reply})

    def _ask(self, args):
        """Ask.

            Args:
            args: Args.
            """
        if not args: self._err("ask: usage: ask <question>"); return
        print(col("\n  AI: ","cyan"), end="", flush=True)
        for ch in self.kernel.ai.complete(" ".join(args)): print(ch,end="",flush=True)
        print("\n")

    def _agent(self, args):
        """Agent.

            Args:
            args: Args.
            """
        if args: self.kernel.agent.ask(" ".join(args)); return
        print(col("\n  NOVA Agent — describe a task, 'exit' to leave","cyan"))
        while True:
            try: task = input(col("  Task: ","purple"))
            except (EOFError, KeyboardInterrupt): break
            if task.strip().lower() in ("exit","quit"): break
            if task.strip(): self.kernel.agent.ask(task)
        print()

    def _advice(self, _):
        """Advice.

            Args:
            _:  .
            """
        items = self.kernel.advisor.drain()
        if not items: print(col("  No pending advice. Advisor runs every 5 min.","dim")); return
        for item in items: print(str(item))

    def _write(self, args):
        """Write the operation.

            Args:
            args: Args.
            """
        if not args: self._err("write: usage: write <description> [--run]"); return
        run  = "--run" in args
        desc = " ".join(a for a in args if a != "--run")
        self.kernel.writer.write(desc, run=run)

    def _llm(self, args):
        """Llm.

            Args:
            args: Args.
            """
        sub = args[0] if args else "status"
        if sub == "status":
            st = self.kernel.ai.status()
            print(col("\n  AI Engine status:","cyan"))
            for k,v in st.items(): print(f"  {k:<12} {v}")
        elif sub == "download":
            model = args[1] if len(args)>1 else "tinyllama"
            ok = self.kernel.ai.download_model(model)
            print(col(f"  {model} ready","green") if ok else col("  Download failed","red"))
        elif sub == "tier":
            print(f"  Active tier: {self.kernel.ai.tier}")
        else:
            print("  Usage: llm status|download [model]|tier")

    # ════ SEARCH + TAGS ══════════════════════════════════════════════════════
    def _search(self, args):
        """Search.

            Args:
            args: Args.
            """
        if not args: self._err("search: usage: search <query>"); return
        q = " ".join(args)
        results = self.kernel.search.search(q, n=10)
        if not results: print(col(f"  No results for: {q}","dim")); return
        st = self.kernel.search.stats()
        mode = "vector" if st["hnsw_nodes"]>0 else "keyword"
        print(col(f"\n  Results ({mode}): '{q}'\n","cyan"))
        for i,r in enumerate(results,1):
            sc = r.get("score",0)
            cc = "green" if sc>.7 else "yellow" if sc>.4 else "dim"
            print(f"  {col(str(i),'dim')}  {col(r['path'],'cyan')}")
            print(f"     {col(f'{sc:.2f}',cc)}  —  {col(r['snippet'],'dim')}")
        print()

    def _tag(self, args):
        """Tag.

            Args:
            args: Args.
            """
        if len(args)<2: self._err("tag: usage: tag <path> <tag1> [tag2...]"); return
        rp = self._resolve(args[0])
        if not self.sos.exists(rp): self._err(f"tag: {args[0]}: not found"); return
        added = self.sos.tag(rp, *args[1:])
        if added: self._ok(f"Tagged: {' '.join('#'+t for t in added)}")
        else: print(col("  No new tags.","dim"))

    def _untag(self, args):
        """Untag.

            Args:
            args: Args.
            """
        if len(args)<2: self._err("untag: usage: untag <path> <tag1>..."); return
        removed = self.sos.untag(self._resolve(args[0]), *args[1:])
        if removed: self._ok(f"Removed: {', '.join(removed)}")

    def _tags(self, args):
        """Tags.

            Args:
            args: Args.
            """
        if args:
            rp   = self._resolve(args[0])
            tags = self.sos.get_tags(rp)
            if tags: print(f"  {col(rp,'cyan')}: {' '.join(col('#'+t,'purple') for t in tags)}")
            else: print(col(f"  No tags on {args[0]}","dim"))
        else:
            all_t = self.sos.all_tags()
            if not all_t: print(col("  No tags yet.","dim")); return
            print(col("\n  All tags:","cyan"))
            for e in all_t:
                bar = "█" * min(e["count"],20)
                print(f"  {col('#'+e['tag'],'purple'):<30} {bar} {col(str(e['count']),'dim')}")
            print()

    def _findtag(self, args):
        """Findtag.

            Args:
            args: Args.
            """
        if not args: self._err("findtag: usage: findtag <tag>"); return
        tag  = " ".join(args)
        hits = self.sos.find_by_tag(tag)
        if not hits: print(col(f"  No objects tagged #{tag}","dim")); return
        print(col(f"\n  Objects tagged #{tag}:","cyan"))
        for p in hits:
            tgs = " ".join(col(f"#{t}","purple") for t in self.sos.get_tags(p))
            print(f"  {col(p,'cyan')}  {tgs}")
        print()

    # ════ PACKAGES ═══════════════════════════════════════════════════════════
    def _pip(self, args):
        """Pip.

            Args:
            args: Args.
            """
        if not args: self._err("pip: usage: pip install|list|uninstall <pkg>"); return
        sub = args[0]; pkgs = args[1:]
        if sub == "install":
            for pkg in pkgs:
                print(col(f"  Installing {pkg}...","dim"))
                r = subprocess.run([sys.executable,"-m","pip","install",pkg],
                                   capture_output=False, text=True)
                if r.returncode==0: self._ok(f"{pkg} installed")
                else: self._err(f"Failed to install {pkg}")
        elif sub in ("list","freeze"):
            subprocess.run([sys.executable,"-m","pip",sub])
        elif sub == "uninstall":
            for pkg in pkgs: subprocess.run([sys.executable,"-m","pip","uninstall",pkg,"-y"])
        else:
            subprocess.run([sys.executable,"-m","pip"]+args)

    # ════ PYTHON ═════════════════════════════════════════════════════════════
    def _python3(self, args):
        """Python3.

            Args:
            args: Args.
            """
        if not args:
            import code
            print(col("  Python 3 REPL — exit() to leave","cyan"))
            code.interact(local={"kernel":self.kernel,"sos":self.sos})
            return
        rp = self._resolve(args[0])
        try: content = self.sos.read(rp)
        except: self._err(f"python3: {args[0]}: not found"); return
        pid = self.kernel.procs.spawn("python3", user=self.user, cmd=f"python3 {args[0]}", state="R")
        print(col(f"  [PID {pid}] python3 {args[0]}","dim"))
        try:
            exec(compile(content, args[0], "exec"), {"__name__":"__main__","kernel":self.kernel,"sos":self.sos})
        except SystemExit: pass
        except Exception as e: self._err(f"python3: {e}")
        finally:
            try: self.kernel.procs.kill(pid)
            except: pass
        print(col(f"  [PID {pid}] exited","dim"))

    # ════ MISC ═══════════════════════════════════════════════════════════════
    def _calc(self, args):
        """Calc.

            Args:
            args: Args.
            """
        if not args: self._err("calc: usage: calc <expr>"); return
        import ast, operator as op
        ops = {ast.Add:op.add,ast.Sub:op.sub,ast.Mult:op.mul,ast.Div:op.truediv,
               ast.Pow:op.pow,ast.Mod:op.mod,ast.UAdd:op.pos,ast.USub:op.neg}
        def ev(n):
            """Ev.

                Args:
                n: N.
                """
            if isinstance(n, ast.Num): return n.n
            if isinstance(n, ast.BinOp): return ops[type(n.op)](ev(n.left),ev(n.right))
            if isinstance(n, ast.UnaryOp): return ops[type(n.op)](ev(n.operand))
            raise TypeError
        try:
            r = ev(ast.parse(" ".join(args),mode="eval").body)
            print(f"  {' '.join(args)} = {col(str(r),'green')}")
        except: self._err("calc: invalid expression")

    def _cowsay(self, args):
        """Cowsay.

            Args:
            args: Args.
            """
        msg=" ".join(args) if args else "Moo!"; d="-"*(len(msg)+2)
        print(f" {d}\n< {msg} >\n {d}\n        \\   ^__^\n         \\  (oo)\\_______\n            (__)\\       )\\/\\\n                ||----w |\n                ||     ||")

    def _man(self, args):
        """Man.

            Args:
            args: Args.
            """
        if not args: self._err("man: what page?"); return
        pages = {
            "sos":    "sos — Semantic Object Store commands\n\n  sos stats       — object graph statistics\n  sos info <p>    — object metadata\n  sos history <p> — version history\n  sos checkout <p> <v> — retrieve version v\n  sos relate <p1> <p2> [rel] — semantic link\n  sos related <p> — show links",
            "chat":   "chat — AI conversation\n\n  chat           — interactive mode\n  chat <message> — single message",
            "agent":  "agent — AI task executor with confirmation\n\n  agent           — interactive mode\n  agent <task>    — single task",
            "write":  "write — AI Python script generator\n\n  write <description> [--run]",
            "llm":    "llm — manage the AI engine\n\n  llm status         — show active tier\n  llm download [model] — download GGUF model\n  llm tier           — show backend tier",
            "search": "search — semantic object search\n\n  search <query>   — find objects by meaning",
        }
        pg = pages.get(args[0])
        if pg:
            print(col(f"\n  MAN({args[0].upper()})\n","yellow"))
            for l in pg.splitlines(): print(f"  {l}")
            print()
        else: self._err(f"No man page for: {args[0]}")

    def _syscalls(self, _):
        """Syscalls.

            Args:
            _:  .
            """
        for name in ["sys_read","sys_write","sys_exec","sys_fork","sys_kill","sys_alloc"]:
            print(f"  {col(name,'green'):<20} kernel.{name}()")

    def _help(self, _):
        """Help.

            Args:
            _:  .
            """
        sections = [
            ("Data (primary)", "data put get up rm find link lock grant"),
            ("Legacy paths",   "ls cat mkdir rm sos tag search"),
            ("AI",             "chat ask agent advice write llm"),
            ("Security",       "cap zk audit crypto"),
            ("System",         "ps top uname help exit"),
        ]
        print(col("\n  DataPy.os — data + Python + AI\n", "cyan"))
        print(col("  No classical folders. Handles are flat labels; collections are tags.\n", "dim"))
        for name, cmds in sections:
            print(col(f"  {name}:", "yellow"))
            print("    " + "  ".join(col(c, "green") for c in cmds.split()))
            print()
        print(col("  Try:  data put note 'hello'  |  data find tag=docs  |  data lock on", "dim"))
        print()

    def _data_cmd(self, args):
        """data put|get|up|rm|find|link|lock|grant — folder-free secure CRUD."""
        from store.dataplane import DataPlaneError

        dp = self.kernel.data
        sub = args[0] if args else "find"
        rest = args[1:]

        try:
            if sub in ("put", "create", "write"):
                if len(rest) < 2:
                    print("  Usage: data put <handle> <content> [--kind text] [--tag t]")
                    return
                handle = rest[0]
                tags, kind, parts = [], "text", []
                i = 1
                while i < len(rest):
                    if rest[i] == "--kind" and i + 1 < len(rest):
                        kind = rest[i + 1]; i += 2
                    elif rest[i] == "--tag" and i + 1 < len(rest):
                        tags.append(rest[i + 1]); i += 2
                    else:
                        parts.append(rest[i]); i += 1
                content = " ".join(parts)
                rec = dp.put(handle, content, kind=kind, tags=tags or None)
                self._ok(f"put {rec.handle}  oid={rec.oid[:12]}…  v{rec.version}")

            elif sub in ("get", "cat", "read"):
                if not rest:
                    print("  Usage: data get <handle|oid>")
                    return
                rec = dp.get(rest[0])
                print(col(f"  @{rec.handle}  {rec.kind}  v{rec.version}  "
                          f"{rec.size}B  tags={rec.tags}", "dim"))
                print(rec.content)

            elif sub in ("up", "update"):
                if len(rest) < 2:
                    print("  Usage: data up <handle> <content>")
                    return
                rec = dp.update(rest[0], " ".join(rest[1:]))
                self._ok(f"updated {rec.handle}  v{rec.version}  oid={rec.oid[:12]}…")

            elif sub in ("rm", "delete", "del"):
                if not rest:
                    print("  Usage: data rm <handle>")
                    return
                oid = dp.delete(rest[0])
                self._ok(f"removed {rest[0]}  (oid {oid[:12]}… retained in history)")

            elif sub in ("find", "list", "ls"):
                tag = kind = query = None
                for a in rest:
                    if a.startswith("tag="):
                        tag = a[4:]
                    elif a.startswith("kind="):
                        kind = a[5:]
                    elif a.startswith("q="):
                        query = a[2:]
                    elif a and not a.startswith("-"):
                        query = a
                rows = dp.find(tag=tag, kind=kind, query=query)
                if not rows:
                    print(col("  (no objects)", "dim")); return
                for r in rows:
                    print(f"  {col('@'+r['handle'],'cyan'):<28} "
                          f"{r['kind']:<8} v{r['version']}  "
                          f"{','.join(r['tags'][:4])}")

            elif sub == "link":
                if len(rest) < 2:
                    print("  Usage: data link <src> <dst> [relation]")
                    return
                rel = rest[2] if len(rest) > 2 else "ref"
                dp.link(rest[0], rest[1], rel)
                self._ok(f"linked {rest[0]} -[{rel}]-> {rest[1]}")

            elif sub == "related":
                if not rest:
                    print("  Usage: data related <handle>")
                    return
                for path, rel in dp.related(rest[0]):
                    print(f"  {rel}: {path}")

            elif sub == "lock":
                mode = (rest[0] if rest else "status").lower()
                if mode in ("on", "1", "true"):
                    if dp._token is None:
                        token = dp.bootstrap_admin(self.user)
                        self._ok("capability lockdown ON (admin token issued)")
                        print(col(f"  admin-token={token}", "yellow"))
                    else:
                        dp.lockdown(True)
                        self._ok("capability lockdown ON")
                elif mode in ("off", "0", "false"):
                    dp.lockdown(False); self._ok("capability lockdown OFF")
                else:
                    print(f"  lockdown={'ON' if dp.enforce else 'OFF'}  "
                          f"token={'set' if dp._token else 'none'}")

            elif sub == "grant":
                if not rest:
                    print("  Usage: data grant <handle> [read,write,delete]")
                    return
                handle = rest[0]
                rights = set((rest[1] if len(rest) > 1 else "read,write,delete").split(","))
                alias = dp.alias_of(handle)
                cap = self.kernel.caps.grant(alias, rights, owner=self.user)
                dp.use_token(cap.token, actor=self.user)
                self._ok(f"granted {sorted(rights)} on @{handle}")
                print(col(f"  token={cap.token[:16]}…", "dim"))

            elif sub == "token":
                if rest:
                    dp.use_token(rest[0], actor=self.user)
                    self._ok("token set")
                else:
                    print(f"  token={dp._token[:16]+'…' if dp._token else 'none'}")

            else:
                print("  Usage: data put|get|up|rm|find|link|related|lock|grant|token")
        except DataPlaneError as exc:
            self._err(str(exc))
            raise
        except Exception as exc:
            self._err(f"data: {exc}")
            raise

    def _dashboard(self, args):
        """Dashboard.

            Args:
            args: Args.
            """
        from apps.dashboard import run_dashboard
        run_dashboard(kernel=self.kernel)

    def _mux(self, args):
        """Mux.

            Args:
            args: Args.
            """
        from apps.mux import run_mux
        run_mux(kernel=self.kernel)

    def _review(self, args):
        """Review the operation and return findings.

            Args:
            args: Args.
            """
        if not args: self._err("review: usage: review <path>"); return
        rp = self._resolve(args[0])
        result = self.kernel.reviewer.review_file(rp, sos=self.sos)
        if "error" in result: self._err(result["error"]); return
        print(self.kernel.reviewer.format_report(result))

    def _memory_cmd(self, args):
        """Memory cmd.

            Args:
            args: Args.
            """
        sub = args[0] if args else "list"
        mem = self.kernel.mem
        if sub == "list":
            mems = mem.all()
            if not mems: print(col("  No memories yet.","dim")); return
            for m in mems[:20]:
                print(f"  {col(m.mid,'dim')} [{m.kind}] {m.text[:80]}")
        elif sub == "add" and len(args)>1:
            m = mem.add(" ".join(args[1:]))
            self._ok(f"Memory added [{m.mid}]")
        elif sub == "forget" and len(args)>1:
            mem.forget(args[1]); self._ok(f"Forgotten: {args[1]}")
        elif sub == "search" and len(args)>1:
            results = mem.search(" ".join(args[1:]))
            for m in results: print(f"  {col(m.mid,'dim')} {m.text[:80]}")
        elif sub == "clear":
            mem.clear(); self._ok("All memories cleared")
        elif sub == "stats":
            s = mem.stats(); print(f"  Total: {s['total']}  By kind: {s['by_kind']}")
        else:
            print("  Usage: memory list|add <text>|forget <id>|search <q>|clear|stats")

    def _agents_cmd(self, args):
        """Agents cmd.

            Args:
            args: Args.
            """
        sub = args[0] if args else "status"
        ag  = self.kernel.agents
        if sub == "status":
            for s in ag.status_all():
                st = col("running","green") if s["running"] else col("stopped","dim")
                print(f"  {col(s['name'],'cyan'):<14} {st:<20} {s['role']}")
        elif sub == "send" and len(args)>=3:
            to,subj = args[1], args[2]
            body    = " ".join(args[3:]) if len(args)>3 else ""
            print(ag.send(to, subj, body))
        elif sub == "task" and len(args)>=2:
            taskmaster = ag.get("taskmaster")
            if taskmaster:
                sub2 = args[1]
                if sub2 == "add" and len(args)>2:
                    t = taskmaster.add_task(" ".join(args[2:]))
                    self._ok(f"Task added [{t['id']}]: {t['text']}")
                elif sub2 == "list":
                    for t in taskmaster.pending_tasks():
                        print(f"  [{t['id']}] {t['text']}")
                elif sub2 == "done" and len(args)>2:
                    taskmaster.complete_task(args[2]); self._ok("Task done")
        else:
            print("  Usage: agents status | agents send <to> <subject> [body] | agents task add|list|done")

    def _net_cmd(self, args):
        """Net cmd.

            Args:
            args: Args.
            """
        sub  = args[0] if args else "help"
        if sub == "api":
            port = int(args[1]) if len(args)>1 else 8080
            try:
                url = self.kernel.api_server.start()
                self._ok(f"REST API listening: {url}")
            except Exception as e: self._err(str(e))
        elif sub == "serve":
            port = int(args[1]) if len(args)>1 else 8081
            try:
                url = self.kernel.file_server.start()
                self._ok(f"File server: {url}")
            except Exception as e: self._err(str(e))
        elif sub == "sync" and len(args)>1:
            url = args[1]
            if not url.startswith("http"): url = f"http://{url}"
            prefix = args[2] if len(args)>2 else "/"
            print(col(f"  Syncing to {url}...","dim"))
            result = self.kernel.sync.sync_to(url, prefix)
            self._ok(f"Pushed {result['pushed']}  Skipped {result['skipped']}  Errors {result['errors']}")
        elif sub == "discover":
            print(col("  Scanning for NOVA instances...","dim"))
            peers = self.kernel.discovery.query()
            if not peers: print(col("  No NOVA instances found on LAN.","dim")); return
            for p in peers:
                print(f"  {col(p['ip'],'cyan')}  {p.get('hostname','?')}  port:{p.get('api_port','?')}")
        else:
            print("  Usage: net api [port] | net serve [port] | net sync <url> | net discover")

    def _crypto_cmd(self, args):
        """Crypto cmd.

            Args:
            args: Args.
            """
        sub = args[0] if args else "status"
        c   = self.kernel.crypto
        if sub == "init":
            import getpass
            pw1 = getpass.getpass("  New password: ")
            pw2 = getpass.getpass("  Confirm    : ")
            if pw1 != pw2: self._err("Passwords don't match"); return
            if c.init(pw1):
                self._ok("Encryption initialised (AES-256-GCM)")
                c.patch_sos()
            else:
                self._err("Init failed")
        elif sub == "unlock":
            import getpass
            pw = getpass.getpass("  Password: ")
            if c.unlock(pw):
                self._ok("Unlocked")
                c.patch_sos()
            else:
                self._err("Wrong password")
        elif sub == "lock":
            c.lock(); self._ok("Locked")
        elif sub == "status":
            s = c.status()
            for k,v in s.items(): print(f"  {k:<16} {v}")
        else:
            print("  Usage: crypto init | crypto unlock | crypto lock | crypto status")

    def _branch_cmd(self, args):
        """Create or manage cmd.

            Args:
            args: Args.
            """
        bm = self.kernel.branches
        if not args or args[0] == "list":
            cur = bm.current_branch()
            for b in bm.list_branches():
                mark = col(" *","green") if b.name==cur else "  "
                print(f"{mark} {col(b['name'] if isinstance(b,dict) else b.name,'cyan')}")
        elif args[0] == "create" and len(args)>1:
            b = bm.create_branch(args[1])
            self._ok(f"Branch '{b.name}' created")
        elif args[0] == "checkout" and len(args)>1:
            bm.checkout(args[1]); self._ok(f"Switched to {args[1]}")
        elif args[0] == "merge" and len(args)>1:
            r = bm.merge(args[1]); self._ok(f"Merge: {r}")
        elif args[0] == "diff" and len(args)>1:
            rp = self._resolve(args[1])
            v1 = int(args[2]) if len(args)>2 else None
            v2 = int(args[3]) if len(args)>3 else None
            print(bm.diff(rp, v1, v2))
        else:
            print("  Usage: branch [list|create <n>|checkout <n>|merge <n>|diff <p> [v1] [v2]]")

    def _audit_cmd(self, args):
        """Audit cmd.

            Args:
            args: Args.
            """
        mode = args[0] if args else "quick"
        print(col(f"  Running {mode} security audit...","cyan"))
        self.kernel.auditor.audit(mode)
        print(self.kernel.auditor.report())

    def _app_cmd(self, args):
        """App cmd.

            Args:
            args: Args.
            """
        pm  = self.kernel.plugins
        sub = args[0] if args else "list"
        if sub == "list":
            apps = pm.installed()
            if not apps: print(col("  No apps installed. Try: app install calculator","dim")); return
            for a in apps:
                cmds = ", ".join(a.commands)
                print(f"  {col(a.name,'cyan'):<20} v{a.version}  [{cmds}]  {a.description}")
        elif sub == "install" and len(args)>1:
            name = args[1]
            url  = args[2] if len(args)>2 else None
            print(col(f"  Installing {name}...","dim"))
            if pm.install(name, url): self._ok(f"{name} installed")
            else: self._err(f"Failed to install {name}")
        elif sub == "remove" and len(args)>1:
            if pm.remove(args[1]): self._ok(f"Removed {args[1]}")
            else: self._err(f"App not found: {args[1]}")
        elif sub == "run" and len(args)>1:
            pm.run(args[1], args[2:])
        elif sub == "search" and len(args)>1:
            results = pm.search(" ".join(args[1:]))
            for r in results:
                st = col("[installed]","green") if r["installed"] else ""
                print(f"  {col(r['name'],'cyan'):<20} {r['description']} {st}")
        elif sub == "info" and len(args)>1:
            m = pm.get_manifest(args[1])
            if m: [print(f"  {k}: {v}") for k,v in m.to_dict().items()]
            else: self._err(f"App not found: {args[1]}")
        else:
            print("  Usage: app list | app install <n> | app remove <n> | app run <n> | app search <q>")

    def _sql_cmd(self, args):
        """Sql cmd.

            Args:
            args: Args.
            """
        if not args: self._err("sql: usage: sql <query>"); return
        query = " ".join(args)
        try:
            conn = self.kernel.sos._pool.get()
            rows = conn.execute(query).fetchall()
            if not rows: print(col("  (no rows)","dim")); return
            cols = rows[0].keys()
            print("  " + "  ".join(f"{c:<16}" for c in cols))
            print("  " + "─"*max(60, 16*len(cols)))
            for row in rows[:50]:
                print("  " + "  ".join(f"{str(row[c]):<16}" for c in cols))
            if len(rows)>50: print(col(f"  ... {len(rows)-50} more rows","dim"))
        except Exception as e: self._err(str(e))

    def _alias_cmd(self, args):
        """Alias cmd.

            Args:
            args: Args.
            """
        sc = self.kernel.scripting
        if not args:
            for name, cmd in sc.aliases.items():
                print(f"  alias {name} = {cmd}")
        elif len(args)>=3 and args[1] == "=":
            name = args[0]; cmd = " ".join(args[2:])
            sc.add_alias(name, cmd); self._ok(f"Alias set: {name} = {cmd}")
        else:
            print("  Usage: alias  |  alias <name> = <command>")

    def _cron_cmd(self, args):
        """Cron cmd.

            Args:
            args: Args.
            """
        sc = self.kernel.scripting
        if not args or args[0] == "list":
            if not sc.cron: print(col("  No cron tasks.","dim")); return
            for t in sc.cron:
                st = col("enabled","green") if t.enabled else col("disabled","dim")
                print(f"  {t.name:<20} every {t.interval_seconds}s  {st}  cmd: {t.command}")
        elif args[0] == "add" and len(args)>=3:
            sc.add_cron(args[1], " ".join(args[2:]))
            self._ok(f"Cron added: every {args[1]}: {' '.join(args[2:])}")
        else:
            print("  Usage: cron [list] | cron add <interval> <command>")

    def _voice_cmd(self, args):
        """Voice cmd.

            Args:
            args: Args.
            """
        sub = args[0] if args else "status"
        from apps.voice import VoiceInterface
        if not hasattr(self, "_voice"):
            self._voice = VoiceInterface(self.kernel)
            self._voice.set_command_callback(lambda cmd: self._exec_line(cmd))
        v = self._voice
        if sub == "start":
            if v.start(): self._ok("Voice interface started. Say 'Nova' to activate.")
            else: self._err("Voice interface failed to start (see above)")
        elif sub == "stop":
            v.stop(); self._ok("Voice stopped")
        elif sub == "say" and len(args)>1:
            v.say(" ".join(args[1:]))
        elif sub == "test":
            result = v.test()
            for k,val in result.items(): print(f"  {k:<20} {val}")
        elif sub == "status":
            for k,val in v.status.items(): print(f"  {k:<20} {val}")
        else:
            print("  Usage: voice start|stop|say <text>|test|status")

    def _fix_cmd(self, args):
        """fix "<problem>" | fix list | fix show <id> | fix rollback <id> | fix simulate <id>"""
        doc = self.kernel.doctor
        sub = args[0] if args else "help"

        if sub == "list":
            fixes = doc.list_fixes()
            if not fixes:
                print(col("  No fix history.","dim"))
                return
            print(col("\n  Fix history:", "cyan"))
            print(f"  {'ID':<32} {'Status':<14} {'Risk':<10} Query")
            print("  " + "─"*80)
            for p in fixes:
                st_col = {"applied":"green","rolled_back":"dim","failed":"red",
                          "rejected":"dim","pending":"yellow"}.get(p.status,"dim")
                ts = col(p.fix_id,"dim")
                st = col(p.status, st_col)
                rc = {"low":"green","medium":"yellow","high":"red","critical":"red"}.get(p.overall_risk,"dim")
                risk = col(p.overall_risk, rc)
                q = p.query[:40]
                print(f"  {p.fix_id:<32} {st:<22} {risk:<18} {q}")
            print()

        elif sub == "show" and len(args) > 1:
            p = doc.ledger.load(args[1])
            if not p:
                self._err(f"Fix not found: {args[1]}")
                return
            from system.doctor import _print_proposal
            _print_proposal(p, self.user)
            print(f"  Status     : {p.status}")
            if p.applied_at:
                import time as _t
                print(f"  Applied at : {_t.strftime('%Y-%m-%d %H:%M', _t.localtime(p.applied_at))}")
            if p.rolled_back_at:
                import time as _t
                print(f"  Rolled back: {_t.strftime('%Y-%m-%d %H:%M', _t.localtime(p.rolled_back_at))}")
            # Show step outputs
            for step in p.steps:
                if step.output:
                    print(f"\n  Step: {step.description}")
                    print(f"  Output: {step.output[:200]}")

        elif sub == "rollback" and len(args) > 1:
            doc.rollback(args[1], current_user=self.user, dry_run=False)

        elif sub == "simulate" and len(args) > 1:
            p = doc.ledger.load(args[1])
            if not p:
                self._err(f"Fix not found: {args[1]}")
                return
            doc.rollback(args[1], current_user=self.user, dry_run=True)

        elif sub == "clear":
            ans = input(col("  Clear all fix history? [y/N]: ","yellow")).strip().lower()
            if ans in ("y","yes"):
                for p in doc.list_fixes():
                    try: doc.sos.remove(doc.ledger._fix_path(p.fix_id))
                    except: pass
                self._ok("Fix history cleared")

        elif sub == "help" or (not args):
            print("  Usage:")
            fix_example = col("fix '<problem>'", "cyan")
            print(f"    {fix_example}   — analyse + propose fixes")
            print(f"    {col('fix list','cyan')}             — show fix history")
            print(f"    {col('fix show <id>','cyan')}        — show fix details + outputs")
            print(f"    {col('fix rollback <id>','cyan')}    — roll back an applied fix")
            print(f"    {col('fix simulate <id>','cyan')}    — dry-run a rollback")
            print(f"    {col('fix clear','cyan')}            — clear history")

        else:
            # Treat as a query: fix "my problem"
            query = " ".join(args)
            proposal = doc.analyse(query)
            approved = doc.present_and_approve(proposal, current_user=self.user)
            if approved:
                doc.apply(proposal, current_user=self.user)

    def _cap_cmd(self, args):
        """Manage capability-based access tokens."""
        caps = self.kernel.caps
        sub  = args[0] if args else "list"
        if sub == "grant" and len(args)>=3:
            rp     = self._resolve(args[1])
            rights = set(args[2].split(","))
            ttl    = float(args[3]) if len(args)>3 else None
            cap    = caps.grant(rp, rights, owner=self.user, ttl=ttl)
            self._ok(f"Capability granted [{cap.token[:16]}...]  rights: {cap.rights}")
        elif sub == "revoke" and len(args)>=2:
            if caps.revoke(args[1]): self._ok("Revoked")
            else: self._err("Token not found")
        elif sub == "show" and len(args)>=2:
            rp   = self._resolve(args[1])
            caps_ = caps.capabilities_for(rp)
            for c in caps_:
                exp = f"expires {c.expires_at:.0f}" if c.expires_at else "never expires"
                print(f"  {col(c.token[:16],'dim')}  {c.rights}  {exp}")
        elif sub == "list":
            for c in caps.list_all()[:20]:
                print(f"  {col(c.token[:16],'dim')}  {c.target_path:<30} {c.rights}")
        elif sub == "check" and len(args)>=3:
            ok = caps.check(args[1], args[2])
            print(f"  Access: {col('GRANTED','green') if ok else col('DENIED','red')}")
        else:
            print("  Usage: cap grant <path> <rights> [ttl] | cap revoke <token> | cap list | cap show <path> | cap check <token> <right>")

    def _zk_cmd(self, args):
        """Zero-knowledge authentication commands."""
        zk  = self.kernel.zk
        sub = args[0] if args else "status"
        if sub == "init" and len(args)>=2:
            import getpass
            secret = getpass.getpass("  Secret passphrase: ")
            zk.setup(args[1], secret)
            self._ok(f"ZK credential set up for {args[1]}")
        elif sub == "login" and len(args)>=2:
            import getpass
            secret = getpass.getpass("  Passphrase: ")
            if zk.verify_proof(args[1], secret):
                self._ok(f"ZK login successful for {args[1]}")
                self.user = args[1]
            else:
                self._err("Authentication failed")
        elif sub == "status":
            users = zk.list_users()
            for u in users: print(f"  {col(u,'cyan')}  ZK credentials configured")
            if not users: print(col("  No ZK credentials set up.","dim"))
        elif sub == "remove" and len(args)>=2:
            if zk.remove(args[1]): self._ok(f"Removed ZK credential for {args[1]}")
            else: self._err("User not found")
        else:
            print("  Usage: zk init <user> | zk login <user> | zk status | zk remove <user>")

    def _lineage_cmd(self, args):
        """Show data provenance and lineage."""
        lt  = self.kernel.lineage
        sub = args[0] if args else "help"
        if sub == "help" or not args:
            rp  = self._resolve(args[0]) if args else self.cwd
            print(lt.render_dag(rp))
        elif sub == "sources" and len(args)>1:
            rp = self._resolve(args[1])
            for r in lt.sources(rp):
                print(f"  ← {r.path}  [{r.writer_user} pid:{r.writer_pid}]")
        elif sub == "impacts" and len(args)>1:
            rp = self._resolve(args[1])
            for r in lt.impacts(rp):
                print(f"  → {r.path}  [{r.writer_user}]")
        elif sub == "who" and len(args)>1:
            rp = self._resolve(args[1])
            r  = lt.who_wrote(rp)
            if r: print(f"  {r.writer_user} (pid {r.writer_pid}) at {r.timestamp:.0f}")
            else: print(col("  No lineage recorded.","dim"))
        else:
            rp = self._resolve(args[0]) if args else self.cwd
            print(lt.render_dag(rp))

    def _classify_cmd(self, args):
        """Classify objects by data sensitivity."""
        cm  = self.kernel.classifier
        sub = args[0] if args else "stats"
        if sub == "stats":
            s = cm.stats()
            for level, count in s.items():
                bar = "█"*min(count, 40) if count else "·"
                print(f"  {col(level,'cyan'):<16} {count:>6}  {bar}")
        elif sub == "show" and len(args)>1:
            rp = self._resolve(args[1])
            lv = cm.get_level(rp)
            print(f"  {rp}  →  {col(lv, {'public':'green','internal':'yellow','confidential':'red','secret':'magenta'}.get(lv,'dim'))}")
        elif sub == "policy":
            print("  Classification levels:")
            print("  public       — safe to share")
            print("  internal     — org-internal only")
            print("  confidential — sensitive, encrypted")
            print("  secret       — highly sensitive, 2FA required")
        elif args:
            rp = self._resolve(args[0])
            r  = cm.classify_object(rp)
            print(f"  {rp}  →  {r.level}  (confidence {r.confidence:.0%})")
            for reason in r.reasons[:3]: print(f"    · {reason}")
        else:
            print("  Usage: classify [<path>] | classify stats | classify show <path> | classify policy")

    def _timelock_cmd(self, args):
        """Manage time-locked and expiring objects."""
        tl  = self.kernel.timelock
        sub = args[0] if args else "list"
        if sub == "set" and len(args)>=3:
            rp  = self._resolve(args[1])
            ts  = __import__("store.timelock",fromlist=["_parse_time"])._parse_time(args[2])
            tl.set_not_before(rp, ts)
            self._ok(f"Locked until {__import__('time').strftime('%Y-%m-%d %H:%M', __import__('time').localtime(ts))}")
        elif sub == "expire" and len(args)>=3:
            rp  = self._resolve(args[1])
            ts  = __import__("store.timelock",fromlist=["_parse_time"])._parse_time(args[2])
            tl.set_expires_at(rp, ts)
            self._ok(f"Will expire at {__import__('time').strftime('%Y-%m-%d %H:%M', __import__('time').localtime(ts))}")
        elif sub == "once" and len(args)>=2:
            rp = self._resolve(args[1])
            tl.set_read_once(rp); self._ok("Read-once set")
        elif sub == "unlock" and len(args)>=2:
            rp = self._resolve(args[1])
            tl.remove_lock(rp); self._ok("Lock removed")
        elif sub == "list":
            for meta in tl.list_all()[:20]:
                print(f"  {meta.get('path'):<35} {meta.get('kind')} "
                      f"{__import__('time').strftime('%Y-%m-%d', __import__('time').localtime(meta.get('not_before') or meta.get('expires_at') or 0))}")
        elif sub == "status" and len(args)>=2:
            rp = self._resolve(args[1])
            ok, reason = tl.is_accessible(rp)
            print(f"  {'accessible' if ok else col(reason,'red')}")
        else:
            print("  Usage: tl set <path> <time> | tl expire <path> <time> | tl once <path> | tl unlock <path> | tl list")

    def _privacy_cmd(self, args):
        """Differential privacy for SQL queries."""
        dp  = self.kernel.privacy
        sub = args[0] if args else "status"
        if sub == "status":
            s = dp.budget.status()
            for k,v in s.items(): print(f"  {k:<15} {v}")
        elif sub == "budget" and len(args)>1 and args[1] == "reset":
            dp.budget.reset(); self._ok("Privacy budget reset")
        elif sub == "query" and len(args)>1:
            sql = " ".join(args[1:])
            result, meta = dp.query(sql)
            print(f"  Result: {result}")
            print(f"  Noise : ±{meta.get('noise_magnitude',0):.2f}")
            print(f"  Budget: -{meta.get('epsilon',0):.2f} epsilon")
        else:
            print("  Usage: privacy status | privacy budget reset | privacy query <sql>")
            print("  Or:    sql --private <query>")

    def _nl_find_cmd(self, args):
        """Find SOS objects using natural language."""
        nle = self.kernel.nlquery
        if not args:
            print("  Usage: find <natural language query>")
            print("  Examples:")
            print("    find all Python files modified this week")
            print("    find large files over 1MB")
            print("    find files mentioning authentication")
            return
        explain = args[0] == "--explain"
        query   = " ".join(args[1:] if explain else args)
        results, plan = nle.execute(query, explain=explain)
        print(nle.format_results(results, plan))

    def _prefetch_cmd(self, args):
        """Manage AI predictive cache prefetch."""
        pf  = self.kernel.prefetch
        sub = args[0] if args else "status"
        if sub == "status":
            s = pf.status()
            for k,v in s.items(): print(f"  {k:<20} {v}")
        elif sub == "warm" and len(args)>1:
            rp = self._resolve(args[1])
            pf.warm_cache(rp); self._ok(f"Cache warmed for {rp}")
        elif sub == "train":
            n = pf.train(); self._ok(f"Model trained on {n} transitions")
        elif sub == "clear":
            pf._transitions.clear(); pf._save(); self._ok("Model cleared")
        else:
            print("  Usage: prefetch status | prefetch warm <path> | prefetch train | prefetch clear")

    def _immutable_cmd(self, args):
        """Manage the immutable append-only partition."""
        im  = self.kernel.immutable
        sub = args[0] if args else "list"
        if sub == "write" and len(args)>=3:
            path    = args[1]
            content = " ".join(args[2:])
            try:
                oid = im.write(path, content)
                self._ok(f"Written to immutable partition [{oid[:12]}...]")
            except ValueError as e:
                self._err(str(e))
        elif sub == "root":
            print(f"  Merkle root: {col(im.merkle_root(),'cyan')}")
        elif sub == "verify":
            ok, msg = im.verify()
            print(f"  {col(msg,'green') if ok else col(msg,'red')}")
        elif sub == "proof" and len(args)>1:
            proof = im.inclusion_proof(args[1])
            if proof:
                print(f"  Leaf hash: {proof['leaf_hash'][:20]}...")
                print(f"  Root     : {proof['root'][:20]}...")
                print(f"  Proof    : {len(proof['proof'])} steps  seq {proof['seq']}/{proof['total']}")
                ok = im.verify_proof(proof)
                print(f"  Valid    : {col('yes','green') if ok else col('no','red')}")
            else:
                self._err("Path not found in immutable partition")
        elif sub == "list":
            for entry in im.list_objects()[:30]:
                ts = __import__("time").strftime("%Y-%m-%d", __import__("time").localtime(entry["ts"]))
                print(f"  {entry['path']:<40} {ts}  {entry['hash'][:12]}...")
        elif sub == "stats":
            s = im.stats()
            for k,v in s.items(): print(f"  {k:<15} {v}")
        else:
            print("  Usage: immutable write <path> <content> | immutable root | immutable verify | immutable proof <path> | immutable list")

    def _crdt_cmd(self, args):
        """CRDT distributed SOS synchronisation."""
        crdt = self.kernel.crdt
        sub  = args[0] if args else "status"
        if sub == "sync" and len(args)>1:
            url = args[1] if args[1].startswith("http") else f"http://{args[1]}"
            print(col(f"  Syncing with {url}...","dim"))
            result = crdt.sync_with_peer(url)
            self._ok(f"Received {result.get('received',0)}  Sent {result.get('sent',0)}  Conflicts {result.get('conflicts',0)}")
        elif sub == "peers":
            if not crdt._peers: print(col("  No peers.","dim"))
            for p in crdt._peers:
                print(f"  {col(p['url'],'cyan')}")
        elif sub == "add-peer" and len(args)>1:
            crdt.add_peer(args[1]); self._ok(f"Peer added: {args[1]}")
        elif sub == "status":
            s = crdt.status()
            for k,v in s.items(): print(f"  {k:<15} {v}")
        else:
            print("  Usage: crdt status | crdt sync <url> | crdt peers | crdt add-peer <url>")

    def _trust_cmd(self, args):
        """Process reputation and trust scoring."""
        rep = self.kernel.reputation
        sub = args[0] if args else "status"
        if sub == "status":
            recs = rep.all_scores()
            if not recs: print(col("  No processes tracked.","dim")); return
            print(f"  {'PID':<8} {'Name':<20} {'Score':>6} {'Level':<12} Restricted")
            for r in recs[:20]:
                lvl  = __import__("net.crdt",fromlist=["_trust_level"])._trust_level(r["score"])
                col_ = {"trusted":"green","normal":"cyan","suspicious":"yellow","restricted":"red"}.get(lvl,"dim")
                rst  = col("yes","red") if r.get("restricted") else "no"
                print(f"  {r['pid']:<8} {r['name']:<20} {r['score']:>6.0f} {col(lvl,col_):<20} {rst}")
        elif sub == "restrict" and len(args)>1:
            rep.restrict(int(args[1])); self._ok(f"Process {args[1]} restricted")
        elif sub == "whitelist" and len(args)>1:
            rep.whitelist(int(args[1])); self._ok(f"Process {args[1]} whitelisted")
        elif sub == "cleanup":
            rep.cleanup_dead_processes(); self._ok("Dead processes cleaned up")
        else:
            print("  Usage: trust status | trust restrict <pid> | trust whitelist <pid> | trust cleanup")

    def _sandbox_cmd(self, args):
        """Run code in isolated sandbox."""
        sb  = self.kernel.sandbox
        sub = args[0] if args else "help"
        if sub == "run" and len(args)>1:
            path_or_code = args[1]
            kw = {}
            if "--net" in args:    kw["allow_network"] = True
            if "--time" in args:
                idx = args.index("--time")
                kw["timeout"] = float(args[idx+1]) if idx+1 < len(args) else 30
            rp = self._resolve(path_or_code)
            if self.sos.exists(rp):
                result = sb.run(rp, **kw)
            else:
                result = sb.run(path_or_code, **kw)
            if result.stdout: print(result.stdout[:4096])
            if result.stderr: print(col(result.stderr[:1024],"red"))
            rc_col = "green" if result.success else "red"
            print(col(f"  Exit: {result.returncode}  Time: {result.duration:.2f}s  Mode: {'ns' if sb.namespace_available else 'exec'}",rc_col))
        elif sub == "status":
            print(f"  Namespace isolation: {col('available','green') if sb.namespace_available else col('exec fallback','yellow')}")
            for s in sb.list_all(): print(f"  [{s['id']}] {s['status']}")
        else:
            print("  Usage: sandbox run <path|code> [--net] [--time N] | sandbox status")

    def _reload_cmd(self, args):
        """Live kernel hot-reload without reboot."""
        hr  = self.kernel.hotreload
        sub = args[0] if args else "status"
        if sub == "status":
            for h in hr.history()[:10]:
                rb = col("[rolled back]","yellow") if h["rolled_back"] else ""
                print(f"  {h['module']:<30} {__import__('time').strftime('%H:%M:%S', __import__('time').localtime(h['ts']))} {rb}")
        elif sub == "undo":
            r = hr.rollback()
            print(col(f"  {r['message']}","green") if r["success"] else col(f"  {r['message']}","red"))
        elif sub == "--dry-run" and len(args)>1:
            r = hr.reload(args[1], dry_run=True)
            print(f"  {r['message']}")
        elif args and args[0] not in ("status","undo","--dry-run"):
            r = hr.reload(args[0])
            print(col(f"  {r['message']}","green") if r["success"] else col(f"  {r['message']}","red"))
        else:
            print("  Usage: reload <module> | reload --dry-run <module> | reload status | reload undo")

    def _waq_cmd(self, args):
        """Write-ahead batch queue management."""
        waq = getattr(self.kernel.sos, '_waq', None)
        sub = args[0] if args else "status"
        if not waq:
            self._err("WAQ not active. Call patch_sos_with_waq() first.")
            return
        if sub == "flush":
            n = waq.flush(force=True); self._ok(f"Flushed {n} items")
        elif sub == "status" or not args:
            s = waq.stats()
            for k,v in s.items(): print(f"  {k:<20} {v}")
        else:
            print("  Usage: waq status | waq flush")

    def _workers_cmd(self, args):
        """Multiprocessing worker pool management."""
        wm  = self.kernel.workers
        sub = args[0] if args else "status"
        if sub == "status":
            for s in wm.status_all():
                alive = col(str(s["alive"]),"green") if s["alive"] else col("0","red")
                print(f"  {s['name']:<12} {alive}/{s['workers']} alive  q:{s.get('queue_depth',-1)}")
        elif sub == "bench":
            print(col("  Benchmarking worker throughput...","dim"))
            import time as _t
            t = _t.perf_counter()
            for i in range(100):
                wm.async_write(f"/bench/w{i}", f"data {i}")
            elapsed = _t.perf_counter()-t
            self._ok(f"100 async writes queued in {elapsed*1000:.1f}ms")
        else:
            print("  Usage: workers status | workers bench")

    def _watch_cmd(self, args):
        """Stream live SOS events matching a pattern."""
        if not args:
            print("  Usage: watch <pattern>  e.g. watch /home/**  or  watch *.py")
            return
        pattern = args[0]
        bus = self.kernel.event_bus
        print(col(f"  Watching: {pattern}  (Ctrl+C to stop)","dim"))
        import threading, time as _t
        stop_evt = threading.Event()
        def _cb(event):
            ts = _t.strftime("%H:%M:%S", _t.localtime(event.timestamp))
            print(f"  {col(ts,'dim')} {col(event.event_type,'cyan'):<8} {event.path}  {event.detail or ''}")
        sub_id = bus.subscribe(pattern, _cb, owner="watch")
        try:
            while not stop_evt.is_set():
                _t.sleep(0.1)
        except KeyboardInterrupt:
            pass
        finally:
            bus.unsubscribe(sub_id)

    def _on_cmd(self, args):
        """Run a shell command when a pattern matches."""
        if len(args) < 2:
            print("  Usage: on <pattern> <command>")
            return
        pattern = args[0]; cmd = " ".join(args[1:])
        bus = self.kernel.event_bus
        def _cb(event):
            self._exec_line(cmd)
        sub_id = bus.subscribe(pattern, _cb, owner="on-trigger")
        self._ok(f"Trigger registered [{sub_id[:12]}]  pattern: {pattern}  cmd: {cmd}")

    def _events_cmd(self, args):
        """Manage event bus subscriptions."""
        bus = self.kernel.event_bus
        sub = args[0] if args else "list"
        if sub == "list":
            subs = bus.list_subs()
            if not subs: print(col("  No active subscriptions.","dim")); return
            for s in subs:
                print(f"  {col(s['id'],'dim')}  {s['pattern']:<30} hits:{s['hits']}  {s['owner']}")
        elif sub == "stats":
            s = bus.stats()
            for k,v in s.items(): print(f"  {k:<20} {v}")
        elif sub == "clear":
            for s in bus.list_subs(): bus.unsubscribe(s["id"])
            self._ok("All subscriptions cleared")
        else:
            print("  Usage: events list | events stats | events clear")

    def _compress_cmd(self, args):
        """Adaptive object compression management."""
        from store.compression import _stats, compress, decompress
        sub = args[0] if args else "status"
        if sub == "status":
            r = _stats.report()
            print(f"  Original:    {r['original_mb']} MB")
            print(f"  Compressed:  {r['compressed_mb']} MB")
            print(f"  Savings:     {col(str(r['savings_pct'])+'%', 'green' if r['savings_pct']>20 else 'yellow')}")
            print(f"  Calls:       {r['compress_calls']}")
        elif sub == "bench":
            import time as _t
            sample = b"Python code with functions and classes " * 200
            codecs = {}
            for name, fn in [("gzip", lambda d: __import__("gzip").compress(d)),
                              ("compress", lambda d: compress(d, "code")[1:])]:
                t = _t.perf_counter()
                for _ in range(100): fn(sample)
                ms = (_t.perf_counter()-t)*10
                ratio = len(fn(sample))/len(sample)
                codecs[name] = (ms, ratio)
                print(f"  {name:<12} {ms:.1f}ms/call  {ratio:.1%} of original")
        else:
            print("  Usage: compress status | compress bench")

    def _spec_cmd(self, args):
        """Speculative command pre-execution."""
        spec = self.kernel.speculative
        sub  = args[0] if args else "status"
        if sub == "status":
            s = spec.status()
            for k,v in s.items(): print(f"  {k:<15} {v}")
        elif sub == "enable":
            spec.enable(); self._ok("Speculative execution enabled")
        elif sub == "disable":
            spec.disable(); self._ok("Speculative execution disabled")
        else:
            print("  Usage: spec status | spec enable | spec disable")

    def _kvcache_cmd(self, args):
        """LLM KV attention cache management."""
        kv  = self.kernel.kvcache
        sub = args[0] if args else "status"
        if sub == "status":
            s = kv.status()
            for k,v in s.items(): print(f"  {k:<20} {v}")
        elif sub == "clear":
            kv.clear_all(); self._ok("KV cache cleared")
        else:
            print("  Usage: kvcache status | kvcache clear")

    def _replay_cmd(self, args):
        """Deterministic replay debugger."""
        rec    = self.kernel.recorder
        player = self.kernel.replayer
        sub    = args[0] if args else "status"
        if sub == "record":
            path = args[1] if len(args)>1 else None
            rec.start(path)
            self._ok(f"Recording to: {rec._path}")
        elif sub == "stop":
            path = rec.stop()
            self._ok(f"Saved: {path}  ({rec._seq} operations)")
        elif sub == "status":
            s = rec.status()
            for k,v in s.items(): print(f"  {k:<15} {v}")
        elif sub == "list":
            sessions = player.list_sessions()
            if not sessions: print(col("  No replay sessions.","dim")); return
            for s in sessions:
                print(f"  {s['file']:<40} {s['entries']:>5} ops  {s['size_kb']}KB")
        elif sub == "run" and len(args)>1:
            file_path = args[1]
            dry_run   = "--dry-run" in args
            result    = player.replay(file_path, dry_run=dry_run)
            for k,v in result.items(): print(f"  {k:<12} {v}")
        else:
            print("  Usage: replay record [path] | replay stop | replay status | replay list | replay run <file> [--dry-run]")

    def _wasm_cmd(self, args):
        """WebAssembly runtime."""
        wasm = self.kernel.wasm
        sub  = args[0] if args else "list"
        if sub == "run" and len(args)>1:
            path  = args[1]
            wargs = args[2:]
            result = wasm.run_path(path, wargs)
            if result.stdout: print(result.stdout[:4096])
            if result.stderr: print(col(result.stderr[:512],"red"))
            rc_col = "green" if result.success else "red"
            has_wt = col("wasmtime","green") if wasm.has_wasmtime else col("fallback","yellow")
            print(col(f"  Exit:{result.returncode}  {result.duration*1000:.1f}ms  [{has_wt}]",rc_col))
        elif sub == "install" and len(args)>=3:
            name, url = args[1], args[2]
            if wasm.install(name, url): self._ok(f"Installed: {name}")
            else: self._err(f"Install failed")
        elif sub == "info" and len(args)>1:
            info = wasm.info(args[1])
            for k,v in info.items(): print(f"  {k:<12} {v}")
        elif sub == "list":
            modules = wasm.list_modules()
            if not modules: print(col("  No WASM modules installed.","dim")); return
            for m in modules:
                print(f"  {col(m['name'],'cyan'):<30} {m['size_kb']}KB")
        elif sub == "runtime":
            engine = col("wasmtime","green") if wasm.has_wasmtime else col("pure-Python fallback","yellow")
            print(f"  Engine: {engine}")
            if not wasm.has_wasmtime:
                print(f"  Install: {col('pip install wasmtime','cyan')}")
        else:
            print("  Usage: wasm run <file.wasm> [args] | wasm install <n> <url> | wasm info <file> | wasm list | wasm runtime")

    def _federated_cmd(self, args):
        """Federated model learning."""
        fed = self.kernel.federated
        sub = args[0] if args else "status"
        if sub == "status":
            s = fed.status()
            for k,v in s.items(): print(f"  {k:<16} {v}")
        elif sub == "push":
            r = fed.push(); self._ok(f"Pushed {r['pushed']} models (round {r['round']})")
        elif sub == "pull":
            r = fed.pull()
            for model, n in r.items():
                if n: print(f"  Applied {n} delta(s) for {model}")
            self._ok("Pull complete")
        elif sub == "sync":
            r = fed.sync()
            self._ok(f"Pushed {r['push']['pushed']} models, pulled {sum(r['pull'].values())} deltas")
        elif sub == "reset":
            self._ok("Federated model reset (models will retrain locally)")
        else:
            print("  Usage: federated status | federated push | federated pull | federated sync | federated reset")

    def _bloom_cmd(self, args):
        """Bloom filter existence index."""
        bloom = self.kernel.bloom
        sub   = args[0] if args else "status"
        if sub == "status":
            s = bloom.stats()
            for k,v in s.items(): print(f"  {k:<22} {v}")
        elif sub == "rebuild":
            n = bloom.rebuild(); self._ok(f"Rebuilt with {n} paths")
        elif sub == "check" and len(args)>1:
            rp  = self._resolve(args[1])
            res = bloom.might_exist(rp)
            print(f"  {'might exist' if res else 'definitely not in SOS'}: {rp}")
        elif sub == "bench":
            import time as _t
            sos = self.kernel.sos
            t = _t.perf_counter()
            for _ in range(10000): sos.exists("/nonexistent/path")
            ms = (_t.perf_counter()-t)*1000
            self._ok(f"10000 exists() checks: {ms:.0f}ms ({10000000/ms:.0f}/sec)")
        else:
            print("  Usage: bloom status | bloom rebuild | bloom check <path> | bloom bench")

    def _schema_cmd(self, args):
        """SOS object schema validation."""
        sr  = self.kernel.schema_reg
        sub = args[0] if args else "list"
        if sub == "define" and len(args)>=3:
            name = args[1]
            try: schema = __import__("json").loads(" ".join(args[2:]))
            except: self._err("Invalid JSON"); return
            sr.define(name, schema); self._ok(f"Schema defined: {name}")
        elif sub == "validate" and len(args)>1:
            rp = self._resolve(args[1])
            ok, errs = sr.validate(rp)
            if ok: self._ok(f"Valid: {rp}")
            else:
                print(col(f"  Invalid: {rp}","red"))
                for e in errs[:5]: print(f"    {e}")
        elif sub == "attach" and len(args)>=3:
            rp = self._resolve(args[1]); sr.attach(rp, args[2])
            self._ok(f"Schema {args[2]} attached to {rp}")
        elif sub == "violations":
            for v in sr.violations()[:20]:
                print(f"  {v['path']:<40} {v['schema_uri']}")
                for e in v.get("errors",[])[:2]: print(f"    {e}")
        elif sub == "list":
            for s in sr.list_schemas(): print(f"  {s}")
        else:
            print("  Usage: schema define <n> <json> | schema validate <path> | schema attach <path> <n> | schema violations | schema list")

    def _stream_cmd(self, args):
        """Reactive stream processor."""
        mgr = self.kernel.stream_mgr
        sub = args[0] if args else "list"
        if sub == "list":
            streams = mgr.list_streams()
            if not streams: print(col("  No active streams.","dim")); return
            for s in streams:
                print(f"  {s['id']}  {s['pattern']:<30} {s['received']} rcvd  {s['throughput']}")
        elif sub == "stop" and len(args)>1:
            if mgr.stop(args[1]): self._ok(f"Stream {args[1]} stopped")
            else: self._err(f"Stream not found: {args[1]}")
        elif sub == "stats":
            s = {"streams": len(mgr.list_streams())}
            for k,v in s.items(): print(f"  {k:<20} {v}")
        elif args and args[0] not in ("list","stop","stats"):
            # Create a simple stream: stream <pattern> [| sink <path>]
            pattern = args[0]
            sink_path = None
            if "|" in " ".join(args):
                full = " ".join(args)
                if "sink" in full:
                    parts = full.split("sink")
                    sink_path = parts[-1].strip()
            builder = mgr.stream(pattern)
            if sink_path:
                s = builder.sink(sink_path)
                self._ok(f"Stream started [{s.stream_id}] → {sink_path}")
            else:
                s = builder.start()
                self._ok(f"Stream started [{s.stream_id}] pattern: {pattern}")
        else:
            print("  Usage: stream <pattern> [| sink <path>]  |  streams list|stop|stats")

    def _eventsource_cmd(self, args):
        """Event sourcing log and rewind."""
        es  = self.kernel.es_log
        sub = args[0] if args else "stats"
        if sub == "stats":
            s = es.stats()
            for k,v in s.items(): print(f"  {k:<20} {v}")
        elif sub == "log":
            events = es.events(n=20)
            for e in events:
                ts = __import__("time").strftime("%H:%M:%S",__import__("time").localtime(e.ts))
                print(f"  {ts}  {col(e.event_type,'cyan'):<8} {e.path}")
        elif sub == "rewind" and len(args)>1:
            from store.timelock import _parse_time
            ts  = _parse_time(args[1])
            n   = es.rewind(ts)
            self._ok(f"Rewound SOS to {args[1]} ({n} aliases restored)")
        elif sub == "snapshot":
            path = es.take_snapshot()
            self._ok(f"Snapshot saved: {path}")
        else:
            print("  Usage: es stats | es log | es rewind <time> | es snapshot")

    def _view_cmd(self, args):
        """Materialised SQL view cache."""
        vc  = self.kernel.views
        sub = args[0] if args else "list"
        if sub == "define" and len(args)>=3:
            vc.define(args[1], " ".join(args[2:]))
            self._ok(f"View defined: {args[1]}")
        elif sub == "query" and len(args)>1:
            result, stale = vc.query(args[1])
            fresh = col("(rebuilt)","yellow") if stale else col("(cached)","green")
            print(f"  {fresh}")
            if isinstance(result, list):
                for row in result[:20]: print(f"  {row}")
        elif sub == "refresh" and len(args)>1:
            vc.query(args[1], force_refresh=True); self._ok(f"Refreshed: {args[1]}")
        elif sub == "list":
            for v in vc.list_views():
                dirty = col("stale","yellow") if v["stale"] else col("fresh","green")
                print(f"  {v['name']:<25} {dirty}  hits:{v['hits']}")
        else:
            print("  Usage: view define <n> <sql> | view query <n> | view refresh <n> | view list")

    def _mvcc_cmd(self, args):
        """MVCC snapshot isolation."""
        mgr = self.kernel.mvcc
        sub = args[0] if args else "status"
        if sub == "status":
            print(f"  Active snapshots: {mgr.active_snapshots()}")
        elif sub == "begin":
            snap = mgr.begin()
            self._ok(f"Snapshot started [{snap.txn_id}] @ {snap.timestamp:.0f}")
        elif sub == "read" and len(args)>=3:
            # mvcc read <txn_id> <path>
            snap = mgr._active.get(args[1])
            if not snap: self._err(f"Snapshot not found: {args[1]}"); return
            rp  = self._resolve(args[2])
            try: print(snap.read(rp, self.sos)[:500])
            except Exception as e: self._err(str(e))
        elif sub == "commit" and len(args)>1:
            if mgr.commit(args[1]): self._ok(f"Snapshot {args[1]} committed")
            else: self._err("Snapshot not found")
        else:
            print("  Usage: mvcc status | mvcc begin | mvcc read <txn> <path> | mvcc commit <txn>")

    def _circuit_cmd(self, args):
        """Circuit breaker management."""
        reg = self.kernel.circuits
        sub = args[0] if args else "list"
        if sub == "list":
            for c in reg.list_all():
                sc = {"closed":"green","open":"red","half_open":"yellow"}.get(c["state"],"dim")
                print(f"  {c['name']:<25} {col(c['state'],sc)}  failures:{c['failures']}")
        elif sub == "reset" and len(args)>1:
            if reg.reset(args[1]): self._ok(f"Circuit {args[1]} reset")
            else: self._err(f"Circuit not found: {args[1]}")
        else:
            print("  Usage: circuit list | circuit reset <name>")

    def _ratelimit_cmd(self, args):
        """REST API rate limiter."""
        rl  = self.kernel.rate_limiter
        sub = args[0] if args else "status"
        if sub == "status":
            s = rl.status()
            for k,v in s.items(): print(f"  {k:<20} {v}")
        elif sub == "set" and len(args)>=3:
            rl.set_limit(args[1], float(args[2]))
            self._ok(f"Rate limit set: {args[1]} → {args[2]} rps")
        else:
            print("  Usage: ratelimit status | ratelimit set <ip> <rps>")

    def _trace_cmd(self, args):
        """Distributed trace viewer."""
        tr  = self.kernel.tracer
        sub = args[0] if args else "list"
        if sub == "list":
            for t in tr.recent_traces(10):
                print(f"  {t['trace_id']}  {t['spans']:>3} spans  {t['total_ms']:>8.1f}ms  {t['service']}")
        elif sub == "show" and len(args)>1:
            path = f"{tr.TRACE_BASE}/{args[1]}"
            try:
                spans = __import__("json").loads(self.sos.read(path))
                print(f"  Trace: {args[1]}")
                for s in spans:
                    indent = "  " * (1 if s.get("parent_id") else 0)
                    print(f"  {indent}{s['name']:<30} {s['duration_ms']:.1f}ms  {s['service']}")
            except Exception as e: self._err(str(e))
        elif sub == "clear":
            for n in self.kernel.sos.listdir(tr.TRACE_BASE):
                try: self.kernel.sos.remove(f"{tr.TRACE_BASE}/{n}")
                except: pass
            self._ok("Traces cleared")
        else:
            print("  Usage: traces list | traces show <id> | traces clear")

    def _sshserver_cmd(self, args):
        """SSH server management."""
        ssh = self.kernel.ssh_server
        sub = args[0] if args else "status"
        if sub == "start":
            port = int(args[1]) if len(args)>1 else 2222
            url = ssh.start(); self._ok(f"SSH server: {url}")
        elif sub == "status":
            s = ssh.status()
            for k,v in s.items(): print(f"  {k:<15} {v}")
        elif sub == "stop":
            ssh.stop(); self._ok("SSH server stopped")
        else:
            print("  Usage: ssh-server start [port] | ssh-server status | ssh-server stop")

    def _gitbridge_cmd(self, args):
        """Git repository bridge."""
        gb  = self.kernel.git_bridge
        sub = args[0] if args else "list"
        if sub == "mount" and len(args)>=3:
            url  = args[1]; sos_path = self._resolve(args[2])
            print(col(f"  Cloning {url}...","dim"), flush=True)
            result = gb.mount(url, sos_path)
            if "error" in result: self._err(result["error"])
            else: self._ok(f"Mounted: {sos_path} ({result.get('files',0)} files)")
        elif sub == "unmount" and len(args)>1:
            if gb.unmount(self._resolve(args[1])): self._ok("Unmounted")
            else: self._err("Mount not found")
        elif sub == "list":
            for m in gb.list_mounts(): print(f"  {m['sas_path']:<30} ← {m['url']}")
        else:
            print("  Usage: git-bridge mount <url> <sas-path> | git-bridge unmount <path> | git-bridge list")

    def _chaos_cmd(self, args):
        """Chaos engineering fault injection."""
        chaos = self.kernel.chaos
        sub   = args[0] if args else "status"
        if sub == "inject" and len(args)>=2:
            fault = args[1]
            rate  = float(args[2]) if len(args)>2 else 0.1
            chaos.inject(fault, rate)
            if not getattr(chaos, "_patched", False):
                chaos.patch_sos(); chaos._patched = True
            self._ok(f"Injecting {fault} at {rate:.0%} rate")
        elif sub == "stop":
            fault = args[1] if len(args)>1 else None
            chaos.stop(fault); self._ok(f"Chaos stopped{' for '+fault if fault else ''}")
        elif sub == "status":
            s = chaos.status()
            print(f"  Active faults: {s['active_faults']}")
            print(f"  Injected total: {s['total_injected']}")
            print(f"  Available: {', '.join(s['available'])}")
        else:
            print("  Usage: chaos inject <fault> [rate] | chaos stop [fault] | chaos status")
            print(f"  Faults: latency, error, loss, memory, cpu")

    def _dns_cmd(self, args):
        """Built-in DNS server."""
        dns = self.kernel.dns
        sub = args[0] if args else "status"
        if sub == "start":
            url = dns.start(); self._ok(f"DNS server: {url}")
        elif sub == "add" and len(args)>=3:
            rtype = args[3].upper() if len(args)>3 else "A"
            dns.add_record(args[1], rtype, args[2])
            self._ok(f"DNS record: {args[1]} {rtype} → {args[2]}")
        elif sub == "list":
            for r in dns.list_records():
                print(f"  {r['name']:<30} {r['type']:<6} {r['value']}")
        elif sub == "status":
            s = dns.status()
            for k,v in s.items(): print(f"  {k:<15} {v}")
        elif sub == "stop":
            dns.stop(); self._ok("DNS server stopped")
        else:
            print("  Usage: dns start|stop|status | dns add <name> <ip> [type] | dns list")

    def _debug_cmd(self, args):
        """Interactive Python debugger."""
        dbg = self.kernel.debugger
        sub = args[0] if args else "help"
        if sub == "breakpoint" and len(args)>=3:
            rp = self._resolve(args[1]); line = int(args[2])
            dbg.set_breakpoint(rp, line); self._ok(f"Breakpoint: {rp}:{line}")
        elif sub == "list":
            for bp in dbg.list_breakpoints():
                print(f"  {bp['path']:<40} line {bp['line']}")
        elif sub == "clear" and len(args)>1:
            n = dbg.clear_breakpoint(self._resolve(args[1])); self._ok(f"Cleared {n} breakpoint(s)")
        elif args and args[0] not in ("breakpoint","list","clear","help"):
            rp = self._resolve(args[0])
            dbg.debug_file(rp, args[1:])
        else:
            print("  Usage: debug <script.py>  |  debug breakpoint <path> <line>  |  debug list  |  debug clear <path>")

    def _make_cmd(self, args):
        """Build system task runner."""
        build = self.kernel.build
        sub   = args[0] if args else "list"
        if sub == "list":
            tasks = build.list_tasks()
            if not tasks: print(col("  No tasks defined. Create /build/Novafile","dim")); return
            for t in tasks:
                dirty = col("dirty","yellow") if t["dirty"] else col("clean","green")
                print(f"  {col(t['name'],'cyan'):<25} {dirty}  deps:{t['deps']}")
        elif sub == "define" and len(args)>=3:
            name = args[1]; cmds = args[2:]
            build.define(name, cmds); self._ok(f"Task defined: {name}")
        elif args and args[0] not in ("list","define"):
            target = args[0]; force = "--force" in args
            print(col(f"  Building {target}...","dim"))
            results = build.run(target, force=force)
            for name, r in results.items():
                st = col(r["status"],"green" if r["status"]=="ok" else "yellow" if r["status"]=="skipped" else "red")
                print(f"  {name:<25} {st}  {r.get('time_s',0):.1f}s")
        else:
            print("  Usage: make [target] [--force] | make list | make define <name> <cmds...>")

    def _profile_cmd(self, args):
        """Flame graph profiler."""
        pr  = self.kernel.profiler
        sub = args[0] if args else "list"
        if sub == "list":
            for p in pr.list_profiles():
                print(f"  {p['name']:<40} {p['size_kb']}KB")
        elif args and args[0] not in ("list","help"):
            # profile a file or shell command
            path_or_cmd = args[0]
            rp = self._resolve(path_or_cmd)
            if self.sos.exists(rp):
                code = self.sos.read(rp)
                name = path_or_cmd.rsplit("/",1)[-1].replace(".py","")
                path = pr.profile_code(code, name=name)
            else:
                code = path_or_cmd
                path = pr.profile_code(code, name="inline")
            self._ok(f"Flame graph saved: {path}")
        else:
            print("  Usage: profile <file.py|command> | profile list")

    def _rec_cmd(self, args):
        """Terminal session recorder."""
        rec = self.kernel.recorder
        sub = args[0] if args else "list"
        if sub == "start":
            title = " ".join(args[1:]) if len(args)>1 else ""
            rid = rec.start(title); self._ok(f"Recording [{rid}]")
        elif sub == "stop":
            if not rec.is_recording: self._err("Not recording"); return
            path = rec.stop(); self._ok(f"Saved: {path}")
        elif sub == "play" and len(args)>1:
            speed = float(args[2]) if len(args)>2 else 1.0
            rec.play(args[1], speed=speed)
        elif sub == "list":
            recs = rec.list_recordings()
            if not recs: print(col("  No recordings.","dim")); return
            for r in recs:
                print(f"  {r['id']}  {r['title']:<30} {r['duration']:.1f}s")
        elif sub == "status":
            print(f"  Recording: {col('yes','red') if rec.is_recording else col('no','dim')}")
        else:
            print("  Usage: rec start [title] | rec stop | rec play <id> [speed] | rec list | rec status")

    def _debug_cmd(self, args):
        """debug <script> [--break <loc>] — interactive debugger."""
        dbg = self.kernel.debugger
        if not args: print("  Usage: debug <script.py>"); return
        rp = self._resolve(args[0])
        dbg.debug_script(rp, args[1:])

    def _breakpoints_cmd(self, args):
        """Manage debugger breakpoints."""
        dbg = self.kernel.debugger
        sub = args[0] if args else "list"
        if sub == "list":
            bps = dbg.list_breakpoints()
            if not bps: print(col("  No breakpoints.","dim")); return
            for b in bps: print(f"  [{b['id']}] {b['location']}  hits:{b['hits']}")
        elif sub == "add" and len(args)>1: b=dbg.add_breakpoint(args[1]); self._ok(f"Breakpoint {b['id']}: {b['location']}")
        elif sub == "rm" and len(args)>1: dbg.remove_breakpoint(int(args[1])); self._ok("Removed")
        elif sub == "clear": dbg.clear_all(); self._ok("Cleared")
        else: print("  Usage: breakpoints [list|add <loc>|rm <id>|clear]")

    def _make_cmd(self, args):
        """make [task] [--list|--dry-run|--clean|--graph] — build system."""
        bs = self.kernel.builder
        bs.load_novafile()
        if "--list" in args: [print(f"  {t.name:<20} {t.desc}") for t in bs.list_tasks()]; return
        if "--clean" in args: bs.clean(); self._ok("Build cache cleared"); return
        if "--graph" in args: print(bs.graph()); return
        target   = next((a for a in args if not a.startswith("--")), None)
        dry_run  = "--dry-run" in args
        results  = bs.run(target, dry_run=dry_run)
        for r in results:
            icon = col("✓","green") if r.ok else col("✗","red")
            ms   = f" {r.duration*1000:.0f}ms" if r.duration else ""
            print(f"  {icon} {r.name:<20} {r.status}{ms}")
            if r.output and len(r.output) < 200: print(f"    {r.output[:150]}")

    def _profile_cmd(self, args):
        """profile run <script> | profile list | profile report <id>."""
        pf  = self.kernel.profiler
        sub = args[0] if args else "list"
        if sub == "run" and len(args)>1:
            rp = self._resolve(args[1])
            r  = pf.profile_script(rp, args[2:])
            if "error" in r: self._err(r["error"]); return
            self._ok(f"Profile saved: {r['profile_id']}  ({r['duration_s']*1000:.0f}ms)")
            for fn in r.get("top", [])[:5]:
                print(f"  {fn['ms']:>8.1f}ms  {fn['fn']}")
            print(f"  SVG: {r['svg_path']}")
        elif sub == "list":
            for p in pf.list_profiles()[:10]:
                print(f"  {p['id']:<30} {p['name']:<25} {p['ms']}ms")
        else: print("  Usage: profile run <script> | profile list")

    def _rec_cmd(self, args):
        """rec / rec stop — session recorder."""
        cast = self.kernel.cast
        if args and args[0] == "stop":
            path = cast.stop(); self._ok(f"Session saved: {path}")
        elif cast.is_recording:
            print(col("  Already recording. Use: rec stop","yellow"))
        else:
            sid = cast.start(); self._ok(f"Recording session [{sid}]. Type 'rec stop' to save.")

    def _cast_cmd(self, args):
        """cast list | cast export <id>."""
        cast = self.kernel.cast
        sub  = args[0] if args else "list"
        if sub == "list":
            for s in cast.list_sessions()[:10]: print(f"  {s['id']}")
        elif sub == "export" and len(args)>1:
            p = cast.export_svg(args[1])
            if p: self._ok(f"SVG: {p}")
            else: self._err("Session not found")
        else: print("  Usage: cast list | cast export <id>")

    def _play_cmd(self, args):
        """play <session_id> [--speed N] — replay a recorded session."""
        if not args: print("  Usage: play <id> [--speed N]"); return
        speed = 1.0
        if "--speed" in args:
            idx = args.index("--speed")
            try: speed = float(args[idx+1])
            except: pass
        self.kernel.cast.replay(args[0], speed=speed)

    def _git_bridge_cmd(self, args):
        """git mount <url> <path> | git unmount <path> | git mounts."""
        gb  = self.kernel.git
        sub = args[0] if args else "mounts"
        if sub == "mount" and len(args)>=3:
            print(col(f"  Cloning {args[1]}...","dim"))
            r = gb.mount(args[1], args[2])
            if "error" in r: self._err(r["error"])
            else: self._ok(f"Mounted {r['files']} files at {r['mounted']}")
        elif sub == "unmount" and len(args)>1:
            gb.unmount(args[1]); self._ok(f"Unmounted {args[1]}")
        elif sub == "mounts":
            mts = gb.list_mounts()
            if not mts: print(col("  No git mounts.","dim"))
            for m in mts: print(f"  {col(m['path'],'cyan'):<35} {m['url']}")
        else: print("  Usage: git mount <url> <path> | git unmount <path> | git mounts")

    def _sshd_cmd(self, args):
        """sshd [port] | sshd stop | sshd status."""
        sub = args[0] if args else "status"
        ssh = self.kernel.ssh
        if sub == "stop": ssh.stop(); self._ok("SSH server stopped")
        elif sub == "status":
            print(f"  SSH server: {col('running','green') if ssh.running else col('stopped','dim')}")
            print(f"  Port: {ssh.port}")
            print(f"  Backend: {'paramiko' if ssh._has_paramiko() else 'TCP fallback'}")
        else:
            port = int(sub) if sub.isdigit() else 2222
            ssh.port = port
            msg = ssh.start()
            self._ok(msg)

    def _resilience_cmd(self, args):
        """resilience status | resilience test <name>."""
        rk  = self.kernel.resilience
        sub = args[0] if args else "status"
        if sub == "status":
            s = rk.status()
            if not s["breakers"] and not s["limiters"]:
                print(col("  No resilience primitives active.","dim")); return
            for n, st in s["breakers"].items(): print(f"  breaker {n}: {col(st,'green' if st=='closed' else 'red')}")
            for n, st in s["limiters"].items(): print(f"  limiter {n}: {st}")
        else: print("  Usage: resilience status")



    def _chaos_cmd(self, args):
        """chaos inject <fault> | chaos stop [fault] | chaos status."""
        ch  = self.kernel.chaos
        sub = args[0] if args else "status"
        if sub == "inject" and len(args)>1:
            if ch.inject(args[1]): self._ok(f"Fault injected: {args[1]}")
            else: print(f"  Unknown fault. Available: {', '.join(ch._active_faults or list(__import__('runtime.completeness',fromlist=['FAULTS']).FAULTS.keys()))}")
        elif sub == "stop":
            stopped = ch.stop(args[1] if len(args)>1 else None)
            self._ok(f"Stopped: {', '.join(stopped) or 'none'}")
        elif sub == "status":
            active = ch.active_faults()
            if not active: print(col("  No active faults.","dim"))
            for f in active: print(f"  {col(f,'red')}  active")
        elif sub == "list":
            from runtime.completeness import FAULTS
            for name, desc in FAULTS.items(): print(f"  {col(name,'cyan'):<14} {desc}")
        else: print("  Usage: chaos inject <fault> | chaos stop | chaos list | chaos status")

    def _view_cmd(self, args):
        """view define <n> <sql> | view refresh <n> | view list | view query <n>."""
        vm  = self.kernel.views
        sub = args[0] if args else "list"
        if sub == "define" and len(args)>=3:
            vm.define(args[1], " ".join(args[2:]))
            self._ok(f"View defined: {args[1]}")
        elif sub == "refresh" and len(args)>1:
            rows = vm.refresh(args[1])
            self._ok(f"Refreshed: {len(rows)} rows")
        elif sub == "query" and len(args)>1:
            rows = vm.query(args[1])
            for r in rows[:20]: print(f"  {r}")
        elif sub == "list":
            for v in vm.list_views(): print(f"  {col(v['name'],'cyan'):<20} {v['rows']} rows  {'dirty' if v['dirty'] else 'fresh'}")
        else: print("  Usage: view define <n> <sql> | view refresh <n> | view query <n> | view list")

    def _mvcc_cmd(self, args):
        """mvcc begin | mvcc read <path> | mvcc write <path> <content> | mvcc commit."""
        sub = args[0] if args else "help"
        if sub == "begin":
            self._mvcc_txn = __import__("runtime.completeness",fromlist=["MVCCTransaction"]).MVCCTransaction(self.kernel.sos)
            self._ok(f"Snapshot transaction started (ts={self._mvcc_txn._start_ts:.0f})")
        elif sub == "read" and len(args)>1:
            txn = getattr(self,"_mvcc_txn",None)
            if not txn: self._err("No active transaction. Run: mvcc begin"); return
            rp  = self._resolve(args[1])
            print(self._mvcc_txn.read(rp)[:500])
        elif sub == "write" and len(args)>=3:
            txn = getattr(self,"_mvcc_txn",None)
            if not txn: self._err("No active transaction"); return
            rp  = self._resolve(args[1])
            self._mvcc_txn.write(rp, " ".join(args[2:]))
            self._ok(f"Buffered write to {rp}")
        elif sub == "commit":
            txn = getattr(self,"_mvcc_txn",None)
            if not txn: self._err("No active transaction"); return
            r = self._mvcc_txn.commit()
            if r["ok"]: self._ok(f"Committed {r['writes']} write(s)")
            else: self._err(f"Conflict: {r.get('conflicts',r.get('error',''))}")
            self._mvcc_txn = None
        elif sub == "rollback":
            txn = getattr(self,"_mvcc_txn",None)
            if txn: txn.rollback(); self._mvcc_txn = None
            self._ok("Rolled back")
        else: print("  Usage: mvcc begin | mvcc read <p> | mvcc write <p> <c> | mvcc commit | mvcc rollback")

    def _pipeline_cmd(self, args):
        """pipeline list | pipeline run <def>."""
        sp = self.kernel.stream
        if not sp: print(col("  Stream processor not initialised.","dim")); return
        sub = args[0] if args else "list"
        if sub == "list":
            for s in sp.status(): print(f"  {col(s['name'],'cyan'):<20} {s['events']} events  {'running' if s['running'] else 'stopped'}")
        else: print("  Usage: pipeline list")

    def _eventsrc_cmd(self, args):
        """eventsrc log [n] | eventsrc rebuild."""
        es  = self.kernel.eventsrc
        sub = args[0] if args else "log"
        if sub == "log":
            n = int(args[1]) if len(args)>1 else 20
            for e in es.recent(n):
                ts = __import__("time").strftime("%H:%M:%S",__import__("time").localtime(e.ts))
                print(f"  {col(ts,'dim')} {e.op:<8} {e.path}")
        elif sub == "rebuild":
            import tempfile, shutil
            td   = tempfile.mkdtemp()
            from store.sos import SemanticObjectStore
            new_sos = SemanticObjectStore(db_path=__import__("os").path.join(td,"rebuilt.db"))
            n   = es.rebuild(new_sos)
            self._ok(f"Rebuilt {n} events in {td}/rebuilt.db")
            shutil.rmtree(td, ignore_errors=True)
        else: print("  Usage: eventsrc log [n] | eventsrc rebuild")

    def _schema_cmd(self, args):
        """schema add <path> <schema_name> | schema validate <path> | schema register <n> <json>."""
        sv  = self.kernel.schema
        sub = args[0] if args else "help"
        if sub == "register" and len(args)>=3:
            import json as _j
            try:
                schema = _j.loads(" ".join(args[2:]))
                sv.register_schema(args[1], schema)
                self._ok(f"Schema registered: {args[1]}")
            except Exception as e: self._err(str(e))
        elif sub == "validate" and len(args)>1:
            rp = self._resolve(args[1])
            errors = sv.validate_sos_object(rp)
            if errors:
                for e in errors: print(f"  {col('✗','red')} {e}")
            else:
                self._ok("Valid")
        elif sub == "add" and len(args)>=3:
            rp = self._resolve(args[1])
            self.sos.tag(rp, f"schema:{args[2]}"); self._ok(f"Schema {args[2]} attached to {rp}")
        else: print("  Usage: schema register <n> <json> | schema validate <path> | schema add <path> <schema>")

    def _bloom_cmd(self, args):
        """bloom status | bloom rebuild."""
        bl  = self.kernel.bloom
        sub = args[0] if args else "status"
        if sub == "status":
            s = bl.status()
            for k,v in s.items(): print(f"  {k:<20} {v}")
        elif sub == "rebuild":
            n = bl.rebuild(); self._ok(f"Bloom filter rebuilt: {n} paths indexed")
        else: print("  Usage: bloom status | bloom rebuild")

    def _lang_cmd(self, args):
        """lang [set <code>|detect <text>|list|auto|install <from> <to>]."""
        i18n = self.kernel.i18n
        sub  = args[0] if args else "status"
        if sub in ("status", "") or not args:
            loc = i18n.locale
            rtl = " (RTL)" if loc.rtl else ""
            print(f"  Language : {col(loc.name,'cyan')} ({loc.code}){rtl}")
            print(f"  Locale   : {loc}")
            print(f"  Argos    : {col('installed','green') if i18n.translator._argos_ok else col('not installed','dim')}")
        elif sub == "set" and len(args)>1:
            code = args[1].lower()[:2]
            from i18n.engine import LANGUAGE_NAMES
            if code in LANGUAGE_NAMES:
                i18n.set_locale(code)
                self._ok(f"Language set to {LANGUAGE_NAMES[code]} ({code})")
            else:
                self._err(f"Unknown language code: {code}. Try: lang list")
        elif sub == "auto":
            i18n.auto_detect_locale()
            self._ok(f"Auto-detected: {i18n.locale}")
        elif sub == "detect" and len(args)>1:
            text  = " ".join(args[1:])
            code, conf = i18n.detect(text)
            from i18n.engine import LANGUAGE_NAMES
            name  = LANGUAGE_NAMES.get(code, code)
            print(f"  Detected: {col(name,'cyan')} ({code})  confidence: {conf:.0%}")
        elif sub == "list":
            langs = i18n.available_languages()
            print(f"  {'Code':<6} {'Language':<20} {'Coverage'}")
            print("  " + "─"*50)
            for l in langs:
                rtl = " ←" if l["rtl"] else ""
                mark = col("●","green") if l["code"] == i18n.locale.code else " "
                print(f"  {mark} {l['code']:<5} {l['name']:<20} {l['coverage']}{rtl}")
        elif sub == "install" and len(args)>=3:
            print(col(f"  Downloading Argos language pair {args[1]}→{args[2]}...","dim"))
            try:
                import argostranslate.package as p
                p.update_package_index()
                pkgs = p.get_available_packages()
                pkg  = next((x for x in pkgs if x.from_code==args[1] and x.to_code==args[2]), None)
                if pkg:
                    p.install_from_path(pkg.download())
                    self._ok(f"Installed {args[1]}→{args[2]} translation model")
                else:
                    self._err(f"No model found for {args[1]}→{args[2]}")
            except ImportError:
                self._err("argostranslate not installed. Run: pip install argostranslate")
        else:
            print("  Usage: lang | lang set <code> | lang detect <text> | lang list | lang auto | lang install <from> <to>")

    def _translate_cmd(self, args):
        """translate <text> [--to <code>] — translate text to current or specified language."""
        if not args:
            print("  Usage: translate <text> [--to <code>]")
            return
        i18n = self.kernel.i18n
        to_lang = i18n.locale.code
        text_parts = []
        i = 0
        while i < len(args):
            if args[i] == "--to" and i+1 < len(args):
                to_lang = args[i+1]; i += 2
            else:
                text_parts.append(args[i]); i += 1
        text = " ".join(text_parts)
        if not text:
            print("  No text to translate")
            return
        result = i18n.translator.translate(text, to_lang)
        from i18n.engine import LANGUAGE_NAMES
        lang_name = LANGUAGE_NAMES.get(to_lang, to_lang)
        print(f"  [{lang_name}] {col(result,'cyan')}")

    def _watchdog_cmd(self, args):
        """watchdog status|restart <n>|quarantine|resume <n>."""
        wd  = self.kernel.watchdog
        sub = args[0] if args else "status"
        if sub == "status":
            for s in wd.status():
                icon = col("●","green") if s["state"]=="healthy" else col("●","red")
                age  = f" ({s['last_ok_s']}s ago)" if s['last_ok_s'] else ""
                print(f"  {icon} {s['name']:<20} {s['state']:<12} restarts:{s['restarts']}{age}")
        elif sub == "restart" and len(args)>1:
            ok = wd.restart(args[1]); self._ok("Restarting") if ok else self._err("Not found")
        elif sub == "quarantine":
            q = wd.quarantined()
            if not q: print(col("  No quarantined subsystems.","dim"))
            for n in q: print(f"  {col(n,'red')}")
        elif sub == "resume" and len(args)>1:
            ok = wd.resume(args[1]); self._ok("Resumed") if ok else self._err("Not found")
        else: print("  Usage: watchdog status|restart <n>|quarantine|resume <n>")

    def _plugin_cmd(self, args):
        """plugin list|search|install|remove|enable|disable|info."""
        reg = self.kernel.plugins
        sub = args[0] if args else "list"
        if sub == "list":
            for m in reg.list_installed():
                icon = col("●","green") if m.enabled else col("○","dim")
                tag  = col("[builtin]","dim") if m.builtin else ""
                print(f"  {icon} {col(m.name,'cyan'):<25} v{m.version:<10} {tag} {m.description[:35]}")
        elif sub == "search":
            q = " ".join(args[1:]) if len(args)>1 else ""
            for p in reg.search(q):
                inst = col("[installed]","green") if p.get("installed") else ""
                print(f"  {p['name']:<25} {inst} {p.get('description','')[:45]}")
        elif sub == "info" and len(args)>1:
            m = reg.get(args[1])
            if not m: self._err("Plugin not found"); return
            print(f"  Name:    {m.name}")
            print(f"  Version: {m.version}")
            print(f"  Desc:    {m.description}")
            print(f"  Perms:   {', '.join(m.permissions)}")
            print(f"  Enabled: {m.enabled}")
        elif sub == "enable" and len(args)>1:
            reg.enable(args[1]); self._ok(f"Enabled {args[1]}")
        elif sub == "disable" and len(args)>1:
            reg.disable(args[1]); self._ok(f"Disabled {args[1]}")
        elif sub == "remove" and len(args)>1:
            ok = reg.remove(args[1]); self._ok("Removed") if ok else self._err("Cannot remove builtin")
        else: print("  Usage: plugin list|search [q]|info <n>|enable <n>|disable <n>|remove <n>")

    def _pkg_cmd(self, args):
        """pkg install|remove|list|search|audit|update|freeze."""
        pm  = self.kernel.pkg
        sub = args[0] if args else "list"
        if sub == "list":
            for r in pm.list_packages()[:30]:
                print(f"  {col(r.name,'cyan'):<30} {r.version:<15} {r.installed_by}")
        elif sub == "install" and len(args)>1:
            name = args[1]; ver = args[2] if len(args)>2 else ""
            print(col(f"  Installing {name}...","dim"))
            ok, msg = pm.install(name, ver, user=self.user)
            self._ok(msg) if ok else self._err(msg)
        elif sub == "remove" and len(args)>1:
            ok, msg = pm.remove(args[1]); self._ok(msg) if ok else self._err(msg)
        elif sub == "search" and len(args)>1:
            results = pm.search_pypi(args[1])
            for r in results: print(f"  {r['name']:<25} {r.get('version','')} — {r.get('summary','')[:50]}")
        elif sub == "audit":
            issues = pm.audit()
            if not issues: self._ok("No known CVEs found")
            for v in issues:
                print(f"  {col(v['cve'],'red')} {v['package']:<20} {v['severity']} — {v['desc'][:50]}")
        elif sub == "update":
            name = args[1] if len(args)>1 else None
            results = pm.update(name, user=self.user)
            for n, ok, msg in results:
                (self._ok if ok else self._err)(f"{n}: {msg[:50]}")
        elif sub == "freeze":
            print(pm.freeze())
        else: print("  Usage: pkg install|remove|list|search|audit|update|freeze")

    def _discover_cmd(self, args):
        """discover list|announce."""
        sd  = self.kernel.discovery
        sub = args[0] if args else "list"
        if sub == "list":
            nodes = sd.list_nodes()
            if not nodes: print(col("  No NOVA nodes discovered yet.","dim")); return
            for n in nodes:
                age = round(time.time()-n.seen_at)
                print(f"  {col(n.hostname,'cyan'):<20} {n.ip:<16} port:{n.api_port}  {age}s ago")
        elif sub == "announce":
            sd.announce_now(); self._ok("Presence broadcast sent")
        else: print("  Usage: discover list|announce")

    def _gateway_cmd(self, args):
        """gateway list|add <path> <upstream>|remove <path>."""
        gw  = self.kernel.gateway
        sub = args[0] if args else "list"
        if sub == "list":
            routes = gw.list_routes()
            if not routes: print(col("  No routes configured.","dim")); return
            for r in routes:
                print(f"  {col(r['pattern'],'cyan'):<25} → {r['upstream']:<30} reqs:{r['requests']}")
        elif sub == "add" and len(args)>=3:
            gw.add_route(args[1], args[2]); self._ok(f"Route added: {args[1]} → {args[2]}")
        elif sub == "remove" and len(args)>1:
            ok = gw.remove_route(args[1]); self._ok("Removed") if ok else self._err("Not found")
        elif sub == "openapi":
            print(json.dumps(gw.generate_openapi(), indent=2)[:1000])
        else: print("  Usage: gateway list|add <pattern> <upstream>|remove <pattern>|openapi")

    def _dns_cmd(self, args):
        """dns start [port]|stop|record <n> <type> <value>|list."""
        dns = self.kernel.dns
        sub = args[0] if args else "list"
        if sub == "start":
            port = int(args[1]) if len(args)>1 and args[1].isdigit() else 5353
            dns._port = port; msg = dns.start(); self._ok(msg)
        elif sub == "stop": dns.stop(); self._ok("DNS server stopped")
        elif sub == "record" and len(args)>=4:
            dns.add_record(args[1], args[2], args[3])
            self._ok(f"Record: {args[1]} {args[2]} {args[3]}")
        elif sub == "list":
            for r in dns.list_records(): print(f"  {r['name']:<30} {r['type']:<8} {r['value']:<20} ttl:{r['ttl']}")
        else: print("  Usage: dns start [port]|stop|record <n> <type> <val>|list")

    def _model_cmd(self, args):
        """model list|pull <repo> [file]|use <n>|rm <n>|bench [n]|recommended."""
        mm  = self.kernel.models
        sub = args[0] if args else "list"
        if sub == "list":
            models = mm.list_models()
            if not models: print(col("  No models installed.","dim")); return
            for m in models:
                active = col(" [active]","green") if m.is_active else ""
                tps    = f"  {m.bench_tps:.0f} t/s" if m.bench_tps else ""
                print(f"  {col(m.name,'cyan'):<25} {m.size_gb():.1f}GB{tps}{active}")
        elif sub == "pull" and len(args)>=2:
            repo = args[1]; fname = args[2] if len(args)>2 else ""
            name = args[3] if len(args)>3 else ""
            print(col(f"  Downloading from {repo}...","dim"))
            def _progress(done, total):
                if total: print(f"  {done//1024//1024}/{total//1024//1024} MB", end="", flush=True)
            ok, msg = mm.pull(repo, fname, name, progress_cb=_progress)
            print(); (self._ok if ok else self._err)(msg)
        elif sub == "use" and len(args)>1:
            ok = mm.use(args[1]); self._ok(f"Active model: {args[1]}") if ok else self._err("Not found")
        elif sub == "rm" and len(args)>1:
            ok, msg = mm.remove(args[1]); (self._ok if ok else self._err)(msg)
        elif sub == "bench":
            n = args[1] if len(args)>1 else None
            for r in mm.bench(n):
                if "error" in r: print(f"  {r['name']}: {col(r['error'],'red')}")
                else: print(f"  {r['name']:<25} {r['tps']:.1f} t/s  {r['ram_mb']} MB  {r['duration']}s")
        elif sub == "recommended":
            for m in mm.recommended():
                inst = col("[installed]","green") if m["installed"] else ""
                print(f"  {m['name']:<20} {m['size_gb']}GB  {inst} {m['desc']}")
        else: print("  Usage: model list|pull <repo> [file]|use <n>|rm <n>|bench|recommended")

    def _finetune_cmd(self, args):
        """finetune start [model]|status|list."""
        ft  = self.kernel.finetuner
        sub = args[0] if args else "status"
        if sub == "start":
            model = args[1] if len(args)>1 else "default"
            job   = ft.start(model)
            self._ok(f"Fine-tuning started: job {job.job_id}  samples: {job.samples}")
        elif sub == "status":
            jobs = ft.list_jobs()
            if not jobs: print(col("  No fine-tuning jobs.","dim")); return
            for j in jobs:
                col_ = "green" if j.status=="done" else "yellow" if j.status=="running" else "red"
                print(f"  [{j.job_id}] {col(j.status,col_):<12} {j.model_name}  samples:{j.samples}")
                if j.error: print(f"    error: {j.error[:60]}")
        else: print("  Usage: finetune start [model]|status")

    def _tenant_cmd(self, args):
        """tenant create <n>|list|quota <n> <mb>|disable <n>."""
        tm  = self.kernel.tenants
        sub = args[0] if args else "list"
        if sub == "list":
            tenants = tm.list_tenants()
            if not tenants: print(col("  No tenants.","dim")); return
            for t in tenants:
                icon = col("●","green") if t.enabled else col("○","dim")
                print(f"  {icon} {col(t.name,'cyan'):<20} quota:{t.quota_mb}MB  used:{t.used_mb:.1f}MB  roles:{t.roles}")
        elif sub == "create" and len(args)>1:
            quota = int(args[2]) if len(args)>2 else 512
            t = tm.create(args[1], quota_mb=quota); self._ok(f"Tenant created: {t.name} ({quota}MB)")
        elif sub == "quota" and len(args)>=3:
            ok = tm.set_quota(args[1], int(args[2])); self._ok("Quota updated") if ok else self._err("Not found")
        elif sub == "disable" and len(args)>1:
            ok = tm.disable(args[1]); self._ok("Disabled") if ok else self._err("Not found")
        else: print("  Usage: tenant create <n>|list|quota <n> <mb>|disable <n>")

    def _rbac_cmd(self, args):
        """rbac role create|list|grant|revoke  assign <user> <role>  check <user> <cap>."""
        rb  = self.kernel.rbac
        sub = args[0] if args else "list"
        if sub in ("list", "roles"):
            for r in rb.list_roles():
                print(f"  {col(r.name,'cyan'):<16} {r.description[:35]}")
                print(f"    caps: {', '.join(r.capabilities[:6])}")
        elif sub == "create" and len(args)>1:
            r = rb.create_role(args[1], description=" ".join(args[2:]))
            self._ok(f"Role created: {r.name}")
        elif sub == "grant" and len(args)>=3:
            ok = rb.grant(args[1], args[2]); self._ok("Granted") if ok else self._err("Role not found")
        elif sub == "revoke" and len(args)>=3:
            ok = rb.revoke(args[1], args[2]); self._ok("Revoked") if ok else self._err("Role not found")
        elif sub == "assign" and len(args)>=3:
            ok = rb.assign(args[1], args[2]); self._ok(f"Role {args[2]} assigned to {args[1]}") if ok else self._err("Role not found")
        elif sub == "check" and len(args)>=3:
            ok = rb.has_capability(args[1], args[2])
            print(f"  {args[1]} has {args[2]}: {col('yes','green') if ok else col('no','red')}")
        else: print("  Usage: rbac list|create <n>|grant <role> <cap>|revoke <role> <cap>|assign <user> <role>|check <user> <cap>")

    def _ha_cmd(self, args):
        """ha status|elect|peers."""
        raft = self.kernel.raft
        sub  = args[0] if args else "status"
        if sub == "status":
            s = raft.status()
            state_col = "green" if s["state"]=="leader" else "yellow" if s["state"]=="candidate" else "cyan"
            for k,v in s.items(): print(f"  {k:<12} {col(str(v), state_col) if k=='state' else v}")
        elif sub == "peers" and len(args)>1:
            raft.peers.append(args[1]); self._ok(f"Peer added: {args[1]}")
        else: print("  Usage: ha status|peers <url>")

    def _gdpr_cmd(self, args):
        """gdpr erase <user>|export-audit [since]|scan-phi <path>."""
        comp = self.kernel.compliance
        sub  = args[0] if args else "help"
        if sub == "erase" and len(args)>1:
            import time as _t
            confirm = input(f"  {col('WARNING','red')}: permanently erase all data for {args[1]}? [yes/no] ")
            if confirm.lower() == "yes":
                r = comp.erase_user(args[1])
                self._ok(f"Erased {r['erased_objects']} objects for {r['user']}")
            else:
                print("  Cancelled.")
        elif sub == "export-audit":
            since = float(args[1]) if len(args)>1 else None
            log   = comp.export_audit_log(since)
            if log: print(log[:2000])
            else: print(col("  No audit entries found.","dim"))
        elif sub == "scan-phi" and len(args)>1:
            rp = self._resolve(args[1])
            content = self.sos.read(rp)
            matches = comp.scan_phi(content)
            if matches:
                print(f"  {col('PHI DETECTED','red')} in {rp}:")
                for m in matches: print(f"    pattern: {m}")
            else:
                self._ok(f"No PHI detected in {rp}")
        else: print("  Usage: gdpr erase <user>|export-audit [since]|scan-phi <path>")

    def _metrics_cmd(self, args):
        """metrics — show Prometheus metrics snapshot."""
        obs = self.kernel.observe
        sub = args[0] if args else "show"
        if sub == "show" or not args:
            print(obs.prometheus_text()[:2000])
        elif sub == "export":
            endpoint = args[1] if len(args)>1 else "http://localhost:4317"
            ok = obs.export_otlp(endpoint)
            self._ok(f"Exported to {endpoint}") if ok else self._err("Export failed")
        elif sub == "server":
            port = int(args[1]) if len(args)>1 else 9090
            url  = obs.start_metrics_server(port); self._ok(f"Metrics: {url}")
        else: print("  Usage: metrics [show|export [endpoint]|server [port]]")

    def _deploy_cmd(self, args):
        """deploy k8s|compose|terraform — generate deployment manifests."""
        cloud = self.kernel.cloud
        sub   = args[0] if args else "help"
        if sub == "k8s":
            manifest = cloud.kubernetes()
            path     = "/deploy/nova.yaml"
            self.sos.write(path, manifest, tags=["deploy-manifest"])
            self._ok(f"Kubernetes manifest saved: {path}")
            print(manifest[:800])
        elif sub == "compose":
            manifest = cloud.docker_compose()
            path     = "/deploy/docker-compose.yml"
            self.sos.write(path, manifest, tags=["deploy-manifest"])
            self._ok(f"Docker Compose saved: {path}")
            print(manifest[:500])
        elif sub == "terraform":
            provider = args[1] if len(args)>1 else "aws"
            region   = args[2] if len(args)>2 else "us-east-1"
            manifest = cloud.terraform(provider, region)
            path     = "/deploy/main.tf"
            self.sos.write(path, manifest, tags=["deploy-manifest"])
            self._ok(f"Terraform module saved: {path}")
            print(manifest[:600])
        else: print("  Usage: deploy k8s|compose|terraform [provider] [region]")

    def _http_cmd(self, args):
        """http GET|POST|PUT|DELETE <url> [--json] [--save <path>] [--auth <token>]."""
        if len(args) < 2:
            print("  Usage: http GET|POST|PUT|DELETE <url> [--json] [--save <path>]")
            return
        method = args[0].upper(); url = args[1]
        save_to = ""; body = None; headers = {}
        i = 2
        while i < len(args):
            if args[i] == "--save" and i+1<len(args):   save_to=args[i+1]; i+=2
            elif args[i] == "--auth" and i+1<len(args):
                headers["Authorization"]=f"Bearer {args[i+1]}"; i+=2
            elif args[i] == "--json" and i+1<len(args):
                import json as _j; body=_j.loads(args[i+1]); i+=2
            else: url = args[i]; i+=1
        client = self.kernel.http
        resp   = client.request(method, url, json_data=body, headers=headers, save_to=save_to)
        status_col = "green" if resp.ok else "red"
        print(f"  {col(str(resp.status), status_col)}  {resp.elapsed_ms:.0f}ms  {url}")
        try:
            import json as _j; data = resp.json()
            print(_j.dumps(data, indent=2)[:1500])
        except Exception:
            print(resp.text[:1000])
        if save_to: self._ok(f"Saved to: {save_to}")

    def _health_cmd(self, args):
        """health — show /health /ready /version status."""
        hs = self.kernel.health
        r  = hs.check_ready()
        all_ok = r["ready"]
        icon = col("●","green") if all_ok else col("●","red")
        print(f"  {icon} Ready: {all_ok}")
        for name, ok in r["subsystems"].items():
            i = col("✓","green") if ok else col("✗","red")
            print(f"    {i} {name}")
        print(f"  Metrics: http://0.0.0.0:{hs.port}/metrics")
        print(f"  Health:  http://0.0.0.0:{hs.port}/health")

    def _vpn_cmd(self, args):
        """vpn start|stop|connect <url>|peers."""
        vpn = self.kernel.vpn
        sub = args[0] if args else "peers"
        if sub == "start":
            msg = vpn.start(); self._ok(msg)
        elif sub == "stop":
            vpn.stop(); self._ok("VPN stopped")
        elif sub == "connect" and len(args)>1:
            ok = vpn.connect(args[1])
            self._ok(f"Connected to {args[1]}") if ok else self._err("Handshake failed")
        elif sub == "peers":
            peers = vpn.peers()
            if not peers: print(col("  No VPN peers.","dim")); return
            for p in peers: print(f"  {col(p['ip'],'cyan'):<16} {p['url']}  age:{p['age_s']}s")
        else: print("  Usage: vpn start|stop|connect <url>|peers")

    def _replicate_cmd(self, args):
        """replicate enable [n]|disable|add-peer <url>|status."""
        rep = self.kernel.replicator
        sub = args[0] if args else "status"
        if sub == "enable":
            n = int(args[1]) if len(args)>1 else 2
            rep.n_replicas = n; rep.start(); self._ok(f"Replication enabled (n={n})")
        elif sub == "disable":
            rep.stop(); self._ok("Replication disabled")
        elif sub == "add-peer" and len(args)>1:
            rep.add_peer(args[1]); self._ok(f"Peer added: {args[1]}")
        elif sub == "status":
            s = rep.status()
            for k,v in s.items(): print(f"  {k:<15} {v}")
        else: print("  Usage: replicate enable [n]|disable|add-peer <url>|status")

    def _ci_cmd(self, args):
        """ci run [job] [--dry-run]|status|history|log <id>|init."""
        ci  = self.kernel.ci
        sub = args[0] if args else "status"
        if sub == "run":
            job     = next((a for a in args[1:] if not a.startswith("--")), None)
            dry_run = "--dry-run" in args
            run     = ci.run(job_filter=job, dry_run=dry_run)
            if dry_run:
                print(col(f"  Dry run — jobs: {list(run.jobs.keys())}","dim"))
                for jname, job_obj in run.jobs.items():
                    for s in job_obj.steps: print(f"    [{jname}] {s.name}: {s.run[:60]}")
            else:
                import time as _t
                print(col(f"  CI run started [{run.run_id}]","dim"))
                # Wait up to 60s
                for _ in range(120):
                    _t.sleep(0.5)
                    if run.status != "running": break
                icon = col("PASSED","green") if run.passed else col("FAILED","red")
                print(f"  {icon}  {run.elapsed_s}s")
                for jname, job_obj in run.jobs.items():
                    ji = col("✓","green") if job_obj.status=="ok" else col("✗","red")
                    print(f"    {ji} {jname:<20} {job_obj.status}  {job_obj.duration:.1f}s")
                    for s in job_obj.steps:
                        si = col("✓","green") if s.status=="ok" else col("✗","red") if s.status=="failed" else col("-","dim")
                        print(f"      {si} {s.name}")
                        if s.output and s.status=="failed": print(f"        {s.output[:200]}")
        elif sub == "status":
            last = ci.last_run()
            if not last: print(col("  No CI runs yet.","dim")); return
            icon = col("PASSED","green") if last.passed else col("FAILED","red")
            print(f"  [{last.run_id}] {icon}  trigger:{last.trigger}  {last.elapsed_s}s")
        elif sub == "history":
            for r in ci.history()[:10]:
                icon = col("✓","green") if r["status"]=="passed" else col("✗","red")
                print(f"  {icon} [{r['run_id']}] {r['status']:<10} {r['elapsed_s']}s  jobs:{list(r.get('jobs',{}).keys())}")
        elif sub == "init":
            path = ci.create_default_config(); self._ok(f"Pipeline config created: {path}")
        else: print("  Usage: ci run [job] [--dry-run]|status|history|init")

    def _bench_cmd(self, args):
        """bench [--sos|--search|--ai|--save] — run performance benchmarks."""
        from tests.bench import BenchmarkSuite
        bs   = BenchmarkSuite(self.kernel)
        cats = []
        save = "--save" in args
        for flag in ("--sos","--search","--ai","--compress"):
            if flag in args: cats.append(flag[2:])
        if not cats: cats = None
        print(col("  Running benchmarks...","dim"))
        results = bs.run_all(categories=cats, save=save)
        bs.print_report(results)
        if save: self._ok("Results saved to SOS")

    def _context_cmd(self, args):
        """context status|clear|compress|pin <n>|save [name]|import <name>."""
        ctx = self.kernel.context
        sub = args[0] if args else "status"
        if sub == "status":
            s = ctx.status()
            for k,v in s.items(): print(f"  {k:<15} {v}")
        elif sub == "clear":
            ctx.clear(); self._ok("Context cleared")
        elif sub == "compress":
            ok = ctx.compress()
            self._ok("Context compressed") if ok else self._err("Compression failed (need AI + 6+ messages)")
        elif sub == "pin" and len(args)>1:
            ok = ctx.pin(int(args[1]))
            self._ok(f"Message {args[1]} pinned") if ok else self._err("Invalid index")
        elif sub == "save":
            name = args[1] if len(args)>1 else ""
            path = ctx.save(name); self._ok(f"Context saved: {path}")
        elif sub == "import" and len(args)>1:
            ok = ctx.load(args[1])
            self._ok(f"Context loaded: {args[1]}") if ok else self._err("Not found")
        else: print("  Usage: context status|clear|compress|pin <n>|save [name]|import <name>")

    def _repl_cmd(self, args):
        """repl [--quiet] — launch the rich Python REPL."""
        quiet = "--quiet" in args
        self.kernel.repl.run(quiet=quiet)

    def _http_cmd(self, args):
        """http GET|POST|PUT|DELETE <url> [--json <body>] [--verbose]."""
        if len(args) < 2: print("  Usage: http GET|POST|PUT|DELETE <url>"); return
        method   = args[0].upper()
        url      = args[1]
        verbose  = "--verbose" in args or "-v" in args
        json_idx = args.index("--json") if "--json" in args else -1
        json_body = None
        if json_idx >= 0 and json_idx+1 < len(args):
            try: json_body = __import__("json").loads(args[json_idx+1])
            except Exception: pass
        client = self.kernel.http
        if method == "GET":     result = client.get(url)
        elif method == "POST":  result = client.post(url, json_body=json_body)
        elif method == "PUT":   result = client.put(url, json_body=json_body)
        elif method == "DELETE": result = client.delete(url)
        else: result = client.request(method, url)
        print(client.format_response(result, verbose=verbose))

    def _bench_cmd(self, args):
        """bench "<expr>" [--n N] [--warmup N] [--compare <expr2>]."""
        if not args: print("  Usage: bench '<expr>' [--n N]"); return
        n = int(args[args.index("--n")+1]) if "--n" in args else 1000
        warmup = int(args[args.index("--warmup")+1]) if "--warmup" in args else 10
        exprs = [a for a in args if not a.startswith("--") and a not in (str(n),str(warmup))]
        bm = self.kernel.bench
        ns = {"kernel":self.kernel,"sos":self.sos}
        if len(exprs) > 1:
            print(bm.compare(*exprs, n=n, ns=ns))
        elif exprs:
            result = bm.run(exprs[0], n=n, warmup=warmup, ns=ns)
            print(result)
        else: print("  No expression given")

    def _lint_cmd(self, args):
        """lint [path] — ruff check."""
        path = args[0] if args else "."
        r = self.kernel.linter.lint(path)
        if "error" in r: self._err(r["error"]); return
        if r["issues"] == 0: self._ok(f"No issues found in {path}")
        else:
            print(r["output"][:2000])
            print(col(f"  {r['issues']} issue(s) found","yellow"))

    def _type_cmd(self, args):
        """type [path] [--ai] — mypy type-check, or AI annotation suggestions."""
        if "--ai" in args:
            path_args = [a for a in args if a != "--ai"]
            rp = self._resolve(path_args[0]) if path_args else self.cwd
            result = self.kernel.linter.suggest_types(rp, self.kernel)
            print(result); return
        path = args[0] if args else "."
        r = self.kernel.linter.typecheck(path)
        if "error" in r: self._err(r["error"]); return
        if r["errors"] == 0 and r["warnings"] == 0: self._ok("No type errors")
        else:
            print(r["output"][:2000])
            e, w = r["errors"], r["warnings"]
            print(col(f"  {e} error(s), {w} warning(s)","red" if e else "yellow"))

    def _tutorial_cmd(self, args):
        """tutorial [N|next] — interactive NOVA tutorial."""
        tut = self.kernel.tutorial
        sub = args[0] if args else None
        if sub == "next": print(tut.next())
        elif sub and sub.isdigit(): print(tut.jump(int(sub)))
        else: print(tut.show())

    def _notify_cmd(self, args):
        """notify "<msg>" [--title T] [--urgency low|normal|critical]."""
        if not args: print("  Usage: notify '<message>'"); return
        msg      = args[0]
        title    = "PyOS NOVA"
        urgency  = "normal"
        if "--title" in args:
            idx = args.index("--title")
            if idx+1 < len(args): title = args[idx+1]
        if "--urgency" in args:
            idx = args.index("--urgency")
            if idx+1 < len(args): urgency = args[idx+1]
        ok = self.kernel.notifier.notify(msg, title=title, urgency=urgency)
        if not ok: self._err("Notification failed")

    def _vpn_cmd(self, args):
        """vpn start [port]|stop|connect <peer> <key>|status."""
        vpn = self.kernel.vpn
        sub = args[0] if args else "status"
        if sub == "start":
            port = int(args[1]) if len(args)>1 and args[1].isdigit() else 51820
            vpn._port = port
            msg = vpn.start(); self._ok(msg)
        elif sub == "stop": vpn.stop(); self._ok("VPN stopped")
        elif sub == "connect" and len(args)>=3:
            vpn.add_peer(args[1], args[2]); self._ok(f"Peer {args[1]} added")
        elif sub == "status":
            s = vpn.status()
            for k,v in s.items(): print(f"  {k:<15} {v}")
        else: print("  Usage: vpn start [port]|stop|connect <ip> <key>|status")

    def _mesh_cmd(self, args):
        """mesh add <n> <url>|list|proxy <n> <path>|status."""
        mesh = self.kernel.mesh
        sub  = args[0] if args else "list"
        if sub == "add" and len(args)>=3:
            mesh.register(args[1], args[2]); self._ok(f"Service {args[1]} registered")
        elif sub == "list":
            svcs = mesh.list_services()
            if not svcs: print(col("  No services in mesh.","dim")); return
            for s in svcs:
                health = col("●","green") if s["healthy"] else col("●","red")
                print(f"  {health} {col(s['name'],'cyan'):<20} {s['url']:<30} {s['requests']} reqs  {s['latency_ms']}ms")
        elif sub == "proxy" and len(args)>=3:
            status, body = mesh.proxy(args[1], args[2])
            print(f"  {status}  {body[:300].decode('utf-8','replace')}")
        else: print("  Usage: mesh add <n> <url>|list|proxy <n> <path>")

    def _mail_cmd(self, args):
        """mail send <to> <subject> <body>|inbox [user]|read <id>."""
        mail = self.kernel.mail
        sub  = args[0] if args else "inbox"
        if sub == "send" and len(args)>=4:
            to   = args[1].split(",")
            subj = args[2]
            body = " ".join(args[3:])
            msg_id = mail.send(self.user+"@nova.local", to, subj, body)
            self._ok(f"Sent [{msg_id}]")
        elif sub == "inbox":
            user = args[1] if len(args)>1 else self.user
            msgs = mail.inbox(user)
            if not msgs: print(col("  Inbox empty.","dim")); return
            for m in msgs:
                read = "" if m.read else col(" [NEW]","cyan")
                ts   = __import__("time").strftime("%m-%d %H:%M",__import__("time").localtime(m.received_at))
                print(f"  {col(m.msg_id[:8],'dim')} {ts}  {col(m.from_addr,'dim'):<25} {m.subject}{read}")
        elif sub == "read" and len(args)>1:
            user = self.user
            msgs = mail.inbox(user)
            for m in msgs:
                if m.msg_id.startswith(args[1]):
                    print(f"  From: {m.from_addr}  Subject: {m.subject}")
                    print(f"  {m.body}")
                    mail.mark_read(user, m.msg_id)
                    break
        else: print("  Usage: mail send <to> <subj> <body>|inbox [user]|read <id>")

    def _stream_cmd(self, args):
        """stream "<prompt>" — streaming token-by-token AI response."""
        if not args: print("  Usage: stream '<prompt>'"); return
        prompt = " ".join(args)
        print()
        self.kernel.stream_ai.stream(prompt)
        print()

    def _loadtest_cmd(self, args):
        """loadtest <url> [--n N] [--c C] — HTTP load test."""
        if not args: print("  Usage: loadtest <url> [--n N] [--c C]"); return
        url = args[0]
        n   = int(args[args.index("--n")+1]) if "--n" in args else 100
        c   = int(args[args.index("--c")+1]) if "--c" in args else 10
        print(col(f"  Load testing {url} ({n} requests, {c} concurrent)...","dim"))
        result = self.kernel.loadtest.run(url, n=n, concurrency=c)
        print(f"  {col('Results','cyan')}:")
        print(f"  Duration:  {result.duration_s}s")
        print(f"  RPS:       {result.rps}")
        print(f"  Success:   {col(str(result.success),'green')}/{result.n_requests}")
        print(f"  Failed:    {col(str(result.failed),'red') if result.failed else '0'}")
        if result.latencies_ms:
            print(f"  Latency p50:  {result.p50:.1f}ms")
            print(f"  Latency p99:  {result.p99:.1f}ms")
            print(f"  Latency mean: {result.mean:.1f}ms")

    def _source_cmd(self, args):
        """source <path> — execute a .nova script from SOS."""
        if not args: print("  Usage: source <path>"); return
        rp = self._resolve(args[0])
        try:
            ctx = self.kernel.script.run_sos_script(rp)
            # Register any defined functions in the shell
            for name, fn in ctx._fns.items():
                self.kernel.shell._cmds[name] = lambda a, _fn=fn: (
                    self.kernel.script._call_fn(_fn, " ".join(a), ctx))
            if ctx._fns:
                self._ok(f"Loaded {len(ctx._fns)} function(s) from {rp}")
        except Exception as e:
            self._err(str(e))

    def _scriptcheck_cmd(self, args):
        """scriptcheck <path> — syntax-check a .nova script."""
        if not args: print("  Usage: scriptcheck <path>"); return
        rp = self._resolve(args[0])
        try:
            src    = self.sos.read(rp)
            errors = self.kernel.script.check_syntax(src)
            if errors:
                for e in errors: print(f"  {col('✗','red')} {e}")
                self._err(f"{len(errors)} error(s)")
            else:
                self._ok(f"No syntax errors in {rp}")
        except Exception as e:
            self._err(str(e))

    def _gpu_cmd(self, args):
        """Gpu cmd.

            Args:
            args: Args.
            """
        sub = args[0] if args else "status"
        if sub == "status":
            try:
                from ai.gpu import gpu_report
                print(gpu_report())
            except Exception as e:
                self._err(str(e))
        elif sub == "install" and len(args) > 1:
            try:
                from ai.gpu import install_gpu_backend
                install_gpu_backend(args[1])
            except Exception as e:
                self._err(str(e))
        else:
            print("  Usage: gpu status | gpu install <cuda|rocm|metal|vulkan|cpu>")

    def _vbox_cmd(self, args):
        """Vbox cmd.

            Args:
            args: Args.
            """
        sub = args[0] if args else "help"
        if sub == "build":
            try:
                from build.vbox_builder import build_ova
                import os
                out = os.path.join(os.environ.get("NOVA_DATA","~/.nova"), "nova_vbox.ova")
                build_ova(out)
            except Exception as e:
                self._err(str(e))
        else:
            print("  VirtualBox integration:")
            print("  vbox build  — rebuild OVA for import")
            print()
            print("  Quick GPU setup:")
            print("  Settings → Display → VMSVGA, VRAM 16MB+")
            print("  For GPU passthrough: VBoxManage modifyvm 'PyOS NOVA' --accelerate3d on")

    def _helix(self, args):
        """Helix.

            Args:
            args: Args.
            """
        from apps.helix import open_editor
        path = self._resolve(args[0]) if args else None
        if path and not self.sos.exists(path):
            self.sos.write(path, "")
        try:
            open_editor(path if (path and os.path.exists(path)) else None, kernel=self.kernel)
        except Exception as e:
            self._err(f"helix: {e}")

    def _setup(self, args):
        """Set up the operation.

            Args:
            args: Args.
            """
        from apps.setup_wizard import run_setup
        run_setup(kernel=self.kernel)

    def _reboot_cmd(self, args):
        """Reboot cmd.

            Args:
            args: Args.
            """
        print(col("\n  Rebooting PyOS NOVA...", "cyan"))
        import time; time.sleep(0.5)
        try:
            import ctypes
            ctypes.CDLL("libc.so.6").reboot(0x1234567)
        except Exception:
            self._exit = True; self._reboot = True

    def _halt_cmd(self, args):
        """Halt cmd.

            Args:
            args: Args.
            """
        print(col("\n  Halting PyOS NOVA...", "cyan"))
        import time; time.sleep(0.5)
        try:
            import ctypes
            ctypes.CDLL("libc.so.6").reboot(0x4321fedc)
        except Exception:
            self._exit = True

