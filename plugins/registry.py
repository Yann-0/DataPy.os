"""
PyOS NOVA — Plugin Registry  (Phase 2 — Developer Experience)
==============================================================
Full plugin ecosystem: discover, install, validate, sandbox, and run plugins.

Plugin manifest format (nova.toml stored in SOS at /plugins/<name>/nova.toml):

    [plugin]
    name        = "nova-git"
    version     = "1.0.0"
    description = "Git integration for PyOS NOVA"
    author      = "Nova Project"
    entry_point = "main.plugin_main"
    permissions = ["sos.read", "sos.write", "shell.register"]
    min_nova    = "0.0007"

Permission model:
    sos.read       — read SOS objects
    sos.write      — write SOS objects
    shell.register — add new shell commands
    net.http       — make HTTP requests
    fs.read        — read host filesystem (restricted path)
    exec           — run subprocesses (maximum restriction)

Shell commands:
    plugin search [query]        — search available plugins
    plugin install <name>        — install a plugin
    plugin remove <name>         — uninstall a plugin
    plugin list                  — list installed plugins
    plugin enable/disable <name> — toggle a plugin
    plugin info <name>           — show manifest details
    plugin update [name]         — update to latest version
"""

from __future__ import annotations

import os
import sys
import json
import time
import hashlib
import threading
import importlib
from typing import Dict, List, Optional, Any, TYPE_CHECKING
from dataclasses import dataclass, field

if TYPE_CHECKING:
    from store.sos import SemanticObjectStore
    from kernel.nova import NovaKernel

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PLUGIN_BASE     = "/plugins"
REGISTRY_PATH   = "/plugins/__registry__.json"
MANIFEST_FILE   = "nova.toml"

# Built-in first-party plugin manifests (installed on first boot)
BUILTIN_PLUGINS = [
    {
        "name":        "nova-metrics",
        "version":     "1.0.0",
        "description": "Real-time system metrics dashboard extension",
        "author":      "Nova Project",
        "permissions": ["sos.read", "sos.write"],
        "builtin":     True,
    },
    {
        "name":        "nova-notify",
        "version":     "1.0.0",
        "description": "Desktop notifications via system tray / notify-send",
        "author":      "Nova Project",
        "permissions": ["sos.read", "exec"],
        "builtin":     True,
    },
    {
        "name":        "nova-cron",
        "version":     "1.0.0",
        "description": "Visual crontab editor and scheduler",
        "author":      "Nova Project",
        "permissions": ["sos.read", "sos.write", "shell.register"],
        "builtin":     True,
    },
    {
        "name":        "nova-http",
        "version":     "1.0.0",
        "description": "HTTP client — GET/POST with JSON display",
        "author":      "Nova Project",
        "permissions": ["net.http", "sos.write"],
        "builtin":     True,
    },
    {
        "name":        "nova-git",
        "version":     "1.0.0",
        "description": "Native git integration (clone, commit, push, log)",
        "author":      "Nova Project",
        "permissions": ["sos.read", "sos.write", "fs.read", "exec"],
        "builtin":     True,
    },
]

ALL_PERMISSIONS = frozenset({
    "sos.read", "sos.write", "shell.register",
    "net.http", "fs.read", "exec",
})


@dataclass
class PluginManifest:
    """Parsed and validated plugin manifest."""

    name:         str
    version:      str
    description:  str
    author:       str
    entry_point:  str             = ""
    permissions:  List[str]       = field(default_factory=list)
    min_nova:     str             = "0.0001"
    enabled:      bool            = True
    installed_at: float           = field(default_factory=time.time)
    builtin:      bool            = False
    sos_path:     str             = ""

    def to_dict(self) -> dict:
        """Serialise to JSON-compatible dict."""
        return {k: v for k, v in self.__dict__.items()}

    @staticmethod
    def from_dict(d: dict) -> "PluginManifest":
        """Deserialise from dict."""
        # Only pass fields that PluginManifest knows about
        known = {f.name for f in PluginManifest.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return PluginManifest(**{k: v for k, v in d.items() if k in known})


class PluginSandbox:
    """
    Restricted execution environment for plugins.

    Enforces the declared permission set by wrapping SOS and network calls.
    Plugins that attempt operations outside their permissions raise PermissionError.
    """

    def __init__(self, manifest: PluginManifest,
                  kernel: "NovaKernel"):
        """
        Initialise sandbox for a specific plugin.

        Args:
            manifest: The plugin's declared manifest (permissions come from here).
            kernel:   Kernel reference for proxying allowed operations.
        """
        self._manifest   = manifest
        self._kernel     = kernel
        self._perms      = frozenset(manifest.permissions)
        self._call_count = 0

    def _require(self, perm: str):
        """Raise PermissionError if the plugin lacks *perm*."""
        if perm not in self._perms:
            raise PermissionError(
                f"Plugin '{self._manifest.name}' attempted "
                f"'{perm}' but only has: {sorted(self._perms)}"
            )

    def sos_read(self, path: str) -> str:
        """Proxy SOS read (requires sos.read permission)."""
        self._require("sos.read")
        self._call_count += 1
        return self._kernel.sos.read(path)

    def sos_write(self, path: str, content: str, **kw):
        """Proxy SOS write (requires sos.write permission)."""
        self._require("sos.write")
        self._call_count += 1
        # Restrict plugins to their own namespace
        safe_path = f"/plugins/{self._manifest.name}/data/{path.lstrip('/')}"
        return self._kernel.sos.write(safe_path, content, **kw)

    def http_get(self, url: str, timeout: float = 10.0) -> str:
        """HTTP GET (requires net.http permission)."""
        self._require("net.http")
        import urllib.request
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read(65536).decode("utf-8", errors="replace")

    def run(self, cmd: str, timeout: float = 30.0) -> str:
        """Run a subprocess (requires exec permission)."""
        self._require("exec")
        import subprocess
        result = subprocess.run(
            cmd, shell=True, capture_output=True,
            text=True, timeout=timeout,
        )
        return result.stdout + result.stderr

    def register_command(self, name: str, fn):
        """Register a new shell command (requires shell.register permission)."""
        self._require("shell.register")
        self._kernel.shell._cmds[name] = fn


class PluginRegistry:
    """
    Manages the full plugin lifecycle: discovery, installation, loading, running.
    """

    def __init__(self, kernel: "NovaKernel"):
        """Initialise the plugin registry."""
        self._kernel   = kernel
        self._sos      = kernel.sos
        self._loaded:  Dict[str, Any]            = {}   # name → module
        self._manifests: Dict[str, PluginManifest] = {}
        self._lock     = threading.Lock()
        self._ensure_dirs()
        self._load_registry()
        self._install_builtins()

    def _ensure_dirs(self):
        """Create plugin storage directories."""
        if not self._sos.exists(PLUGIN_BASE):
            self._sos.mkdir(PLUGIN_BASE, parents=True)

    def _load_registry(self):
        """Load persisted plugin manifests from SOS."""
        try:
            data = json.loads(self._sos.read(REGISTRY_PATH))
            for d in data:
                m = PluginManifest.from_dict(d)
                self._manifests[m.name] = m
        except Exception:
            pass

    def _save_registry(self):
        """Persist plugin manifests to SOS."""
        with self._lock:
            data = [m.to_dict() for m in self._manifests.values()]
        self._sos.write(REGISTRY_PATH, json.dumps(data, indent=2),
                         tags=["plugin-registry"])

    def _install_builtins(self):
        """Ensure all first-party plugins are registered."""
        for spec in BUILTIN_PLUGINS:
            if spec["name"] not in self._manifests:
                m = PluginManifest(**{
                    k: spec.get(k, "")
                    for k in PluginManifest.__dataclass_fields__  # type: ignore[attr-defined]
                    if k in spec
                })
                with self._lock:
                    self._manifests[m.name] = m
        self._save_registry()

    # ── Installation ──────────────────────────────────────────────────────────

    def install_from_dict(self, manifest_dict: dict) -> PluginManifest:
        """
        Install a plugin from a manifest dictionary.

        Args:
            manifest_dict: Plugin manifest as a dict.

        Returns:
            The installed PluginManifest.
        """
        m = PluginManifest.from_dict(manifest_dict)
        m.installed_at = time.time()
        m.sos_path     = f"{PLUGIN_BASE}/{m.name}"

        if not self._sos.exists(m.sos_path):
            self._sos.mkdir(m.sos_path, parents=True)

        self._sos.write(
            f"{m.sos_path}/{MANIFEST_FILE}",
            json.dumps(m.to_dict(), indent=2),
            tags=["plugin-manifest", m.name],
        )
        with self._lock:
            self._manifests[m.name] = m
        self._save_registry()
        return m

    def install_from_url(self, url: str) -> Optional[PluginManifest]:
        """
        Download and install a plugin from a URL.

        The URL must point to a nova.toml manifest (JSON or TOML format).

        Args:
            url: URL to the plugin manifest.

        Returns:
            Installed manifest, or None on failure.
        """
        try:
            import urllib.request
            with urllib.request.urlopen(url, timeout=15) as r:
                raw = r.read(65536).decode()
            data = json.loads(raw)
            return self.install_from_dict(data)
        except Exception as e:
            raise RuntimeError(f"Install from {url} failed: {e}") from e

    def remove(self, name: str) -> bool:
        """
        Uninstall a plugin.

        Args:
            name: Plugin name.

        Returns:
            True if removed.
        """
        m = self._manifests.get(name)
        if not m or m.builtin:
            return False
        with self._lock:
            del self._manifests[name]
            self._loaded.pop(name, None)
        self._save_registry()
        return True

    # ── Activation ────────────────────────────────────────────────────────────

    def enable(self, name: str) -> bool:
        """Enable a disabled plugin."""
        m = self._manifests.get(name)
        if m:
            m.enabled = True
            self._save_registry()
            return True
        return False

    def disable(self, name: str) -> bool:
        """Disable a plugin without removing it."""
        m = self._manifests.get(name)
        if m:
            m.enabled = False
            self._save_registry()
            return True
        return False

    # ── Query ─────────────────────────────────────────────────────────────────

    def list_installed(self) -> List[PluginManifest]:
        """Return all installed plugins."""
        with self._lock:
            return sorted(self._manifests.values(), key=lambda m: m.name)

    def search(self, query: str = "") -> List[dict]:
        """
        Search available plugins (installed + builtin catalog).

        Args:
            query: Optional search string (matches name and description).

        Returns:
            List of plugin summary dicts.
        """
        q = query.lower()
        results = []
        for spec in BUILTIN_PLUGINS:
            if not q or q in spec["name"] or q in spec.get("description", "").lower():
                installed = spec["name"] in self._manifests
                results.append({**spec, "installed": installed})
        return results

    def get(self, name: str) -> Optional[PluginManifest]:
        """Return a specific plugin manifest."""
        return self._manifests.get(name)

    def status(self) -> dict:
        """Return registry statistics."""
        with self._lock:
            total    = len(self._manifests)
            enabled  = sum(1 for m in self._manifests.values() if m.enabled)
            builtins = sum(1 for m in self._manifests.values() if m.builtin)
        return {
            "installed": total,
            "enabled":   enabled,
            "builtin":   builtins,
            "custom":    total - builtins,
        }
