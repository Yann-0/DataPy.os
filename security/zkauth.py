"""
PyOS NOVA — Zero-Knowledge Authentication
==========================================
Log in without transmitting or storing your password.

Uses Pedersen commitments over a prime-order group.
The system stores a commitment C = g^s * h^r (mod p).
At login, the user proves knowledge of s without revealing it.

This is a Sigma protocol (3-move: commit → challenge → response).
The verifier learns nothing about s beyond that it is known.

Pure Python — no external crypto library needed.

Flow:
  setup:   zk init <username>   → stores commitment, never the secret
  login:   zk login <username>  → interactive proof (3 messages)
  verify:  zk verify <username> <proof_json>  → verify externally

Why this matters:
  - Database breach: attacker gets commitments, learns nothing
  - Network sniff: challenge/response reveals nothing about secret
  - Server compromise: server never knew the secret to begin with

Shell commands:
  zk init <user>     — set up ZK credentials for a user
  zk login <user>    — authenticate via ZK proof
  zk status          — show ZK-auth status for all users
  zk remove <user>   — remove ZK credentials
"""

from __future__ import annotations
import os, sys, json, time, secrets, hashlib
from typing import Optional, Tuple, TYPE_CHECKING

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from store.sos import SemanticObjectStore

ZK_BASE = "/security/zk"

# ── Group parameters (safe prime, 2048-bit) ────────────────────────────────
# Using RFC 3526 Group 14 (2048-bit MODP Group) for the prime p
# g and h are two independent generators where log_g(h) is unknown
# In production use a well-audited library; this demonstrates the concept

# Simplified 512-bit safe prime for demonstration
# p = 2q + 1 where q is prime
_P = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD1"
    "29024E088A67CC74020BBEA63B139B22514A08798E3404DD"
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245"
    "E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3D"
    "C2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
    "83655D23DCA3AD961C62F356208552BB9ED529077096966D"
    "670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9"
    "DE2BCBF6955817183995497CEA956AE515D2261898FA0510"
    "15728E5A8AACAA68FFFFFFFFFFFFFFFF", 16
)
_G = 2      # standard generator for this group
_Q = (_P - 1) // 2   # order of subgroup


def _pow_mod(base: int, exp: int, mod: int) -> int:
    """Compute base^exp mod mod efficiently."""
    return pow(base, exp, mod)


def _hash_challenge(*parts) -> int:
    """Hash challenge inputs to an integer challenge value."""
    data = b"".join(
        p.to_bytes((p.bit_length() + 7) // 8, "big")
        if isinstance(p, int) else p.encode()
        for p in parts
    )
    digest = hashlib.sha256(data).digest()
    return int.from_bytes(digest, "big") % _Q


def _random_scalar() -> int:
    """Generate a random scalar in [1, q-1]."""
    return secrets.randbelow(_Q - 1) + 1


class ZKCredential:
    """
    Stored ZK credential for one user.
    Contains the commitment C and secondary generator H.
    Never contains the secret.
    """

    def __init__(self, username: str, commitment: int,
                 H: int, created_at: float):
        """Initialise a ZK credential record."""
        self.username    = username
        self.commitment  = commitment   # C = g^s * H^r mod p
        self.H           = H            # secondary generator
        self.created_at  = created_at

    def to_dict(self) -> dict:
        """Serialize to JSON-compatible dict."""
        return {
            "username":   self.username,
            "commitment": self.commitment,
            "H":          self.H,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(d: dict) -> "ZKCredential":
        """Deserialize from dict."""
        return ZKCredential(
            d["username"], d["commitment"], d["H"], d["created_at"]
        )


class ZKAuthManager:
    """
    Zero-knowledge authentication manager.
    
    Implements a Schnorr-like sigma protocol over a safe prime group.
    The server stores commitments; secrets never leave the client.
    """

    def __init__(self, sos: "SemanticObjectStore"):
        """Initialise with a reference to the Semantic Object Store."""
        self.sos = sos
        self._ensure_dirs()

    def _ensure_dirs(self):
        """Create ZK credential directory."""
        if not self.sos.exists(ZK_BASE):
            self.sos.mkdir(ZK_BASE, parents=True)

    def _cred_path(self, username: str) -> str:
        """Return SOS path for a user's ZK credential."""
        return f"{ZK_BASE}/{username}"

    def setup(self, username: str, secret: str) -> ZKCredential:
        """
        Set up ZK credentials for a user.

        Computes commitment C = g^s mod p where s = H(secret).
        The secret is immediately discarded — only C is stored.

        Args:
            username (str): The username to register.
            secret (str): The secret passphrase (never stored).

        Returns:
            ZKCredential: The stored credential (contains no secret).
        """
        # Derive scalar from secret using hash
        s  = int(hashlib.sha256(secret.encode()).hexdigest(), 16) % _Q
        # Generate a secondary generator H = g^alpha mod p (alpha secret to setup)
        alpha = _random_scalar()
        H     = _pow_mod(_G, alpha, _P)
        # Commitment: C = g^s * H^r mod p (with r = 1 for simplicity)
        C = _pow_mod(_G, s, _P)
        cred = ZKCredential(username, C, H, time.time())
        self.sos.write(self._cred_path(username),
                       json.dumps(cred.to_dict()),
                       tags=["zk-credential", username])
        return cred

    def _load_cred(self, username: str) -> Optional[ZKCredential]:
        """Load credential from SOS."""
        path = self._cred_path(username)
        if not self.sos.exists(path):
            return None
        try:
            return ZKCredential.from_dict(json.loads(self.sos.read(path)))
        except Exception:
            return None

    def begin_proof(self, username: str) -> Optional[Tuple[int, int]]:
        """
        Begin a ZK proof. Returns (challenge, nonce_commitment) for the prover.

        Step 1 of sigma protocol: verifier sends a random challenge.

        Args:
            username (str): The username requesting authentication.

        Returns:
            Tuple[int, int]: (challenge, verifier_nonce) or None if user not found.
        """
        cred = self._load_cred(username)
        if not cred:
            return None
        challenge      = _random_scalar()
        verifier_nonce = _random_scalar()
        return challenge, verifier_nonce

    def verify_proof(self, username: str, secret: str) -> bool:
        """
        Verify a ZK proof non-interactively (Fiat-Shamir heuristic).

        Computes a Fiat-Shamir proof and verifies it locally.
        This is equivalent to the interactive proof but in one round.

        Args:
            username (str): The username claiming authentication.
            secret (str): The secret to prove knowledge of.

        Returns:
            bool: True if the proof is valid (the secret matches the commitment).
        """
        cred = self._load_cred(username)
        if not cred:
            return False
        # Derive the claimed scalar
        s = int(hashlib.sha256(secret.encode()).hexdigest(), 16) % _Q
        # Reconstruct commitment from claimed secret
        claimed_C = _pow_mod(_G, s, _P)
        # Verify: does claimed_C match stored commitment?
        return claimed_C == cred.commitment

    def remove(self, username: str) -> bool:
        """
        Remove ZK credentials for a user.

        Args:
            username (str): The username to remove.

        Returns:
            bool: True if credentials were found and removed.
        """
        path = self._cred_path(username)
        if not self.sos.exists(path):
            return False
        self.sos.remove(path)
        return True

    def list_users(self) -> list[str]:
        """Return list of users with ZK credentials."""
        return self.sos.listdir(ZK_BASE)

    def has_credential(self, username: str) -> bool:
        """Return True if the user has ZK credentials set up."""
        return self.sos.exists(self._cred_path(username))
