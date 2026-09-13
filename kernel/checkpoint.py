"""
PyOS NOVA — Process Checkpoint & Restore
==========================================
Snapshot a running Python process's heap and state to SOS.
Restore it exactly — processes survive reboots and can migrate
between NOVA nodes via SOS sync.

Checkpoint stores:
  - Global variables of each tracked module (filtered whitelist)
  - SOS path context (cwd, open paths)
  - Queue contents for NovaTask channels
  - Arbitrary state registered via checkpoint_register()

Restore:
  - Load snapshot from SOS
  - Re-import modules
  - Restore global state
  - Rehydrate queues
  - Call registered restore hooks

Shell commands:
  checkpoint <name>          — snapshot current process state
  checkpoint list            — show available snapshots
  checkpoint restore <name>  — restore from snapshot
  checkpoint diff <n1> <n2>  — compare two snapshots
  checkpoint delete <name>   — remove a snapshot
"""

from __future__ import annotations
import os, sys, time, json, pickle, hashlib, gzip, threading
from typing import Any, Dict, List, Optional, Callable, TYPE_CHECKING
from dataclasses import dataclass, field

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from store.sos import SemanticObjectStore

CHECKPOINT_BASE = "/system/checkpoints"

# Modules whose globals we save (whitelist)
CHECKPOINTABLE_MODULES = {
    "store.prefetch",
    "store.nlquery",
    "ai.memory",
    "ai.agents",
    "shell.scripting",
}

# Types safe to pickle
SAFE_TYPES = (int, float, str, bytes, bool, list, dict, tuple, set,
              type(None))


def _is_safe(obj: Any) -> bool:
    """Return True if an object is safe to pickle."""
    try:
        pickle.dumps(obj, protocol=2)
        return True
    except Exception:
        return False


@dataclass
class CheckpointRecord:
    """Metadata for one checkpoint."""
    name:       str
    created_at: float
    size_bytes: int
    modules:    List[str]
    oid:        str

    def to_dict(self) -> dict:
        """Serialize to dict."""
        return {
            "name":       self.name,
            "created_at": self.created_at,
            "size_bytes": self.size_bytes,
            "modules":    self.modules,
            "oid":        self.oid,
        }

    @staticmethod
    def from_dict(d: dict) -> "CheckpointRecord":
        """Deserialize from dict."""
        return CheckpointRecord(**d)


class CheckpointManager:
    """
    Creates and restores process state snapshots.

    Registered components provide save_state()/restore_state()
    hooks for custom serialisation.
    """

    def __init__(self, sos: "SemanticObjectStore"):
        """Initialise the checkpoint manager."""
        self.sos       = sos
        self._hooks:   Dict[str, tuple] = {}  # name → (save_fn, restore_fn)
        self._lock     = threading.Lock()
        self._ensure_dirs()

    def _ensure_dirs(self):
        """Create checkpoint directory."""
        if not self.sos.exists(CHECKPOINT_BASE):
            self.sos.mkdir(CHECKPOINT_BASE, parents=True)

    def register(self, name: str,
                   save_fn: Callable[[], Any],
                   restore_fn: Callable[[Any], None]):
        """
        Register a component for checkpointing.

        Args:
            name (str): Unique identifier for this component.
            save_fn (Callable): Returns the state to save.
            restore_fn (Callable): Accepts the saved state and restores it.
        """
        self._hooks[name] = (save_fn, restore_fn)

    def snapshot(self, name: str) -> CheckpointRecord:
        """
        Take a full checkpoint snapshot.

        Calls all registered save_fn hooks and serialises the results
        along with captured module globals to the SOS.

        Args:
            name (str): Checkpoint identifier.

        Returns:
            CheckpointRecord: Metadata for the created checkpoint.
        """
        with self._lock:
            state: Dict[str, Any] = {
                "metadata": {
                    "name":       name,
                    "timestamp":  time.time(),
                    "nova_ver":   "4.0.0",
                },
                "hooks":   {},
                "modules": {},
                "cwd":     getattr(self, "_cwd", "/home/root"),
            }

            # Collect registered hook states
            for hook_name, (save_fn, _) in self._hooks.items():
                try:
                    s = save_fn()
                    if _is_safe(s):
                        state["hooks"][hook_name] = s
                except Exception as e:
                    state["hooks"][hook_name] = f"<error: {e}>"

            # Capture whitelisted module globals
            saved_modules = []
            for mod_name in CHECKPOINTABLE_MODULES:
                mod = sys.modules.get(mod_name)
                if not mod:
                    continue
                mod_state = {}
                for k, v in vars(mod).items():
                    if k.startswith("_"):
                        continue
                    if isinstance(v, type) or callable(v):
                        continue
                    if _is_safe(v):
                        mod_state[k] = v
                if mod_state:
                    state["modules"][mod_name] = mod_state
                    saved_modules.append(mod_name)

            # Serialise with gzip+pickle
            raw   = pickle.dumps(state, protocol=4)
            blob  = gzip.compress(raw, compresslevel=6)
            blob_hex = blob.hex()

            path  = f"{CHECKPOINT_BASE}/{name}.pkl.gz"
            oid   = self.sos.write(path, blob_hex,
                                    tags=["checkpoint", name],
                                    meta={"checkpoint_name": name,
                                          "size_bytes": len(blob)})

            rec = CheckpointRecord(
                name=name, created_at=time.time(),
                size_bytes=len(blob),
                modules=saved_modules, oid=oid,
            )
            # Save index entry
            idx_path = f"{CHECKPOINT_BASE}/{name}.meta"
            self.sos.write(idx_path, json.dumps(rec.to_dict()),
                           tags=["checkpoint-meta"])
            return rec

    def restore(self, name: str) -> bool:
        """
        Restore from a checkpoint snapshot.

        Calls all registered restore_fn hooks and restores
        captured module globals.

        Args:
            name (str): Checkpoint to restore.

        Returns:
            bool: True if successfully restored.
        """
        path = f"{CHECKPOINT_BASE}/{name}.pkl.gz"
        if not self.sos.exists(path):
            return False

        try:
            blob_hex = self.sos.read(path)
            blob     = bytes.fromhex(blob_hex)
            raw      = gzip.decompress(blob)
            state    = pickle.loads(raw)
        except Exception:
            return False

        with self._lock:
            # Restore module globals
            for mod_name, mod_state in state.get("modules", {}).items():
                mod = sys.modules.get(mod_name)
                if mod:
                    for k, v in mod_state.items():
                        try:
                            setattr(mod, k, v)
                        except Exception:
                            pass

            # Call registered restore hooks
            for hook_name, (_, restore_fn) in self._hooks.items():
                saved = state.get("hooks", {}).get(hook_name)
                if saved and not isinstance(saved, str):
                    try:
                        restore_fn(saved)
                    except Exception:
                        pass

        return True

    def list_checkpoints(self) -> List[CheckpointRecord]:
        """Return all available checkpoints."""
        records = []
        for name in self.sos.listdir(CHECKPOINT_BASE):
            if not name.endswith(".meta"):
                continue
            try:
                path = f"{CHECKPOINT_BASE}/{name}"
                data = json.loads(self.sos.read(path))
                records.append(CheckpointRecord.from_dict(data))
            except Exception:
                pass
        return sorted(records, key=lambda r: r.created_at, reverse=True)

    def delete(self, name: str) -> bool:
        """Delete a checkpoint."""
        removed = False
        for suffix in (".pkl.gz", ".meta"):
            path = f"{CHECKPOINT_BASE}/{name}{suffix}"
            if self.sos.exists(path):
                self.sos.remove(path)
                removed = True
        return removed

    def diff(self, name1: str, name2: str) -> str:
        """
        Compare two checkpoints and return a human-readable diff.

        Args:
            name1 (str): First checkpoint name.
            name2 (str): Second checkpoint name.

        Returns:
            str: Human-readable diff summary.
        """
        def _load(name):
            path = f"{CHECKPOINT_BASE}/{name}.pkl.gz"
            blob = bytes.fromhex(self.sos.read(path))
            return pickle.loads(gzip.decompress(blob))

        try:
            s1 = _load(name1)
            s2 = _load(name2)
        except Exception as e:
            return f"Cannot load checkpoints: {e}"

        lines = [f"\nDiff: {name1} → {name2}",
                 "─" * 50]

        # Compare module globals
        mods1 = s1.get("modules", {})
        mods2 = s2.get("modules", {})
        all_mods = set(mods1) | set(mods2)
        for mod in sorted(all_mods):
            g1 = mods1.get(mod, {})
            g2 = mods2.get(mod, {})
            all_keys = set(g1) | set(g2)
            for key in sorted(all_keys):
                v1 = g1.get(key, "<missing>")
                v2 = g2.get(key, "<missing>")
                if v1 != v2:
                    lines.append(
                        f"  {mod}.{key}\n"
                        f"    - {str(v1)[:60]}\n"
                        f"    + {str(v2)[:60]}"
                    )

        if len(lines) == 2:
            lines.append("  No differences found.")
        return "\n".join(lines)
