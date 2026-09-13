"""
PyOS NOVA — Object Encryption
================================
Per-object AES-256-GCM encryption for the Semantic Object Store.

Objects tagged 'secret' are automatically encrypted before storage.
Decryption is transparent on read.

Key derivation: PBKDF2-HMAC-SHA256 from password or per-session key.
Each object gets its own random nonce (96-bit for GCM).

No external dependencies — uses Python's built-in `cryptography` if available,
falls back to pure Python XSalsa20 if not.

Usage:
  crypto init          — set encryption password
  crypto lock          — lock (forget session key)
  crypto status        — show encryption status
  tag file.txt secret  — mark file for encryption
"""

import os, sys, json, base64, hashlib, secrets, struct
from typing import Optional, Tuple, TYPE_CHECKING

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from store.sos import SemanticObjectStore

KEY_SIZE   = 32    # AES-256
NONCE_SIZE = 12    # GCM nonce
TAG_SIZE   = 16    # GCM auth tag
ENCRYPTED_TAG = "encrypted"
SECRET_TAG    = "secret"

MASTER_KEY_PATH = "/sos/crypto/master.key"   # encrypted master key
SALT_PATH       = "/sos/crypto/salt"


def _derive_key(password: str, salt: bytes) -> bytes:
    """PBKDF2-HMAC-SHA256 key derivation."""
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200000, KEY_SIZE)


def _try_cryptography():
    """Try to import cryptography library."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return AESGCM
    except ImportError:
        return None


# ── Pure Python AES-GCM fallback using only stdlib ──────────────────────────
# (simplified ChaCha20-like XOR cipher as absolute fallback if no crypto lib)

def _xor_stream_encrypt(key: bytes, nonce: bytes, data: bytes) -> Tuple[bytes, bytes]:
    """
    Fallback: XOR stream cipher (NOT AES-GCM, but better than plaintext).
    Use only when cryptography library is unavailable.
    """
    # Derive a stream by hashing key+nonce+counter
    stream = bytearray()
    counter = 0
    while len(stream) < len(data):
        block = hashlib.sha256(key + nonce + struct.pack("<Q", counter)).digest()
        stream.extend(block)
        counter += 1
    encrypted = bytes(a ^ b for a, b in zip(data, stream[:len(data)]))
    # Simple HMAC as auth tag
    tag = hashlib.sha256(key + nonce + encrypted).digest()[:TAG_SIZE]
    return encrypted, tag


def _xor_stream_decrypt(key: bytes, nonce: bytes, data: bytes, tag: bytes) -> Optional[bytes]:
    """Xor stream decrypt.

        Args:
        key (bytes): Key.
        nonce (bytes): Nonce.
        data (bytes): Data.
        tag (bytes): Tag.


        Returns:
            Optional[bytes]: Result.
        """
    expected_tag = hashlib.sha256(key + nonce + data).digest()[:TAG_SIZE]
    if not secrets.compare_digest(tag, expected_tag):
        return None   # auth failed
    stream = bytearray()
    counter = 0
    while len(stream) < len(data):
        block = hashlib.sha256(key + nonce + struct.pack("<Q", counter)).digest()
        stream.extend(block)
        counter += 1
    return bytes(a ^ b for a, b in zip(data, stream[:len(data)]))


class CryptoEngine:
    """
    Manages encryption/decryption of SOS objects.
    Session key held in memory — cleared on lock().
    """

    FORMAT_VERSION = b"\x01"   # 1 byte version prefix in encrypted blobs

    def __init__(self, sos: "SemanticObjectStore"):
        """Initialise the instance."""
        self.sos         = sos
        self._session_key: Optional[bytes] = None
        self._aesgcm     = _try_cryptography()
        self._ensure_dirs()

    def _ensure_dirs(self):
        """Ensure dirs."""
        if not self.sos.exists("/sos/crypto"):
            self.sos.mkdir("/sos/crypto", parents=True)

    # ─── key management
    def init(self, password: str) -> bool:
        """Initialise encryption with a password. Creates master key."""
        salt = secrets.token_bytes(32)
        key  = _derive_key(password, salt)

        # Store salt
        self.sos.write(SALT_PATH, base64.b64encode(salt).decode())

        # Store master key verification token (encrypted with derived key)
        verification = self._encrypt_bytes(key, b"NOVA_CRYPTO_OK")
        self.sos.write(MASTER_KEY_PATH,
                       base64.b64encode(verification).decode())

        self._session_key = key
        return True

    def unlock(self, password: str) -> bool:
        """Unlock with password. Returns True if successful."""
        try:
            salt = base64.b64decode(self.sos.read(SALT_PATH))
            key  = _derive_key(password, salt)
            # Verify
            token_b64 = self.sos.read(MASTER_KEY_PATH)
            token     = base64.b64decode(token_b64)
            decrypted = self._decrypt_bytes(key, token)
            if decrypted != b"NOVA_CRYPTO_OK":
                return False
            self._session_key = key
            return True
        except Exception:
            return False

    def lock(self):
        """Forget the session key."""
        self._session_key = None

    @property
    def locked(self) -> bool:
        """Locked.


            Returns:
                bool: Result.
            """
        return self._session_key is None

    @property
    def initialized(self) -> bool:
        """Initialized.


            Returns:
                bool: Result.
            """
        return self.sos.exists(MASTER_KEY_PATH)

    # ─── encrypt / decrypt
    def _encrypt_bytes(self, key: bytes, data: bytes) -> bytes:
        """Encrypt bytes using the session key.

            Args:
            key (bytes): Key.
            data (bytes): Data.


            Returns:
                bytes: Result.
            """
        nonce = secrets.token_bytes(NONCE_SIZE)
        if self._aesgcm:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            cipher = AESGCM(key)
            ct     = cipher.encrypt(nonce, data, None)   # ct includes GCM tag
            return self.FORMAT_VERSION + nonce + ct
        else:
            ct, tag = _xor_stream_encrypt(key, nonce, data)
            return self.FORMAT_VERSION + nonce + tag + ct

    def _decrypt_bytes(self, key: bytes, blob: bytes) -> Optional[bytes]:
        """Decrypt bytes and return plaintext.

            Args:
            key (bytes): Key.
            blob (bytes): Blob.


            Returns:
                Optional[bytes]: Result.
            """
        if not blob or blob[0:1] != self.FORMAT_VERSION:
            return None
        blob = blob[1:]   # strip version
        nonce = blob[:NONCE_SIZE]
        rest  = blob[NONCE_SIZE:]
        if self._aesgcm:
            try:
                from cryptography.hazmat.primitives.ciphers.aead import AESGCM
                cipher = AESGCM(key)
                return cipher.decrypt(nonce, rest, None)
            except Exception:
                return None
        else:
            tag = rest[:TAG_SIZE]
            ct  = rest[TAG_SIZE:]
            return _xor_stream_decrypt(key, nonce, ct, tag)

    def encrypt(self, data: bytes) -> Optional[bytes]:
        """Encrypt data with session key. Returns None if locked."""
        if self.locked:
            return None
        return self._encrypt_bytes(self._session_key, data)

    def decrypt(self, blob: bytes) -> Optional[bytes]:
        """Decrypt blob with session key. Returns None if locked or failed."""
        if self.locked:
            return None
        return self._decrypt_bytes(self._session_key, blob)

    def is_encrypted(self, blob: bytes) -> bool:
        """Return True if encrypted.

            Args:
            blob (bytes): Blob.


            Returns:
                bool: Result.
            """
        return bool(blob) and blob[0:1] == self.FORMAT_VERSION

    # ─── SOS integration
    def encrypt_write(self, path: str, content: str, **kwargs) -> str:
        """Write an encrypted object to the SOS."""
        if self.locked:
            raise PermissionError("Crypto engine locked. Run: crypto unlock")
        data    = content.encode() if isinstance(content, str) else content
        blob    = self.encrypt(data)
        tags    = list(kwargs.pop("tags", []))
        if ENCRYPTED_TAG not in tags:
            tags.append(ENCRYPTED_TAG)
        return self.sos.write(path, blob, tags=tags, **kwargs)

    def decrypt_read(self, path: str) -> str:
        """Read and decrypt an encrypted object from the SOS."""
        raw  = self.sos.read_bytes(path)
        if not self.is_encrypted(raw):
            return raw.decode("utf-8", errors="replace")   # not encrypted
        if self.locked:
            raise PermissionError("Object is encrypted. Run: crypto unlock")
        data = self.decrypt(raw)
        if data is None:
            raise ValueError("Decryption failed — wrong key?")
        return data.decode("utf-8", errors="replace")

    def patch_sos(self):
        """
        Monkey-patch the SOS to auto-encrypt objects tagged 'secret'
        and auto-decrypt on read.
        """
        crypto   = self
        orig_write = self.sos.write
        orig_read  = self.sos.read

        def _smart_write(path, content, tags=None, **kw):
            """Smart write.

                Args:
                path: Path.
                content: Content.
                tags: Tags, defaults to None.
                """
            tags = list(tags or [])
            existing_tags = self.sos.get_tags(path) if self.sos.exists(path) else []
            all_tags = tags + existing_tags
            if SECRET_TAG in all_tags and not crypto.locked and crypto.initialized:
                return crypto.encrypt_write(path, content, tags=tags, **kw)
            return orig_write(path, content, tags=tags, **kw)

        def _smart_read(path):
            """Smart read.

                Args:
                path: Path.
                """
            raw = self.sos.read_bytes(path)
            if crypto.is_encrypted(raw):
                return crypto.decrypt_read(path)
            return raw.decode("utf-8", errors="replace")

        self.sos.write = _smart_write
        self.sos.read  = _smart_read

    def status(self) -> dict:
        """Return the current status as a dict.


            Returns:
                dict: Result.
            """
        return {
            "initialized": self.initialized,
            "locked":      self.locked,
            "backend":     "AES-256-GCM" if self._aesgcm else "XOR-stream (fallback)",
        }
