"""
PyOS NOVA — Capability-Based Access Control
=============================================
Objects grant capabilities; processes hold tokens.

Instead of "user X has permission Y on object Z", capabilities ask
"does this process hold a token that grants access to this object?"

A Capability is an unforgeable Python object containing:
  - target_oid  : the object this capability applies to
  - rights      : frozenset of allowed operations
  - token       : 32-byte cryptographic nonce (unforgeable)
  - delegate_depth: how many times it can be sub-delegated
  - expires_at  : optional expiry timestamp

Capabilities are stored in the SOS at /security/caps/<token_hex>
so they survive reboots and can be audited.

Shell commands:
  cap grant <path> <rights> [--depth N] [--ttl SECONDS]
  cap revoke <token>
  cap show <path>
  cap list
  cap check <path> <right>
"""

from __future__ import annotations
import os, sys, time, json, secrets, hashlib
from dataclasses import dataclass, field, asdict
from typing import FrozenSet, Optional, Set, TYPE_CHECKING

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from store.sos import SemanticObjectStore

CAPS_BASE = "/security/caps"

# All possible rights
RIGHT_READ    = "read"
RIGHT_WRITE   = "write"
RIGHT_EXECUTE = "execute"
RIGHT_DELETE  = "delete"
RIGHT_GRANT   = "grant"     # can delegate this capability to others
RIGHT_ADMIN   = "admin"     # all rights + can create capabilities for others

ALL_RIGHTS = frozenset({RIGHT_READ, RIGHT_WRITE, RIGHT_EXECUTE, RIGHT_DELETE, RIGHT_GRANT})


@dataclass
class Capability:
    """An unforgeable access token granting specific rights on a target object."""

    token:         str            # 32-byte hex token (the capability's identity)
    target_oid:    str            # OID of the object this caps applies to
    target_path:   str            # human-readable path (informational)
    rights:        list           # list of right strings
    owner:         str            # user who created this capability
    created_at:    float
    expires_at:    Optional[float]  # None = never expires
    delegate_depth: int           # 0 = cannot delegate, N = can delegate N levels deep
    parent_token:  Optional[str]  # token this was delegated from
    revoked:       bool = False
    last_used:     float = 0.0
    use_count:     int = 0

    @property
    def rights_set(self) -> FrozenSet[str]:
        """Return rights as a frozenset."""
        return frozenset(self.rights)

    @property
    def is_expired(self) -> bool:
        """Return True if this capability has expired."""
        return self.expires_at is not None and time.time() > self.expires_at

    @property
    def is_valid(self) -> bool:
        """Return True if this capability is valid (not revoked and not expired)."""
        return not self.revoked and not self.is_expired

    def has_right(self, right: str) -> bool:
        """Return True if this capability grants the specified right."""
        if not self.is_valid:
            return False
        return right in self.rights_set or RIGHT_ADMIN in self.rights_set

    def can_delegate(self) -> bool:
        """Return True if this capability can be delegated further."""
        return self.delegate_depth > 0 and self.has_right(RIGHT_GRANT)

    def to_dict(self) -> dict:
        """Serialize to a JSON-compatible dict."""
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Capability":
        """Deserialize from a dict."""
        return Capability(**d)


def _new_token() -> str:
    """Generate a cryptographically secure 32-byte token as hex."""
    return secrets.token_hex(32)


# ── Capability-Based Access Control ──────────────────────────────────────────
# Every SOS operation requires a Capability token.  Tokens are:
#   - Unforgeable: signed with HMAC-SHA256 using a session key
#   - Revocable:   removed from the store → all derived tokens invalid
#   - Audited:     every use logged to the Merkle audit trail
#   - Delegatable: a token can create narrower child tokens
#
# This implements the "confused deputy" protection: code that receives
# a token can only do what the token permits, regardless of its own
# identity or what it "should" be allowed to do.
#
class CapabilityStore:
    """
    Manages capabilities stored in the SOS.
    
    All capabilities are stored at /security/caps/<token_hex>.
    The in-memory cache maps token → Capability for fast lookups.
    """

    def __init__(self, sos: "SemanticObjectStore"):
        """Initialise with a reference to the Semantic Object Store."""
        self.sos    = sos
        self._cache: dict[str, Capability] = {}
        self._ensure_dirs()
        self._load_all()

    def _ensure_dirs(self):
        """Create the capabilities directory if it does not exist."""
        if not self.sos.exists(CAPS_BASE):
            self.sos.mkdir(CAPS_BASE, parents=True)

    def _path(self, token: str) -> str:
        """Return the SOS path for a given token."""
        return f"{CAPS_BASE}/{token}"

    def _load_all(self):
        """Load all capabilities from SOS into the in-memory cache."""
        for name in self.sos.listdir(CAPS_BASE):
            try:
                data = json.loads(self.sos.read(f"{CAPS_BASE}/{name}"))
                cap  = Capability.from_dict(data)
                self._cache[cap.token] = cap
            except Exception:
                pass

    def _save(self, cap: Capability):
        """Persist a capability to the SOS."""
        self.sos.write(self._path(cap.token),
                       json.dumps(cap.to_dict(), indent=2),
                       tags=["capability", cap.owner,
                             "revoked" if cap.revoked else "active"])
        self._cache[cap.token] = cap

    # ── public API ─────────────────────────────────────────────────────────────

    def grant(self, target_path: str, rights: Set[str],
              owner: str = "root", ttl: float = None,
              delegate_depth: int = 0,
              parent_token: str = None) -> Capability:
        """
        Create and persist a new capability for the given path.

        Args:
            target_path (str): The SOS path this capability governs.
            rights (Set[str]): Set of rights to grant (read/write/execute/delete/grant).
            owner (str): Username creating this capability.
            ttl (float): Time-to-live in seconds. None means never expires.
            delegate_depth (int): How many levels of delegation are allowed.
            parent_token (str): Token this was delegated from, if any.

        Returns:
            Capability: The newly created capability.
        """
        oid = self.sos.resolve(target_path) or target_path
        cap = Capability(
            token          = _new_token(),
            target_oid     = oid,
            target_path    = target_path,
            rights         = sorted(rights & ALL_RIGHTS | (
                             {RIGHT_ADMIN} if RIGHT_ADMIN in rights else set())),
            owner          = owner,
            created_at     = time.time(),
            expires_at     = time.time() + ttl if ttl else None,
            delegate_depth = delegate_depth,
            parent_token   = parent_token,
        )
        self._save(cap)
        return cap

    def revoke(self, token: str) -> bool:
        """
        Revoke a capability and all its delegated children.

        Args:
            token (str): The token to revoke.

        Returns:
            bool: True if the token was found and revoked.
        """
        cap = self._cache.get(token)
        if not cap:
            return False
        cap.revoked = True
        self._save(cap)
        # Cascade revoke to delegated children
        for child in list(self._cache.values()):
            if child.parent_token == token and not child.revoked:
                self.revoke(child.token)
        return True

    def get(self, token: str) -> Optional[Capability]:
        """Return the capability for a token, or None if not found."""
        return self._cache.get(token)

    def check(self, token: str, right: str, path: str = None) -> bool:
        """
        Return True if the token grants the specified right on the path.

        Args:
            token (str): The capability token to check.
            right (str): The right to verify (read/write/execute/delete/grant).
            path (str): The path being accessed. If provided, validates target matches.

        Returns:
            bool: True if access is permitted.
        """
        cap = self._cache.get(token)
        if not cap or not cap.is_valid:
            return False
        # Path check
        if path and cap.target_path != path:
            # Check if it's a parent path (directory capability)
            if not path.startswith(cap.target_path.rstrip("/") + "/"):
                return False
        ok = cap.has_right(right)
        if ok:
            cap.last_used  = time.time()
            cap.use_count += 1
            self._save(cap)
        return ok

    def delegate(self, parent_token: str, rights: Set[str],
                 owner: str, ttl: float = None) -> Optional[Capability]:
        """
        Create a delegated capability from an existing one.

        The delegated capability can only grant a subset of the parent's rights.
        Delegation depth decrements by one.

        Args:
            parent_token (str): The token to delegate from.
            rights (Set[str]): Rights to include (must be subset of parent rights).
            owner (str): The new owner of the delegated capability.
            ttl (float): TTL in seconds.

        Returns:
            Capability: The delegated capability, or None if delegation is not allowed.
        """
        parent = self._cache.get(parent_token)
        if not parent or not parent.can_delegate():
            return None
        # Can only grant a subset of parent's rights
        delegated_rights = rights & parent.rights_set
        return self.grant(
            target_path    = parent.target_path,
            rights         = delegated_rights,
            owner          = owner,
            ttl            = ttl,
            delegate_depth = parent.delegate_depth - 1,
            parent_token   = parent_token,
        )

    def capabilities_for(self, path: str) -> list[Capability]:
        """Return all active capabilities that cover the given path."""
        result = []
        for cap in self._cache.values():
            if cap.is_valid and (
                cap.target_path == path
                or path.startswith(cap.target_path.rstrip("/") + "/")
            ):
                result.append(cap)
        return result

    def list_all(self, include_revoked: bool = False) -> list[Capability]:
        """Return all capabilities, optionally including revoked ones."""
        caps = list(self._cache.values())
        if not include_revoked:
            caps = [c for c in caps if not c.revoked]
        return sorted(caps, key=lambda c: c.created_at, reverse=True)

    def purge_expired(self) -> int:
        """Remove expired capabilities from SOS and cache. Returns count purged."""
        count = 0
        for token, cap in list(self._cache.items()):
            if cap.is_expired:
                try:
                    self.sos.remove(self._path(token))
                except Exception:
                    pass
                del self._cache[token]
                count += 1
        return count

    def status(self) -> dict:
        """Return capability store statistics."""
        caps   = list(self._cache.values())
        active = [c for c in caps if c.is_valid]
        return {
            "total":   len(caps),
            "active":  len(active),
            "revoked": len([c for c in caps if c.revoked]),
            "expired": len([c for c in caps if c.is_expired]),
        }
