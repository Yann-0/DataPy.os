"""
PyOS NOVA — SOS Branching
===========================
Git-like branching on top of the Semantic Object Store.

The SOS is already a DAG of content-addressed objects with parent_oid links.
We add:
  - Branches: named pointers to tip OIDs per path
  - Checkout: switch active branch
  - Merge:    three-way merge of text objects
  - Diff:     line-level diff between versions/branches

Commands:
  sos branch              — list branches
  sos branch <name>       — create branch from current state
  sos checkout <branch>   — switch to branch
  sos merge <branch>      — merge branch into current
  sos diff <path>         — diff current vs previous version
  sos diff <p> <v1> <v2>  — diff two specific versions
"""

import os, sys, json, time, difflib
from typing import List, Dict, Optional, Tuple, TYPE_CHECKING

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from store.sos import SemanticObjectStore, SObject

BRANCHES_PATH = "/sos/branches.json"
HEAD_PATH     = "/sos/HEAD"


class Branch:
    """Branch."""
    def __init__(self, name: str, tips: Dict[str, str],
                 created: float = None, description: str = ""):
        """Initialise the instance."""
        self.name        = name
        self.tips        = tips       # path → OID at branch creation
        self.created     = created or time.time()
        self.description = description

    """To dict."""
    def to_dict(self): return self.__dict__

    @staticmethod
    def from_dict(d):
        """From dict.

            Args:
            d: D.
            """
        return Branch(d["name"], d.get("tips",{}),
                      d.get("created",time.time()), d.get("description",""))


class BranchManager:
    """
    Manages branches for the Semantic Object Store.
    """

    def __init__(self, sos: "SemanticObjectStore"):
        """Initialise the instance."""
        self.sos  = sos
        self._ensure_dirs()

    def _ensure_dirs(self):
        """Ensure dirs."""
        if not self.sos.exists("/sos"):
            self.sos.mkdir("/sos", parents=True)

    def _load_branches(self) -> Dict[str, Branch]:
        """Load branches.


            Returns:
                Dict[str, Branch]: Result.
            """
        try:
            data = json.loads(self.sos.read(BRANCHES_PATH))
            return {k: Branch.from_dict(v) for k, v in data.items()}
        except Exception:
            return {"main": Branch("main", {})}

    def _save_branches(self, branches: Dict[str, Branch]):
        """Save branches.

            Args:
            branches (Dict[str, Branch]): Branches.
            """
        data = {k: v.to_dict() for k, v in branches.items()}
        self.sos.write(BRANCHES_PATH, json.dumps(data, indent=2))

    def current_branch(self) -> str:
        """Current branch.


            Returns:
                str: Result.
            """
        try:
            return self.sos.read(HEAD_PATH).strip()
        except Exception:
            return "main"

    def _set_head(self, branch: str):
        """Set the head.

            Args:
            branch (str): Branch.
            """
        self.sos.write(HEAD_PATH, branch)

    def list_branches(self) -> List[Branch]:
        """Return a list of branches.


            Returns:
                List[Branch]: Result.
            """
        return list(self._load_branches().values())

    def create_branch(self, name: str, description: str = "") -> Branch:
        """Create branch.

            Args:
            name (str): Name.
            description (str): Description, defaults to ''.


            Returns:
                Branch: Result.
            """
        branches = self._load_branches()
        if name in branches:
            raise ValueError(f"Branch '{name}' already exists")
        # Snapshot current state — record all current alias→OID mappings
        conn = self.sos._pool.get()
        rows = conn.execute("SELECT path, oid FROM aliases").fetchall()
        tips = {row["path"]: row["oid"] for row in rows}
        branch = Branch(name, tips, description=description)
        branches[name] = branch
        self._save_branches(branches)
        return branch

    def checkout(self, name: str) -> Branch:
        """Switch to a branch — restore all OIDs to their branch-tip state."""
        branches = self._load_branches()
        if name not in branches:
            raise ValueError(f"Branch '{name}' not found")
        branch = branches[name]
        # Restore all path→OID mappings from the branch snapshot
        with self.sos._lock:
            conn = self.sos._pool.get()
            for path, oid in branch.tips.items():
                # Only restore if OID still exists
                exists = conn.execute("SELECT 1 FROM objects WHERE oid=?", (oid,)).fetchone()
                if exists:
                    conn.execute("INSERT OR REPLACE INTO aliases(path,oid,is_dir) VALUES (?,?,0)",
                                 (path, oid))
            conn.commit()
        # Invalidate path cache
        for path in branch.tips:
            self.sos._path_cache.invalidate(path)
        self._set_head(name)
        return branch

    def diff(self, path: str, v1: int = None, v2: int = None) -> str:
        """
        Compute unified diff between two versions of an object.
        v1/v2: version numbers. If None, compare v(n) vs v(n-1).
        """
        history = self.sos.history(path)
        if len(history) < 2:
            return "  Only one version exists — nothing to diff."

        if v1 is None and v2 is None:
            # current vs previous
            a, b = history[1], history[0]
        else:
            versions = {obj.version: obj for obj in history}
            a = versions.get(v1 or history[-1].version, history[-1])
            b = versions.get(v2 or history[0].version, history[0])

        lines_a = a.text.splitlines(keepends=True)
        lines_b = b.text.splitlines(keepends=True)
        diff    = list(difflib.unified_diff(
            lines_a, lines_b,
            fromfile=f"{path} (v{a.version})",
            tofile=f"{path} (v{b.version})",
            lineterm="",
        ))

        if not diff:
            return f"  No differences between v{a.version} and v{b.version}."

        # Colorise
        lines = []
        for line in diff:
            if line.startswith("+") and not line.startswith("+++"):
                lines.append(f"\033[32m{line}\033[0m")
            elif line.startswith("-") and not line.startswith("---"):
                lines.append(f"\033[31m{line}\033[0m")
            elif line.startswith("@@"):
                lines.append(f"\033[36m{line}\033[0m")
            else:
                lines.append(line)
        return "\n".join(lines)

    def merge(self, source_branch: str, target_branch: str = None) -> dict:
        """
        Three-way text merge.
        For each path in source that differs from target, attempt line-level merge.
        Returns: {merged: n, conflicts: n, unchanged: n}
        """
        if target_branch is None:
            target_branch = self.current_branch()

        branches = self._load_branches()
        if source_branch not in branches:
            raise ValueError(f"Source branch '{source_branch}' not found")
        if target_branch not in branches:
            raise ValueError(f"Target branch '{target_branch}' not found")

        src = branches[source_branch]
        tgt = branches[target_branch]

        merged = 0; conflicts = 0; unchanged = 0

        for path, src_oid in src.tips.items():
            tgt_oid = tgt.tips.get(path)

            if src_oid == tgt_oid:
                unchanged += 1
                continue

            src_obj = self.sos.get(src_oid)
            tgt_obj = self.sos.get(tgt_oid) if tgt_oid else None

            if src_obj is None:
                unchanged += 1
                continue

            if tgt_obj is None:
                # New file in source — add to target
                self.sos.alias(path, src_oid)
                self.sos._path_cache.set(path, src_oid)
                merged += 1
                continue

            if src_obj.kind != "text" or tgt_obj.kind != "text":
                # Binary/non-text: source wins
                self.sos.alias(path, src_oid)
                merged += 1
                continue

            # Three-way text merge
            result, had_conflict = self._merge_text(tgt_obj.text, src_obj.text)
            new_oid = self.sos.store(result, kind="text",
                                      meta={**tgt_obj.meta, "merged": True},
                                      parent_oid=tgt_oid)
            self.sos.alias(path, new_oid)
            if had_conflict:
                conflicts += 1
            else:
                merged += 1

        # Update target branch snapshot
        conn = self.sos._pool.get()
        rows = conn.execute("SELECT path, oid FROM aliases").fetchall()
        tgt.tips = {row["path"]: row["oid"] for row in rows}
        branches[target_branch] = tgt
        self._save_branches(branches)

        return {"merged": merged, "conflicts": conflicts, "unchanged": unchanged}

    def _merge_text(self, base: str, incoming: str) -> Tuple[str, bool]:
        """
        Simple two-way merge (no common ancestor for simplicity).
        In real git this is three-way. Here we use difflib SequenceMatcher.
        """
        base_lines     = base.splitlines(keepends=True)
        incoming_lines = incoming.splitlines(keepends=True)
        matcher = difflib.SequenceMatcher(None, base_lines, incoming_lines)
        result  = []
        had_conflict = False

        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                result.extend(base_lines[i1:i2])
            elif tag == "replace":
                # Mark as conflict
                result.append("<<<<<<< current\n")
                result.extend(base_lines[i1:i2])
                result.append("=======\n")
                result.extend(incoming_lines[j1:j2])
                result.append(">>>>>>> incoming\n")
                had_conflict = True
            elif tag == "delete":
                pass   # removed in incoming
            elif tag == "insert":
                result.extend(incoming_lines[j1:j2])

        return "".join(result), had_conflict
