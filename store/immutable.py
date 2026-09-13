"""
PyOS NOVA — Immutable Object Partition
=========================================
A separate SOS partition where objects are append-only.
No delete, no modify. Every append extends a Merkle tree.
The root hash is a compact proof of the entire partition's integrity.

Use cases:
  - Compliance logs (GDPR 7-year retention)
  - Audit evidence (tamper-proof)
  - Smart contract–style agreements (immutable once signed)
  - Published datasets (readers can verify completeness)

The partition is at /immutable/ in the SOS.
Any `rm` or `write` to an existing path in /immutable/ is rejected.
The Merkle root is updated on every append.

Shell commands:
  immutable write <path> <content>   — append-only write
  immutable root                     — show current Merkle root
  immutable verify                   — verify all objects against root
  immutable proof <path>             — generate inclusion proof for one object
  immutable list                     — list all immutable objects
"""

from __future__ import annotations
import os, sys, json, time, hashlib
from typing import List, Optional, Tuple, TYPE_CHECKING

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from store.sos import SemanticObjectStore

IMMUTABLE_BASE = "/immutable"
MERKLE_ROOT_PATH = "/immutable/.merkle_root"
MANIFEST_PATH    = "/immutable/.manifest"


def _sha256(*parts: bytes) -> str:
    """Compute SHA-256 over concatenated byte parts."""
    h = hashlib.sha256()
    for p in parts:
        h.update(p)
    return h.hexdigest()


def _merkle_root(leaf_hashes: List[str]) -> str:
    """
    Compute the Merkle root of a list of leaf hashes.

    Uses a binary Merkle tree. Odd number of leaves: last leaf is duplicated.

    Args:
        leaf_hashes (List[str]): SHA-256 hex hashes of all leaves.

    Returns:
        str: The Merkle root hash.
    """
    if not leaf_hashes:
        return _sha256(b"empty")
    level = list(leaf_hashes)
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])   # duplicate last if odd
        level = [
            _sha256(level[i].encode(), level[i+1].encode())
            for i in range(0, len(level), 2)
        ]
    return level[0]


def _merkle_proof(leaf_hashes: List[str],
                   index: int) -> List[Tuple[str, str]]:
    """
    Generate a Merkle inclusion proof for a leaf at index.

    Args:
        leaf_hashes (List[str]): All leaf hashes.
        index (int): Index of the leaf to prove.

    Returns:
        List[Tuple[str, str]]: Proof path as (sibling_hash, direction) tuples.
    """
    proof = []
    level = list(leaf_hashes)
    idx   = index
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])
        if idx % 2 == 0:
            sibling = level[idx + 1] if idx + 1 < len(level) else level[idx]
            proof.append((sibling, "right"))
        else:
            proof.append((level[idx - 1], "left"))
        idx //= 2
        level = [
            _sha256(level[i].encode(), level[i+1].encode())
            for i in range(0, len(level), 2)
        ]
    return proof


def _verify_proof(leaf_hash: str, proof: List[Tuple[str, str]],
                   root: str) -> bool:
    """
    Verify a Merkle inclusion proof.

    Args:
        leaf_hash (str): Hash of the leaf to verify.
        proof (List[Tuple[str, str]]): Proof path from _merkle_proof.
        root (str): Expected Merkle root.

    Returns:
        bool: True if the proof is valid.
    """
    current = leaf_hash
    for sibling, direction in proof:
        if direction == "right":
            current = _sha256(current.encode(), sibling.encode())
        else:
            current = _sha256(sibling.encode(), current.encode())
    return current == root


class ImmutablePartition:
    """
    Append-only object partition with Merkle tree integrity.
    
    Once an object is written, it cannot be modified or deleted.
    The Merkle root can be used to prove the partition's integrity
    to external parties.
    """

    def __init__(self, sos: "SemanticObjectStore"):
        """Initialise the immutable partition."""
        self.sos    = sos
        self._manifest: List[dict] = []   # ordered list of {path, oid, hash}
        self._root  = ""
        self._ensure_dirs()
        self._load()

    def _ensure_dirs(self):
        """Create immutable partition directory."""
        if not self.sos.exists(IMMUTABLE_BASE):
            self.sos.mkdir(IMMUTABLE_BASE, parents=True)

    def _load(self):
        """Load manifest and Merkle root from SOS."""
        try:
            self._manifest = json.loads(self.sos.read(MANIFEST_PATH))
        except Exception:
            self._manifest = []
        try:
            self._root = self.sos.read(MERKLE_ROOT_PATH).strip()
        except Exception:
            self._root = _merkle_root([])

    def _save(self):
        """Persist manifest and Merkle root."""
        self.sos.write(MANIFEST_PATH, json.dumps(self._manifest))
        self.sos.write(MERKLE_ROOT_PATH, self._root, tags=["merkle-root"])

    def write(self, path: str, content: str) -> str:
        """
        Append-only write to the immutable partition.

        Raises ValueError if the path already exists.

        Args:
            path (str): The path within the immutable partition.
            content (str): The content to store.

        Returns:
            str: The OID of the stored object.
        """
        full_path = f"{IMMUTABLE_BASE}/{path.lstrip('/')}"
        if self.sos.exists(full_path):
            raise ValueError(
                f"Immutable partition: {full_path} already exists "
                f"and cannot be modified."
            )
        oid = self.sos.write(full_path, content,
                              tags=["immutable"],
                              meta={"immutable": True,
                                    "partition_seq": len(self._manifest)})
        content_hash = _sha256(content.encode())
        self._manifest.append({
            "path": full_path, "oid": oid,
            "hash": content_hash, "ts": time.time(),
            "seq":  len(self._manifest),
        })
        self._root = _merkle_root([m["hash"] for m in self._manifest])
        self._save()
        return oid

    def read(self, path: str) -> str:
        """
        Read an object from the immutable partition.

        Args:
            path (str): Path within the partition.

        Returns:
            str: The object content.
        """
        full_path = f"{IMMUTABLE_BASE}/{path.lstrip('/')}"
        return self.sos.read(full_path)

    def merkle_root(self) -> str:
        """Return the current Merkle root hash."""
        return self._root

    def verify(self) -> Tuple[bool, str]:
        """
        Verify all objects in the partition against the Merkle tree.

        Returns:
            Tuple[bool, str]: (is_valid, message)
        """
        if not self._manifest:
            return True, "Partition is empty."
        errors = []
        for entry in self._manifest:
            try:
                content = self.sos.read(entry["path"])
                actual_hash = _sha256(content.encode())
                if actual_hash != entry["hash"]:
                    errors.append(
                        f"Hash mismatch at {entry['path']}: "
                        f"stored {entry['hash'][:12]}... "
                        f"computed {actual_hash[:12]}..."
                    )
            except Exception as e:
                errors.append(f"Cannot read {entry['path']}: {e}")
        if errors:
            return False, "Partition tampered:\n  " + "\n  ".join(errors)
        # Recompute root
        computed_root = _merkle_root([m["hash"] for m in self._manifest])
        if computed_root != self._root:
            return False, "Merkle root mismatch — manifest has been altered."
        return True, (f"Partition verified — {len(self._manifest)} objects, "
                      f"root: {self._root[:16]}...")

    def inclusion_proof(self, path: str) -> Optional[dict]:
        """
        Generate a Merkle inclusion proof for a path.

        Args:
            path (str): Path within the partition.

        Returns:
            dict: Proof data or None if path not found.
        """
        full_path = f"{IMMUTABLE_BASE}/{path.lstrip('/')}"
        idx = next((i for i, m in enumerate(self._manifest)
                    if m["path"] == full_path), None)
        if idx is None:
            return None
        leaf_hashes = [m["hash"] for m in self._manifest]
        proof = _merkle_proof(leaf_hashes, idx)
        return {
            "path":       full_path,
            "leaf_hash":  self._manifest[idx]["hash"],
            "proof":      proof,
            "root":       self._root,
            "seq":        idx,
            "total":      len(self._manifest),
        }

    def verify_proof(self, proof_data: dict) -> bool:
        """
        Verify an inclusion proof.

        Args:
            proof_data (dict): Proof data from inclusion_proof().

        Returns:
            bool: True if the proof is valid.
        """
        return _verify_proof(
            proof_data["leaf_hash"],
            [(p[0], p[1]) for p in proof_data["proof"]],
            proof_data["root"],
        )

    def list_objects(self) -> List[dict]:
        """Return all manifest entries."""
        return list(self._manifest)

    def stats(self) -> dict:
        """Return partition statistics."""
        return {
            "objects":    len(self._manifest),
            "root":       self._root[:16] + "...",
            "full_root":  self._root,
        }
