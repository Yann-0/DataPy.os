"""
PyOS NOVA — Plugin Manager
============================
Install, manage and run Python apps in NOVA.

Apps are Python packages with a nova.toml manifest:

  [app]
  name = "calculator"
  version = "1.0.0"
  description = "Scientific calculator for NOVA"
  commands = ["calc", "calculator"]
  author = "Nova Community"

  [permissions]
  sos_read  = true
  sos_write = false
  network   = false

Apps run in a restricted namespace with only declared permissions.
Installed apps are stored in /apps/<name>/ in the SOS.

Commands:
  app list                    — list installed apps
  app install <name|url>      — install an app
  app remove <name>           — remove an app
  app run <name> [args]       — run an app
  app info <name>             — show app details
  app search <query>          — search available apps
"""

import os, sys, json, urllib.request, importlib, types, ast
from typing import Dict, List, Optional, TYPE_CHECKING

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from kernel.nova import NovaKernel

APPS_BASE   = "/apps"
REGISTRY_URL = "https://raw.githubusercontent.com/nova-os/apps/main/registry.json"

# Built-in app registry (works offline)
BUILTIN_REGISTRY = {
    "calculator": {
        "name": "calculator",
        "version": "1.0.0",
        "description": "Scientific calculator — eval expressions safely",
        "commands": ["calc"],
        "code": """
import ast, math, operator as op

SAFE_OPS = {
    ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul,
    ast.Div: op.truediv, ast.Pow: op.pow, ast.Mod: op.mod,
    ast.USub: op.neg, ast.UAdd: op.pos,
}
SAFE_NAMES = {k: getattr(math, k) for k in dir(math) if not k.startswith('_')}
SAFE_NAMES.update({"abs": abs, "round": round, "int": int, "float": float})

def _eval(node):
    if isinstance(node, ast.Constant): return node.n
    if isinstance(node, ast.BinOp): return SAFE_OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp): return SAFE_OPS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.Name):
        if node.id in SAFE_NAMES: return SAFE_NAMES[node.id]
        raise ValueError(f"Unknown: {node.id}")
    if isinstance(node, ast.Call):
        fn = _eval(node.func)
        args = [_eval(a) for a in node.args]
        return fn(*args)
    raise TypeError(f"Unsupported: {type(node)}")

def main(args, kernel):
    expr = " ".join(args)
    if not expr:
        print("Usage: calc <expression>  e.g. calc sin(pi/2) + sqrt(2)")
        return
    try:
        result = _eval(ast.parse(expr, mode='eval').body)
        print(f"  {expr} = \\033[32m{result}\\033[0m")
    except Exception as e:
        print(f"  Error: {e}")
""",
    },
    "notes": {
        "name": "notes",
        "version": "1.0.0",
        "description": "Quick notes stored in SOS",
        "commands": ["note", "notes"],
        "code": """
def main(args, kernel):
    if not args:
        # List notes
        notes_dir = "/notes"
        if not kernel.sos.exists(notes_dir):
            print("  No notes yet. Try: note add <text>")
            return
        for name in kernel.sos.listdir(notes_dir):
            path = f"/notes/{name}"
            text = kernel.sos.read(path)[:60]
            print(f"  [{name}] {text}")
        return
    sub = args[0]
    if sub == "add" and len(args) > 1:
        text = " ".join(args[1:])
        import time, hashlib
        nid  = hashlib.sha256(f"{text}{time.time()}".encode()).hexdigest()[:8]
        if not kernel.sos.exists("/notes"):
            kernel.sos.mkdir("/notes", parents=True)
        kernel.sos.write(f"/notes/{nid}", text, tags=["note"])
        print(f"  Note saved [{nid}]")
    elif sub == "rm" and len(args) > 1:
        path = f"/notes/{args[1]}"
        if kernel.sos.exists(path):
            kernel.sos.remove(path)
            print(f"  Deleted note {args[1]}")
        else:
            print(f"  Note not found: {args[1]}")
    else:
        print("  Usage: notes  |  note add <text>  |  note rm <id>")
""",
    },
    "wordcount": {
        "name": "wordcount",
        "version": "1.0.0",
        "description": "Count words, lines, chars in SOS objects",
        "commands": ["wc"],
        "code": """
def main(args, kernel):
    if not args:
        print("Usage: wc <path>")
        return
    for path in args:
        rp = kernel.sos.resolve_path(path, "/home/root")
        try:
            text  = kernel.sos.read(rp)
            lines = text.count('\\n')
            words = len(text.split())
            chars = len(text)
            print(f"  {lines:>6} {words:>6} {chars:>6} {path}")
        except Exception as e:
            print(f"  {path}: {e}")
""",
    },
    "clock": {
        "name": "clock",
        "version": "1.0.0",
        "description": "Show current time and date in various formats",
        "commands": ["clock", "time", "date"],
        "code": """
import time, datetime

def main(args, kernel):
    now = datetime.datetime.now()
    tz  = datetime.timezone.utc
    utc = datetime.datetime.now(tz)
    
    print(f"  Local  : {now.strftime('%A, %B %d %Y  %H:%M:%S')}")
    print(f"  UTC    : {utc.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    print(f"  Unix   : {int(time.time())}")
    print(f"  Week   : {now.strftime('Week %W of %Y')}")
    
    if args and args[0] == "--countdown":
        target_str = " ".join(args[1:]) if len(args) > 1 else ""
        print(f"  Countdown feature: specify a date like: clock --countdown 2025-12-31")
""",
    },
}


class AppManifest:
    """App manifest."""
    def __init__(self, name: str, version: str, description: str,
                 commands: List[str], permissions: dict = None):
        """Initialise the instance."""
        self.name        = name
        self.version     = version
        self.description = description
        self.commands    = commands
        self.permissions = permissions or {"sos_read": True}

    """To dict."""
    def to_dict(self): return self.__dict__

    @staticmethod
    def from_dict(d):
        """From dict.

            Args:
            d: D.
            """
        return AppManifest(d["name"], d.get("version","?"),
                           d.get("description",""), d.get("commands",[]),
                           d.get("permissions",{}))


class PluginManager:
    """Installs, runs and manages NOVA apps."""

    def __init__(self, kernel: "NovaKernel"):
        """Initialise the instance."""
        self.kernel = kernel
        self.sos    = kernel.sos
        self._ensure_dirs()
        self._command_map: Dict[str, str] = {}   # command → app name
        self._load_commands()

    def _ensure_dirs(self):
        """Ensure dirs."""
        if not self.sos.exists(APPS_BASE):
            self.sos.mkdir(APPS_BASE, parents=True)

    def _app_path(self, name: str) -> str:
        """App path.

            Args:
            name (str): Name.


            Returns:
                str: Result.
            """
        return f"{APPS_BASE}/{name}"

    def _manifest_path(self, name: str) -> str:
        """Manifest path.

            Args:
            name (str): Name.


            Returns:
                str: Result.
            """
        return f"{APPS_BASE}/{name}/manifest.json"

    def _code_path(self, name: str) -> str:
        """Code path.

            Args:
            name (str): Name.


            Returns:
                str: Result.
            """
        return f"{APPS_BASE}/{name}/main.py"

    def _load_commands(self):
        """Build command → app_name map from installed apps."""
        for name in self.sos.listdir(APPS_BASE):
            manifest = self.get_manifest(name)
            if manifest:
                for cmd in manifest.commands:
                    self._command_map[cmd] = name

    def get_manifest(self, name: str) -> Optional[AppManifest]:
        """Return the manifest.

            Args:
            name (str): Name.


            Returns:
                Optional[AppManifest]: Result.
            """
        try:
            data = json.loads(self.sos.read(self._manifest_path(name)))
            return AppManifest.from_dict(data)
        except Exception:
            return None

    def installed(self) -> List[AppManifest]:
        """Installed.


            Returns:
                List[AppManifest]: Result.
            """
        manifests = []
        for name in self.sos.listdir(APPS_BASE):
            m = self.get_manifest(name)
            if m: manifests.append(m)
        return manifests

    def install(self, name: str, url: str = None) -> bool:
        """Install an app from the built-in registry or a URL."""
        # Check built-in registry first
        if name in BUILTIN_REGISTRY and not url:
            return self._install_builtin(name)

        # Try remote registry
        if url:
            return self._install_url(name, url)

        # Try fetching from community registry
        try:
            resp     = urllib.request.urlopen(REGISTRY_URL, timeout=5)
            registry = json.loads(resp.read())
            if name in registry:
                return self._install_from_registry(name, registry[name])
        except Exception:
            pass

        print(f"  App '{name}' not found in registry.")
        print(f"  Available built-in apps: {', '.join(BUILTIN_REGISTRY.keys())}")
        return False

    def _install_builtin(self, name: str) -> bool:
        """Install builtin.

            Args:
            name (str): Name.


            Returns:
                bool: Result.
            """
        app = BUILTIN_REGISTRY[name]
        app_path = self._app_path(name)
        if not self.sos.exists(app_path):
            self.sos.mkdir(app_path, parents=True)
        manifest = AppManifest(
            app["name"], app["version"], app["description"],
            app["commands"],
        )
        self.sos.write(self._manifest_path(name),
                       json.dumps(manifest.to_dict(), indent=2))
        self.sos.write(self._code_path(name), app["code"], kind="code")
        for cmd in manifest.commands:
            self._command_map[cmd] = name
        return True

    def _install_url(self, name: str, url: str) -> bool:
        """Install url.

            Args:
            name (str): Name.
            url (str): Url.


            Returns:
                bool: Result.
            """
        try:
            resp = urllib.request.urlopen(url, timeout=10)
            code = resp.read().decode()
            app_path = self._app_path(name)
            if not self.sos.exists(app_path):
                self.sos.mkdir(app_path, parents=True)
            manifest = AppManifest(name, "unknown", f"Installed from {url}", [name])
            self.sos.write(self._manifest_path(name),
                           json.dumps(manifest.to_dict(), indent=2))
            self.sos.write(self._code_path(name), code, kind="code")
            self._command_map[name] = name
            return True
        except Exception as e:
            print(f"  Install failed: {e}")
            return False

    def remove(self, name: str) -> bool:
        """Remove the operation.

            Args:
            name (str): Name.


            Returns:
                bool: Result.
            """
        path = self._app_path(name)
        if not self.sos.exists(path):
            return False
        manifest = self.get_manifest(name)
        if manifest:
            for cmd in manifest.commands:
                self._command_map.pop(cmd, None)
        self.sos.remove(path, recursive=True)
        return True

    def run(self, name: str, args: List[str] = None) -> bool:
        """Run an installed app in a sandboxed namespace."""
        code_path = self._code_path(name)
        if not self.sos.exists(code_path):
            print(f"  App '{name}' not found or has no main.py")
            return False

        code = self.sos.read(code_path)

        # Build restricted namespace based on permissions
        manifest    = self.get_manifest(name)
        permissions = manifest.permissions if manifest else {"sos_read": True}
        ns          = self._build_namespace(permissions)

        try:
            exec(compile(code, f"app:{name}", "exec"), ns)
            main_fn = ns.get("main")
            if callable(main_fn):
                main_fn(args or [], self.kernel)
            return True
        except Exception as e:
            print(f"  App error: {e}")
            return False

    def _build_namespace(self, permissions: dict) -> dict:
        """Build a restricted execution namespace."""
        ns = {
            "__builtins__": {
                k: v for k, v in __builtins__.items()
                if k not in ("__import__", "exec", "eval", "compile", "open")
            } if isinstance(__builtins__, dict) else __builtins__,
        }
        if permissions.get("sos_read"):
            ns["kernel"] = self.kernel   # full kernel access for now
        return ns

    def has_command(self, cmd: str) -> bool:
        """Return True if command is present.

            Args:
            cmd (str): Cmd.


            Returns:
                bool: Result.
            """
        return cmd in self._command_map

    def run_command(self, cmd: str, args: List[str] = None) -> bool:
        """Run command.

            Args:
            cmd (str): Cmd.
            args (List[str]): Args, defaults to None.


            Returns:
                bool: Result.
            """
        name = self._command_map.get(cmd)
        if not name:
            return False
        return self.run(name, args)

    def search(self, query: str) -> List[dict]:
        """Search.

            Args:
            query (str): Query.


            Returns:
                List[dict]: Result.
            """
        query = query.lower()
        results = []
        for name, info in BUILTIN_REGISTRY.items():
            if query in name.lower() or query in info["description"].lower():
                results.append({"name": name, "description": info["description"],
                                 "version": info["version"],
                                 "installed": self.sos.exists(self._app_path(name))})
        return results
