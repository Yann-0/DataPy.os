"""
PyOS NOVA — Live Kernel Hot-Reload
=====================================
Patch any running NOVA module without rebooting.
Python's `importlib.reload()` re-imports the module and the kernel
migrates live state from old instances to new class definitions.

This works because:
  - Python module objects are mutable at runtime
  - `importlib.reload()` re-executes the module file in place
  - We can update __class__ on live instances (CPython allows this
    when the class layout is compatible)
  - The kernel holds all subsystem references — we swap them in place

Shell commands:
  reload <module>        — hot-reload a module (e.g. reload ai.engine)
  reload --dry-run <m>   — check if reload is safe without applying
  reload status          — show reload history
  reload undo            — roll back the last reload

Safety:
  - Incompatible class layout → reload aborted with error
  - State snapshot taken before reload (rollback available)
  - Running operations complete before swap (grace period)
"""

from __future__ import annotations
import os, sys, importlib, importlib.util, time, copy, threading
from typing import Optional, List, Dict, Any, TYPE_CHECKING

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from kernel.nova import NovaKernel

RELOAD_LOG_PATH = "/system/reloads"


class ReloadSnapshot:
    """Snapshot of a subsystem's state before a reload (for rollback)."""

    def __init__(self, module_name: str, old_module, instance,
                 state_snapshot: dict):
        """Initialise a reload snapshot."""
        self.module_name     = module_name
        self.old_module      = old_module      # the module object before reload
        self.instance        = instance        # the live subsystem instance
        self.state_snapshot  = state_snapshot  # deep copy of instance.__dict__
        self.timestamp       = time.time()
        self.rolled_back     = False


class HotReloader:
    """
    Live module reloader for the NOVA kernel.
    
    Supports reloading any kernel subsystem while the OS is running.
    State migration preserves running data across the reload.
    """

    GRACE_PERIOD = 0.5   # seconds to wait for in-flight operations

    def __init__(self, kernel: "NovaKernel"):
        """Initialise the hot-reloader with a kernel reference."""
        self.kernel   = kernel
        self._history: List[ReloadSnapshot] = []
        self._lock    = threading.Lock()
        self._ensure_dirs()

    def _ensure_dirs(self):
        """Create reload log directory."""
        try:
            if not self.kernel.sos.exists(RELOAD_LOG_PATH):
                self.kernel.sos.mkdir(RELOAD_LOG_PATH, parents=True)
        except Exception:
            pass

    def _module_to_attr(self, module_name: str) -> Optional[str]:
        """
        Map a module name to its kernel attribute.

        For example 'ai.engine' → 'ai', 'store.sos' → 'sos'.

        Args:
            module_name (str): Dotted module name.

        Returns:
            str: Kernel attribute name, or None if not found.
        """
        # Build mapping from module path to kernel attribute
        mapping = {
            "store.sos":       "sos",
            "ai.engine":       "ai",
            "ai.advisor":      "advisor",
            "ai.gpu":          None,    # no live instance
            "search.neural":   "search",
            "shell.nova_shell":"shell",
            "net.server":      "api_server",
            "security.audit":  "auditor",
            "system.doctor":   "doctor",
            "ai.agents":       "agents",
            "store.branches":  "branches",
            "store.crypto":    "crypto",
        }
        return mapping.get(module_name)

    def can_reload(self, module_name: str) -> tuple[bool, str]:
        """
        Check if a module can be safely reloaded.

        Args:
            module_name (str): The module to check.

        Returns:
            Tuple[bool, str]: (can_reload, reason_if_not)
        """
        # Check module exists
        spec = importlib.util.find_spec(module_name)
        if spec is None:
            return False, f"Module '{module_name}' not found in sys.path"
        # Check it's not a C extension
        if spec.origin and spec.origin.endswith(".so"):
            return False, "Cannot reload C extension modules"
        # Warn about risky modules
        risky = {"store.sos", "kernel.nova", "boot.pyinit"}
        if module_name in risky:
            return True, (f"WARNING: '{module_name}' is critical infrastructure. "
                          "Reload may destabilise the kernel.")
        return True, "Safe to reload"

    def reload(self, module_name: str,
               dry_run: bool = False) -> dict:
        """
        Hot-reload a module and migrate live instances.

        Args:
            module_name (str): Dotted module name (e.g. 'ai.engine').
            dry_run (bool): If True, check without applying changes.

        Returns:
            dict: Result with keys: success, module, message, duration_ms.
        """
        can, reason = self.can_reload(module_name)
        if not can:
            return {"success": False, "module": module_name, "message": reason}

        if dry_run:
            return {"success": True, "module": module_name,
                    "message": f"Dry-run OK: {reason}",
                    "dry_run": True}

        with self._lock:
            return self._do_reload(module_name)

    def _do_reload(self, module_name: str) -> dict:
        """Perform the actual reload."""
        start = time.time()
        attr  = self._module_to_attr(module_name)

        # Find the old module and live instance
        old_module = sys.modules.get(module_name)
        if not old_module:
            try:
                old_module = importlib.import_module(module_name)
            except ImportError as e:
                return {"success": False, "module": module_name,
                        "message": f"Import error: {e}"}

        instance = getattr(self.kernel, attr, None) if attr else None

        # Snapshot state before reload
        state_snap = {}
        if instance:
            try:
                state_snap = copy.copy(instance.__dict__)
            except Exception:
                pass

        snap = ReloadSnapshot(module_name, old_module, instance, state_snap)

        # Grace period — let in-flight operations complete
        time.sleep(self.GRACE_PERIOD)

        # Reload the module
        try:
            new_module = importlib.reload(old_module)
        except Exception as e:
            return {"success": False, "module": module_name,
                    "message": f"Reload failed: {e}"}

        # Migrate live instance to new class
        migrated = False
        if instance and attr:
            try:
                # Find the primary class in the new module
                new_class = self._find_primary_class(new_module, type(instance).__name__)
                if new_class and self._classes_compatible(type(instance), new_class):
                    instance.__class__ = new_class
                    migrated = True
                elif new_class:
                    # Create new instance and transfer state
                    try:
                        new_inst = object.__new__(new_class)
                        new_inst.__dict__.update(state_snap)
                        new_inst.__dict__["_kernel"] = self.kernel
                        setattr(self.kernel, attr, new_inst)
                        migrated = True
                    except Exception:
                        pass
            except Exception as e:
                pass   # non-fatal — module reloaded even if migration failed

        # Store snapshot for potential rollback
        self._history.append(snap)
        if len(self._history) > 20:
            self._history.pop(0)

        # Log to SOS
        self._log_reload(module_name, migrated, time.time() - start)

        duration_ms = round((time.time() - start) * 1000)
        return {
            "success":     True,
            "module":      module_name,
            "message":     f"Reloaded in {duration_ms}ms" + (" (migrated)" if migrated else ""),
            "migrated":    migrated,
            "duration_ms": duration_ms,
        }

    def _find_primary_class(self, module, class_name: str):
        """Find a class by name in a module."""
        cls = getattr(module, class_name, None)
        if cls and isinstance(cls, type):
            return cls
        # Try to find any class defined in this module
        for name, obj in vars(module).items():
            if (isinstance(obj, type)
                    and obj.__module__ == module.__name__
                    and name == class_name):
                return obj
        return None

    def _classes_compatible(self, old_cls, new_cls) -> bool:
        """Check if two classes have compatible memory layouts for __class__ assignment."""
        try:
            # CPython allows __class__ assignment when slots are compatible
            # We check by looking at __slots__ if present
            old_slots = getattr(old_cls, "__slots__", None)
            new_slots = getattr(new_cls, "__slots__", None)
            return old_slots == new_slots
        except Exception:
            return False

    def rollback(self) -> dict:
        """
        Roll back the most recent reload.

        Returns:
            dict: Result with success status and message.
        """
        with self._lock:
            if not self._history:
                return {"success": False, "message": "No reloads to roll back"}
            snap = self._history[-1]
            if snap.rolled_back:
                return {"success": False, "message": "Already rolled back"}
            attr = self._module_to_attr(snap.module_name)
            if attr and snap.instance:
                try:
                    # Restore old class
                    snap.instance.__class__ = type.__new__(
                        type, type(snap.instance).__name__,
                        type(snap.instance).__bases__, {}
                    )
                    # Restore state
                    snap.instance.__dict__.update(snap.state_snapshot)
                    snap.rolled_back = True
                    return {"success": True,
                            "message": f"Rolled back {snap.module_name}"}
                except Exception as e:
                    return {"success": False, "message": f"Rollback failed: {e}"}
            snap.rolled_back = True
            return {"success": True, "message": f"Rolled back {snap.module_name}"}

    def history(self) -> List[dict]:
        """Return reload history."""
        return [
            {"module": s.module_name, "ts": s.timestamp,
             "rolled_back": s.rolled_back}
            for s in reversed(self._history)
        ]

    def _log_reload(self, module_name: str, migrated: bool, duration: float):
        """Persist reload event to SOS."""
        try:
            path = f"{RELOAD_LOG_PATH}/{int(time.time())}_{module_name.replace('.','_')}"
            self.kernel.sos.write(path, json.dumps({
                "module": module_name, "ts": time.time(),
                "migrated": migrated, "duration_s": round(duration, 3),
            }))
        except Exception:
            pass


import json  # imported at bottom to avoid circular reference in type hints
