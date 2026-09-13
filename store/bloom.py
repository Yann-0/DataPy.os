"""
PyOS NOVA — Bloom Filter Existence Index
==========================================
Probabilistic existence check for SOS paths.
O(1) for negative lookups (the common case) — eliminates SQLite
round-trips for paths that don't exist.

Parameters:
  capacity   = 500_000 paths
  error_rate = 1% false positives
  size       = 64KB (499,712 bits)
  hash_fns   = 7

A false positive means: bloom says "exists" but SQLite says "no".
That just causes one extra SQLite lookup — no data corruption.
A false negative is impossible by Bloom filter definition.

Shell commands:
  bloom status          — show filter statistics
  bloom rebuild         — rebuild from current SOS aliases
  bloom check <path>    — test path membership
  bloom bench           — benchmark vs direct SQLite lookup
"""

from __future__ import annotations
import hashlib, math, os, sys, struct, threading, time
from typing import Optional, TYPE_CHECKING

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from store.sos import SemanticObjectStore

BLOOM_PATH = "/.bloom/filter"


class BloomFilter:
    """
    Space-efficient probabilistic set membership tester.

    Uses k independent hash functions derived from two base hashes
    (double hashing technique — avoids expensive independent hashes).
    """

    def __init__(self, capacity: int = 500_000,
                  error_rate: float = 0.01):
        """Initialise a Bloom filter with target capacity and error rate."""
        self.capacity   = capacity
        self.error_rate = error_rate
        self._n_bits    = self._optimal_bits(capacity, error_rate)
        self._n_hashes  = self._optimal_hashes(self._n_bits, capacity)
        self._bits      = bytearray(math.ceil(self._n_bits / 8))
        self._count     = 0
        self._lock      = threading.Lock()

    @staticmethod
    def _optimal_bits(n: int, p: float) -> int:
        """Return optimal filter size in bits."""
        return int(-n * math.log(p) / (math.log(2) ** 2))

    @staticmethod
    def _optimal_hashes(m: int, n: int) -> int:
        """Return optimal number of hash functions."""
        return max(1, round((m / n) * math.log(2)))

    def _hashes(self, item: str) -> list[int]:
        """
        Generate k hash positions using double hashing.

        Args:
            item (str): The item to hash.

        Returns:
            list[int]: k bit positions in range [0, n_bits).
        """
        h1 = int(hashlib.sha256(item.encode()).hexdigest(), 16)
        h2 = int(hashlib.md5(item.encode()).hexdigest(), 16)
        return [(h1 + i * h2) % self._n_bits
                for i in range(self._n_hashes)]

    def add(self, item: str):
        """
        Add an item to the filter.

        Args:
            item (str): The item to add.
        """
        with self._lock:
            for pos in self._hashes(item):
                self._bits[pos >> 3] |= (1 << (pos & 7))
            self._count += 1

    def __contains__(self, item: str) -> bool:
        """
        Return True if item is probably in the set.

        False means definitely not in the set.
        True means probably in the set (1% false positive rate).

        Args:
            item (str): Item to test.

        Returns:
            bool: Membership result.
        """
        bits = self._bits
        for pos in self._hashes(item):
            if not (bits[pos >> 3] & (1 << (pos & 7))):
                return False
        return True

    def __len__(self) -> int:
        """Return approximate number of items added."""
        return self._count

    def to_bytes(self) -> bytes:
        """Serialise filter to bytes."""
        header = struct.pack("<IIIf",
            self._n_bits, self._n_hashes, self._count, self.error_rate)
        return header + bytes(self._bits)

    @staticmethod
    def from_bytes(data: bytes) -> "BloomFilter":
        """Deserialise filter from bytes."""
        n_bits, n_hashes, count, error_rate = struct.unpack("<IIIf", data[:16])
        f          = BloomFilter.__new__(BloomFilter)
        f.capacity   = count
        f.error_rate = error_rate
        f._n_bits    = n_bits
        f._n_hashes  = n_hashes
        f._bits      = bytearray(data[16:])
        f._count     = count
        f._lock      = threading.Lock()
        return f

    def stats(self) -> dict:
        """Return filter statistics."""
        fill_rate = sum(bin(b).count("1")
                        for b in self._bits) / self._n_bits
        return {
            "size_kb":     len(self._bits) // 1024,
            "bits":        self._n_bits,
            "hash_fns":    self._n_hashes,
            "items":       self._count,
            "fill_rate":   f"{fill_rate:.1%}",
            "error_rate":  f"{self.error_rate:.1%}",
        }


class SOSBloomIndex:
    """
    Bloom filter index for SOS path existence checks.

    Intercepts `sos.exists(path)` and returns False immediately
    for paths that are definitely not in the SOS, avoiding
    SQLite round-trips entirely.
    """

    def __init__(self, sos: "SemanticObjectStore"):
        """Initialise the SOS Bloom index."""
        self.sos      = sos
        self._filter: Optional[BloomFilter] = None
        self._hits    = 0    # bloom returned "exists", saved a lookup
        self._saves   = 0    # bloom returned "not exists", saved SQLite
        self._total   = 0
        self._lock    = threading.Lock()
        self._ensure_dirs()
        self._load_or_build()

    def _ensure_dirs(self):
        """Create Bloom filter storage directory."""
        if not self.sos.exists("/.bloom"):
            self.sos.mkdir("/.bloom", parents=True)

    def _load_or_build(self):
        """Load persisted filter or build from scratch."""
        if self.sos.exists(BLOOM_PATH):
            try:
                blob = bytes.fromhex(self.sos.read(BLOOM_PATH))
                self._filter = BloomFilter.from_bytes(blob)
                return
            except Exception:
                pass
        self.rebuild()

    def rebuild(self) -> int:
        """
        Rebuild the Bloom filter from all current SOS aliases.

        Returns:
            int: Number of paths indexed.
        """
        conn    = self.sos._pool.get()
        rows    = conn.execute("SELECT path FROM aliases").fetchall()
        f       = BloomFilter(capacity=max(len(rows) * 2, 50_000))
        for row in rows:
            f.add(row[0])
        with self._lock:
            self._filter = f
        self._persist()
        return len(rows)

    def _persist(self):
        """Save filter to SOS."""
        try:
            blob = self._filter.to_bytes()
            self.sos.write(BLOOM_PATH, blob.hex(),
                            kind="data", tags=["bloom-filter"])
        except Exception:
            pass

    def add(self, path: str):
        """Add a path to the filter (called on every SOS write)."""
        with self._lock:
            if self._filter:
                self._filter.add(path)

    def might_exist(self, path: str) -> bool:
        """
        Return False if path definitely does not exist.

        Args:
            path (str): SOS path to check.

        Returns:
            bool: True if path MIGHT exist (check SOS to confirm).
                  False means DEFINITELY does not exist.
        """
        self._total += 1
        with self._lock:
            f = self._filter
        if f is None:
            return True   # no filter → must check SQLite
        result = path in f
        if not result:
            self._saves += 1
        return result

    def patch_sos(self):
        """
        Monkey-patch SOS.exists() to use Bloom filter for fast negatives.
        """
        orig_exists = self.sos.exists
        orig_write  = self.sos.write
        bloom       = self

        def _fast_exists(path: str) -> bool:
            if not bloom.might_exist(path):
                return False   # definitely not in SOS
            return orig_exists(path)

        def _write_and_index(path: str, content, **kw):
            oid = orig_write(path, content, **kw)
            bloom.add(path)
            return oid

        self.sos.exists = _fast_exists
        self.sos.write  = _write_and_index
        self.sos._bloom = bloom

    def stats(self) -> dict:
        """Return index statistics."""
        with self._lock:
            f_stats = self._filter.stats() if self._filter else {}
        save_rate = self._saves / max(self._total, 1)
        return {
            **f_stats,
            "total_checks":   self._total,
            "sqlite_saves":   self._saves,
            "save_rate":      f"{save_rate:.1%}",
        }
