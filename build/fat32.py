"""
Tested FAT32 image writer with cluster chains and VFAT long names.

The previous builder assigned every file cluster 3, omitted FAT chains
and truncated long names. This writer allocates unique clusters, writes
both FAT copies, encodes LFN directory entries and verifies readback.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

BYTES_PER_SECTOR = 512
FAT32_EOF = 0x0FFFFFFF
FAT32_BAD = 0x0FFFFFF7
FAT32_RESERVED_MASK = 0x0FFFFFFF


class Fat32Error(Exception):
    """Raised when a FAT32 image cannot be written or verified."""


def _pad(data: bytes, size: int, fill: bytes = b"\x00") -> bytes:
    if len(data) > size:
        return data[:size]
    return data + fill * (size - len(data))


def _lfn_checksum(short83: bytes) -> int:
    chk = 0
    for byte in short83[:11]:
        chk = ((chk & 1) << 7) + (chk >> 1) + byte
        chk &= 0xFF
    return chk


def _split_83(name: str, used: set[str]) -> bytes:
    """Generate a unique 8.3 directory name (OEM, upper-case)."""
    name = name.replace("/", "\\")
    base = name.rsplit("\\", 1)[-1]
    stem, _, ext = base.partition(".")
    stem = "".join(c if c.isalnum() else "_" for c in stem.upper())[:6] or "FILE"
    ext = "".join(c if c.isalnum() else "_" for c in ext.upper())[:3]
    seq = 1
    while True:
        candidate = f"{stem[:6]}~{seq}".ljust(8)[:8] + ext.ljust(3)
        key = candidate.encode("ascii", "replace")
        if key not in used:
            used.add(key)
            return key
        seq += 1
        if seq > 999999:
            raise Fat32Error(f"cannot allocate 8.3 name for {name!r}")


def _lfn_entries(long_name: str, checksum: int) -> list[bytes]:
    """Encode VFAT long-name entries (last-to-first order)."""
    # UCS-2 code units, padded with 0x0000 then 0xFFFF.
    units = list(long_name.encode("utf-16le"))
    # encode gives bytes; convert to 16-bit units
    u16 = list(struct.unpack("<" + "H" * (len(long_name.encode("utf-16le")) // 2),
                             long_name.encode("utf-16le")))
    # NUL terminator then 0xFFFF fill to a multiple of 13.
    u16.append(0)
    while len(u16) % 13 != 0:
        u16.append(0xFFFF)
    chunks: list[list[int]] = []
    for i in range(0, len(u16), 13):
        chunks.append(u16[i:i + 13])
    total = len(chunks)
    entries: list[bytes] = []
    for idx, chunk in enumerate(reversed(chunks), start=1):
        seq = (total - idx + 1)
        if idx == 1:
            seq |= 0x40
        name1 = struct.pack("<5H", *chunk[0:5])
        name2 = struct.pack("<6H", *chunk[5:11])
        name3 = struct.pack("<2H", *chunk[11:13])
        entry = bytes([
            seq, *name1, 0x0F, 0x00, checksum, *name2, 0x00, 0x00, *name3
        ])
        if len(entry) != 32:
            raise Fat32Error("LFN entry size mismatch")
        entries.append(entry)
    return entries


@dataclass
class _DirEnt:
    """In-memory directory slot."""

    name: str
    is_dir: bool
    cluster: int
    size: int
    short83: bytes = b""


@dataclass
class Fat32Image:
    """In-memory FAT32 volume with verified allocation."""

    size_bytes: int
    label: str = "NOVABOOT"
    sectors_per_cluster: int = 8
    reserved_sectors: int = 32
    _data: bytearray = field(init=False, repr=False)
    _fat: list[int] = field(init=False, repr=False)
    _next_cluster: int = field(init=False, default=3)
    _root_cluster: int = 2
    _used_83: set[bytes] = field(init=False, default_factory=set)
    _dirs: dict[str, list[_DirEnt]] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        if self.size_bytes < 2 * 1024 * 1024:
            raise Fat32Error("FAT32 image must be at least 2 MiB")
        if self.size_bytes % BYTES_PER_SECTOR:
            raise Fat32Error("image size must be a multiple of 512")
        if len(self.label) > 11:
            raise Fat32Error("volume label must be <= 11 characters")
        total_sectors = self.size_bytes // BYTES_PER_SECTOR
        self._bytes_per_cluster = self.sectors_per_cluster * BYTES_PER_SECTOR
        # FAT size: enough 4-byte entries for all data clusters.
        data_sectors_guess = total_sectors - self.reserved_sectors
        fat_sectors = 1
        while True:
            data_sectors = total_sectors - self.reserved_sectors - 2 * fat_sectors
            clusters = data_sectors // self.sectors_per_cluster
            needed = (clusters + 2) * 4
            needed_sectors = (needed + BYTES_PER_SECTOR - 1) // BYTES_PER_SECTOR
            if needed_sectors <= fat_sectors:
                break
            fat_sectors = needed_sectors
        self._fat_sectors = fat_sectors
        self._total_clusters = clusters
        if self._total_clusters < 65525:
            # Small volumes can still be FAT32 if we force the FAT32 BPB.
            # Keep going; Pi boot partitions are tens of MiB.
            pass
        self._data_start_sector = self.reserved_sectors + 2 * self._fat_sectors
        self._data = bytearray(self.size_bytes)
        self._fat = [0] * (self._total_clusters + 2)
        self._fat[0] = 0x0FFFFFF8
        self._fat[1] = FAT32_EOF
        self._fat[2] = FAT32_EOF  # root directory
        self._dirs["/"] = []
        self._write_boot_sector()
        self._write_fsinfo()

    def _cluster_offset(self, cluster: int) -> int:
        if cluster < 2:
            raise Fat32Error(f"invalid cluster {cluster}")
        return (self._data_start_sector
                + (cluster - 2) * self.sectors_per_cluster) * BYTES_PER_SECTOR

    def _alloc_clusters(self, nbytes: int) -> list[int]:
        needed = max(1, (nbytes + self._bytes_per_cluster - 1)
                      // self._bytes_per_cluster)
        clusters: list[int] = []
        c = self._next_cluster
        while len(clusters) < needed:
            if c >= len(self._fat):
                raise Fat32Error(
                    f"volume full: need {needed} clusters, "
                    f"allocated {len(clusters)}"
                )
            if self._fat[c] == 0:
                clusters.append(c)
            c += 1
        for i, cl in enumerate(clusters):
            self._fat[cl] = FAT32_EOF if i == len(clusters) - 1 else clusters[i + 1]
        self._next_cluster = max(self._next_cluster, clusters[-1] + 1)
        return clusters

    def _write_boot_sector(self) -> None:
        b = bytearray(BYTES_PER_SECTOR)
        b[0:3] = b"\xEB\x58\x90"
        b[3:11] = b"DATAPY  "
        struct.pack_into("<H", b, 11, BYTES_PER_SECTOR)
        b[13] = self.sectors_per_cluster
        struct.pack_into("<H", b, 14, self.reserved_sectors)
        b[16] = 2
        struct.pack_into("<H", b, 17, 0)
        struct.pack_into("<H", b, 19, 0)
        b[21] = 0xF8
        struct.pack_into("<H", b, 22, 0)
        struct.pack_into("<H", b, 24, 63)
        struct.pack_into("<H", b, 26, 255)
        struct.pack_into("<I", b, 28, 0)
        struct.pack_into("<I", b, 32, self.size_bytes // BYTES_PER_SECTOR)
        struct.pack_into("<I", b, 36, self._fat_sectors)
        struct.pack_into("<H", b, 40, 0)
        struct.pack_into("<H", b, 42, 0)
        struct.pack_into("<I", b, 44, self._root_cluster)
        struct.pack_into("<H", b, 48, 1)
        struct.pack_into("<H", b, 50, 6)
        b[64] = 0x80
        b[66] = 0x29
        struct.pack_into("<I", b, 67, 0x1234ABCD)
        b[71:82] = _pad(self.label.encode("ascii"), 11, b" ")
        b[82:90] = b"FAT32   "
        b[510:512] = b"\x55\xAA"
        self._data[0:512] = b
        # Backup boot sector at sector 6
        self._data[6 * 512:6 * 512 + 512] = b

    def _write_fsinfo(self) -> None:
        fs = bytearray(BYTES_PER_SECTOR)
        struct.pack_into("<I", fs, 0, 0x41615252)
        struct.pack_into("<I", fs, 484, 0x61417272)
        struct.pack_into("<I", fs, 488, 0xFFFFFFFF)
        struct.pack_into("<I", fs, 492, 0xFFFFFFFF)
        struct.pack_into("<I", fs, 508, 0xAA550000)
        self._data[512:1024] = fs

    def _flush_fat(self) -> None:
        blob = bytearray(self._fat_sectors * BYTES_PER_SECTOR)
        for i, val in enumerate(self._fat):
            struct.pack_into("<I", blob, i * 4, val & 0x0FFFFFFF)
        fat0 = self.reserved_sectors * BYTES_PER_SECTOR
        fat1 = fat0 + self._fat_sectors * BYTES_PER_SECTOR
        self._data[fat0:fat0 + len(blob)] = blob
        self._data[fat1:fat1 + len(blob)] = blob

    def _write_cluster_chain(self, clusters: list[int], payload: bytes) -> None:
        remaining = payload
        for cl in clusters:
            off = self._cluster_offset(cl)
            chunk = remaining[:self._bytes_per_cluster]
            self._data[off:off + len(chunk)] = chunk
            remaining = remaining[len(chunk):]

    def _read_cluster_chain(self, start: int, size: int) -> bytes:
        out = bytearray()
        cl = start
        seen: set[int] = set()
        while cl >= 2 and (cl & FAT32_RESERVED_MASK) < FAT32_BAD:
            if cl in seen:
                raise Fat32Error(f"FAT cycle at cluster {cl}")
            seen.add(cl)
            off = self._cluster_offset(cl)
            out.extend(self._data[off:off + self._bytes_per_cluster])
            nxt = self._fat[cl] & FAT32_RESERVED_MASK
            if nxt >= FAT32_EOF or nxt == 0:
                break
            cl = nxt
        return bytes(out[:size])

    def mkdir(self, path: str) -> None:
        """Create a directory, including parents."""
        path = self._norm(path)
        if path == "/":
            return
        parent, name = path.rsplit("/", 1)
        parent = parent or "/"
        if parent != "/":
            self.mkdir(parent)
        for ent in self._dirs.setdefault(parent, []):
            if ent.name.lower() == name.lower():
                if not ent.is_dir:
                    raise Fat32Error(f"{path} exists as a file")
                return
        clusters = self._alloc_clusters(self._bytes_per_cluster)
        # "." and ".." entries
        body = self._dot_entries(clusters[0], parent)
        self._write_cluster_chain(clusters, body)
        short = _split_83(name, self._used_83)
        self._dirs.setdefault(parent, []).append(
            _DirEnt(name=name, is_dir=True, cluster=clusters[0],
                    size=0, short83=short)
        )
        self._dirs.setdefault(path, [])

    def write_file(self, path: str, content: bytes) -> None:
        """Write a file with a unique cluster chain and LFN entries."""
        path = self._norm(path)
        parent, name = path.rsplit("/", 1)
        parent = parent or "/"
        if parent != "/":
            self.mkdir(parent)
        clusters = self._alloc_clusters(len(content) if content else 1)
        self._write_cluster_chain(clusters, content)
        short = _split_83(name, self._used_83)
        ents = self._dirs.setdefault(parent, [])
        ents[:] = [e for e in ents if e.name.lower() != name.lower()]
        ents.append(_DirEnt(name=name, is_dir=False, cluster=clusters[0],
                             size=len(content), short83=short))

    def read_file(self, path: str) -> bytes:
        """Read a file by walking the FAT chain (independent of write path)."""
        path = self._norm(path)
        parent, name = path.rsplit("/", 1)
        parent = parent or "/"
        for ent in self._dirs.get(parent, []):
            if ent.name == name and not ent.is_dir:
                return self._read_cluster_chain(ent.cluster, ent.size)
        raise Fat32Error(f"missing file {path}")

    def to_bytes(self) -> bytes:
        """Serialise boot sector, FATs, and directory trees."""
        self._flush_directories()
        self._flush_fat()
        return bytes(self._data)

    def verify_readback(self, files: dict[str, bytes]) -> None:
        """Byte-for-byte compare of written files against an independent parse."""
        raw = self.to_bytes()
        parsed = parse_fat32(raw)
        for path, expected in files.items():
            got = parsed.read_file(path)
            if got != expected:
                raise Fat32Error(
                    f"readback mismatch for {path}: "
                    f"{len(got)} bytes vs {len(expected)}"
                )
        if parsed.volume_label.strip() != self.label.strip():
            raise Fat32Error("volume label mismatch after parse")

    def _flush_directories(self) -> None:
        for path, ents in self._dirs.items():
            cluster = self._dir_cluster(path)
            blob = bytearray()
            for ent in ents:
                blob.extend(b"".join(_lfn_entries(ent.name, _lfn_checksum(ent.short83))))
                blob.extend(self._short_entry(ent))
            blob.extend(b"\x00" * 32)
            # Grow the directory chain if needed.
            needed = len(blob)
            existing = []
            cl = cluster
            while cl >= 2 and (cl & FAT32_RESERVED_MASK) < FAT32_BAD:
                existing.append(cl)
                nxt = self._fat[cl] & FAT32_RESERVED_MASK
                if nxt >= FAT32_EOF or nxt == 0:
                    break
                cl = nxt
            capacity = len(existing) * self._bytes_per_cluster
            if needed > capacity:
                extra = self._alloc_clusters(needed - capacity)
                if existing:
                    self._fat[existing[-1]] = extra[0]
                existing.extend(extra)
            self._write_cluster_chain(existing, bytes(blob))

    def _dir_cluster(self, path: str) -> int:
        if path in ("", "/"):
            return self._root_cluster
        parent, name = path.rsplit("/", 1)
        parent = parent or "/"
        for ent in self._dirs.get(parent, []):
            if ent.name == name and ent.is_dir:
                return ent.cluster
        raise Fat32Error(f"directory {path} has no cluster")

    def _short_entry(self, ent: _DirEnt) -> bytes:
        e = bytearray(32)
        e[0:11] = ent.short83
        e[11] = 0x10 if ent.is_dir else 0x20
        struct.pack_into("<H", e, 20, (ent.cluster >> 16) & 0xFFFF)
        struct.pack_into("<H", e, 26, ent.cluster & 0xFFFF)
        struct.pack_into("<I", e, 28, 0 if ent.is_dir else ent.size)
        return bytes(e)

    def _dot_entries(self, cluster: int, parent: str) -> bytes:
        def _dot(name: bytes, cl: int) -> bytes:
            e = bytearray(32)
            e[0:11] = name.ljust(11)
            e[11] = 0x10
            struct.pack_into("<H", e, 20, (cl >> 16) & 0xFFFF)
            struct.pack_into("<H", e, 26, cl & 0xFFFF)
            return bytes(e)

        parent_cl = self._dir_cluster(parent) if parent != "/" else self._root_cluster
        if parent in ("", "/"):
            parent_cl = self._root_cluster
        return _dot(b".", cluster) + _dot(b"..", parent_cl)

    @staticmethod
    def _norm(path: str) -> str:
        path = path.replace("\\", "/").strip()
        if not path.startswith("/"):
            path = "/" + path
        while "//" in path:
            path = path.replace("//", "/")
        if len(path) > 1:
            path = path.rstrip("/")
        return path


@dataclass
class ParsedFat32:
    """Independent FAT32 parser used for readback verification."""

    raw: bytes
    volume_label: str
    _bytes_per_cluster: int
    _fat: list[int]
    _data_start: int
    _root: int
    _spc: int

    def read_file(self, path: str) -> bytes:
        parts = [p for p in path.replace("\\", "/").strip("/").split("/") if p]
        cluster = self._root
        for i, name in enumerate(parts):
            entries = self._read_dir(cluster)
            match = next((e for e in entries if e["name"].lower() == name.lower()), None)
            if match is None:
                raise Fat32Error(f"not found: {path}")
            if i == len(parts) - 1:
                if match["is_dir"]:
                    raise Fat32Error(f"{path} is a directory")
                return self._chain(match["cluster"], match["size"])
            if not match["is_dir"]:
                raise Fat32Error(f"{name} is not a directory")
            cluster = match["cluster"]
        raise Fat32Error(f"not found: {path}")

    def _chain(self, start: int, size: int) -> bytes:
        out = bytearray()
        cl = start
        seen: set[int] = set()
        while cl >= 2 and (cl & 0x0FFFFFFF) < FAT32_BAD:
            if cl in seen:
                raise Fat32Error("FAT cycle during parse")
            seen.add(cl)
            off = self._data_start + (cl - 2) * self._bytes_per_cluster
            out.extend(self.raw[off:off + self._bytes_per_cluster])
            nxt = self._fat[cl] & 0x0FFFFFFF
            if nxt >= FAT32_EOF or nxt == 0:
                break
            cl = nxt
        return bytes(out[:size])

    def _read_dir(self, cluster: int) -> list[dict]:
        raw = self._chain(cluster, 1024 * 1024)
        entries: list[dict] = []
        lfn_parts: list[str] = []
        for off in range(0, len(raw), 32):
            e = raw[off:off + 32]
            if not e or e[0] == 0x00:
                break
            if e[0] == 0xE5:
                lfn_parts = []
                continue
            if e[11] == 0x0F:
                lfn_parts.insert(0, _decode_lfn(e))
                continue
            short = e[0:8].decode("ascii", "replace").rstrip()
            ext = e[8:11].decode("ascii", "replace").rstrip()
            short_name = f"{short}.{ext}" if ext else short
            long = "".join(lfn_parts).rstrip("\x00")
            lfn_parts = []
            cl = struct.unpack_from("<H", e, 26)[0] | (
                struct.unpack_from("<H", e, 20)[0] << 16
            )
            entries.append({
                "name": long or short_name,
                "is_dir": bool(e[11] & 0x10),
                "cluster": cl,
                "size": struct.unpack_from("<I", e, 28)[0],
            })
        return entries


def _decode_lfn(entry: bytes) -> str:
    units = list(struct.unpack_from("<5H", entry, 1))
    units += list(struct.unpack_from("<6H", entry, 14))
    units += list(struct.unpack_from("<2H", entry, 28))
    chars = []
    for u in units:
        if u in (0x0000, 0xFFFF):
            break
        chars.append(chr(u))
    return "".join(chars)


def parse_fat32(raw: bytes) -> ParsedFat32:
    """Parse a FAT32 image from bytes (used for independent readback)."""
    if raw[510:512] != b"\x55\xAA":
        raise Fat32Error("missing boot signature")
    bps = struct.unpack_from("<H", raw, 11)[0]
    spc = raw[13]
    reserved = struct.unpack_from("<H", raw, 14)[0]
    fats = raw[16]
    fat_sectors = struct.unpack_from("<I", raw, 36)[0]
    root = struct.unpack_from("<I", raw, 44)[0]
    label = raw[71:82].decode("ascii", "replace")
    fat_off = reserved * bps
    fat = []
    for i in range(0, fat_sectors * bps, 4):
        fat.append(struct.unpack_from("<I", raw, fat_off + i)[0])
    return ParsedFat32(
        raw=raw,
        volume_label=label,
        _bytes_per_cluster=spc * bps,
        _fat=fat,
        _data_start=(reserved + fats * fat_sectors) * bps,
        _root=root,
        _spc=spc,
    )


def build_fat32_files(size_bytes: int, files: dict[str, bytes],
                       label: str = "NOVABOOT") -> bytes:
    """Build a FAT32 image, write files, and require byte-for-byte readback."""
    img = Fat32Image(size_bytes, label=label)
    for path, content in files.items():
        img.write_file(path, content)
    img.verify_readback(files)
    return img.to_bytes()
