"""
PyOS NOVA — Plan 9-Style Namespace Mounts
==========================================
Each NOVA process sees its own view of the SOS, constructed by
mounting paths from different places.

Inspired by Plan 9's revolutionary namespace model:
  - bind /remote/nova /home/root  → remote path appears local
  - mount --union /a /b           → /b shows union of /a and /b
  - Process A and Process B see different filesystems
  - Sandbox gets a stripped-down namespace with only what it needs

Namespace table: list of (bind_src, bind_dst, mode) entries.
Lookup: check namespace table before the canonical SOS alias table.

Modes:
  before  — namespace entry shadows canonical SOS
  after   — fallback to namespace if not in canonical
  replace — namespace completely replaces the canonical path
  union   — merge both namespaces (canonical first)

Shell commands:
  bind <src> <dst> [--before|--after|--replace|--union]
  unbind <dst>
  namespace           — show current namespace bindings
  namespace new       — create a new empty namespace
  namespace clone     — clone current namespace (for sandboxes)
"""

from __future__ import annotations
import os, sys, threading
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING
from dataclasses import dataclass, field
from enum import Enum

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from store.sos import SemanticObjectStore


class BindMode(Enum):
    """Namespace binding mode."""
    BEFORE  = "before"   # shadow canonical
    AFTER   = "after"    # fallback
    REPLACE = "replace"  # completely replace
    UNION   = "union"    # merge both


@dataclass
class BindEntry:
    """One namespace binding: src mounted at dst."""
    src:     str         # real SOS path (source)
    dst:     str         # virtual path (destination)
    mode:    BindMode    = BindMode.BEFORE
    created: float       = field(default_factory=lambda: __import__("time").time())

    def __repr__(self):
        """Return human-readable representation."""
        return f"bind {self.src!r} {self.dst!r} ({self.mode.value})"


class Namespace:
    """
    A per-process view of the SOS constructed by bind mounts.

    Each process gets a namespace that overlays the canonical
    SOS alias table with custom bindings.
    """

    def __init__(self, ns_id: str,
                  sos: "SemanticObjectStore",
                  parent: Optional["Namespace"] = None):
        """Initialise a namespace."""
        self.ns_id   = ns_id
        self.sos     = sos
        self.parent  = parent
        self._binds: List[BindEntry] = []
        self._lock   = threading.RLock()
        # Clone parent bindings
        if parent:
            with parent._lock:
                self._binds = list(parent._binds)

    def bind(self, src: str, dst: str,
              mode: BindMode = BindMode.BEFORE):
        """
        Mount src at dst in this namespace.

        Args:
            src (str): Real SOS path to mount.
            dst (str): Virtual destination path.
            mode (BindMode): How to handle conflicts.
        """
        with self._lock:
            # Remove any existing binding for this dst
            self._binds = [b for b in self._binds if b.dst != dst]
            self._binds.append(BindEntry(src=src, dst=dst, mode=mode))

    def unbind(self, dst: str) -> bool:
        """
        Remove the binding for dst.

        Args:
            dst (str): Destination path to unbind.

        Returns:
            bool: True if a binding was found and removed.
        """
        with self._lock:
            before = len(self._binds)
            self._binds = [b for b in self._binds if b.dst != dst]
            return len(self._binds) < before

    def resolve(self, path: str) -> Optional[str]:
        """
        Resolve a virtual path to a real SOS path.

        Checks namespace bindings before the canonical alias table.

        Args:
            path (str): Virtual path to resolve.

        Returns:
            str: The resolved real SOS OID, or None if not found.
        """
        with self._lock:
            binds = list(self._binds)

        # Check BEFORE / REPLACE bindings first
        for bind in binds:
            if bind.mode in (BindMode.BEFORE, BindMode.REPLACE):
                mapped = self._map_path(path, bind)
                if mapped:
                    oid = self.sos.resolve(mapped)
                    if oid:
                        return oid

        # Check canonical SOS
        canonical_oid = self.sos.resolve(path)
        if canonical_oid:
            return canonical_oid

        # Check AFTER / UNION bindings as fallback
        for bind in binds:
            if bind.mode in (BindMode.AFTER, BindMode.UNION):
                mapped = self._map_path(path, bind)
                if mapped:
                    oid = self.sos.resolve(mapped)
                    if oid:
                        return oid

        return None

    def listdir(self, path: str) -> List[str]:
        """
        List a directory, merging namespace bindings.

        UNION mode merges contents from both src and canonical.

        Args:
            path (str): Directory path to list.

        Returns:
            List[str]: Combined directory entries.
        """
        entries = set()

        # Namespace-bound directories (BEFORE/REPLACE)
        with self._lock:
            binds = list(self._binds)
        for bind in binds:
            if bind.mode in (BindMode.BEFORE, BindMode.REPLACE):
                mapped = self._map_path(path, bind, dir_match=True)
                if mapped:
                    try:
                        for name in self.sos.listdir(mapped):
                            entries.add(name)
                    except Exception:
                        pass
            if bind.mode == BindMode.REPLACE:
                return sorted(entries)

        # Canonical directory
        try:
            for name in self.sos.listdir(path):
                entries.add(name)
        except Exception:
            pass

        # AFTER/UNION bindings
        for bind in binds:
            if bind.mode in (BindMode.AFTER, BindMode.UNION):
                mapped = self._map_path(path, bind, dir_match=True)
                if mapped:
                    try:
                        for name in self.sos.listdir(mapped):
                            entries.add(name)
                    except Exception:
                        pass

        return sorted(entries)

    def read(self, path: str) -> str:
        """
        Read a file through the namespace.

        Args:
            path (str): Virtual path to read.

        Returns:
            str: File content.

        Raises:
            FileNotFoundError: If path is not found in any binding.
        """
        oid = self.resolve(path)
        if oid:
            obj = self.sos.get(oid)
            if obj:
                return obj.text
        raise FileNotFoundError(f"No such file: {path}")

    def _map_path(self, path: str, bind: BindEntry,
                   dir_match: bool = False) -> Optional[str]:
        """
        Map a virtual path through a bind entry.

        Args:
            path (str): Virtual path.
            bind (BindEntry): The binding to apply.
            dir_match (bool): If True, match directory prefix.

        Returns:
            str: The mapped real path, or None if binding doesn't apply.
        """
        dst = bind.dst.rstrip("/")
        src = bind.src.rstrip("/")

        if dir_match:
            if path.startswith(dst + "/") or path == dst:
                suffix = path[len(dst):]
                return src + suffix
            return None

        if path == dst:
            return src
        if path.startswith(dst + "/"):
            return src + "/" + path[len(dst)+1:]
        return None

    def clone(self, new_id: str) -> "Namespace":
        """
        Create a copy of this namespace with the same bindings.

        Args:
            new_id (str): ID for the new namespace.

        Returns:
            Namespace: The cloned namespace.
        """
        return Namespace(new_id, self.sos, parent=self)

    def bindings(self) -> List[dict]:
        """Return all current bindings as dicts."""
        with self._lock:
            return [{"src": b.src, "dst": b.dst, "mode": b.mode.value}
                    for b in self._binds]


class NamespaceManager:
    """
    Manages namespaces for all NOVA processes.

    Thread-local storage maps process/thread IDs to namespaces.
    """

    def __init__(self, sos: "SemanticObjectStore"):
        """Initialise the namespace manager."""
        self.sos         = sos
        self._root_ns    = Namespace("root", sos)
        self._namespaces: Dict[str, Namespace] = {"root": self._root_ns}
        self._thread_ns  = threading.local()  # thread → namespace_id
        self._lock       = threading.Lock()

    def current(self) -> Namespace:
        """Return the namespace for the current thread."""
        ns_id = getattr(self._thread_ns, "ns_id", "root")
        return self._namespaces.get(ns_id, self._root_ns)

    def set_current(self, ns_id: str):
        """Set the namespace for the current thread."""
        self._thread_ns.ns_id = ns_id

    def create(self, ns_id: str, clone_from: str = "root") -> Namespace:
        """
        Create a new namespace.

        Args:
            ns_id (str): Unique ID for the new namespace.
            clone_from (str): Namespace to clone bindings from.

        Returns:
            Namespace: The new namespace.
        """
        parent = self._namespaces.get(clone_from, self._root_ns)
        ns     = Namespace(ns_id, self.sos, parent=parent)
        with self._lock:
            self._namespaces[ns_id] = ns
        return ns

    def bind(self, src: str, dst: str,
              mode: BindMode = BindMode.BEFORE,
              ns_id: str = None):
        """Bind src to dst in the specified (or current) namespace."""
        ns = self._namespaces.get(ns_id, self.current())
        ns.bind(src, dst, mode)

    def unbind(self, dst: str, ns_id: str = None) -> bool:
        """Remove a binding from the specified (or current) namespace."""
        ns = self._namespaces.get(ns_id, self.current())
        return ns.unbind(dst)

    def list_namespaces(self) -> List[str]:
        """Return all namespace IDs."""
        return list(self._namespaces.keys())

    def patch_sos(self):
        """
        Patch SOS resolve() and listdir() to consult the current namespace first.
        """
        mgr       = self
        orig_res  = self.sos.resolve
        orig_ls   = self.sos.listdir
        orig_read = self.sos.read

        def _ns_resolve(path):
            ns  = mgr.current()
            oid = ns.resolve(path)
            return oid if oid else orig_res(path)

        def _ns_listdir(path):
            ns = mgr.current()
            if ns.ns_id != "root" and ns._binds:
                return ns.listdir(path)
            return orig_ls(path)

        self.sos.resolve = _ns_resolve
        self.sos.listdir = _ns_listdir
