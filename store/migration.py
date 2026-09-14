"""
Versioned SOS migration: blobs stay content-addressed; revisions are unique.

Always operate on a COPY of a legacy database. The original file is never
opened read-write. Irrecoverable history is reported; the copy is rolled
back if the migration cannot complete cleanly.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass, field

log = logging.getLogger("nova.store.migration")

SCHEMA_VERSION = 3

REVISION_DDL = """
CREATE TABLE IF NOT EXISTS nova_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS blobs (
    oid TEXT PRIMARY KEY,
    content BLOB NOT NULL,
    size INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS revisions (
    rev_id TEXT PRIMARY KEY,
    blob_oid TEXT NOT NULL,
    path TEXT NOT NULL,
    parent_rev TEXT,
    kind TEXT NOT NULL DEFAULT 'text',
    meta_json TEXT NOT NULL DEFAULT '{}',
    tags_json TEXT NOT NULL DEFAULT '[]',
    links_json TEXT NOT NULL DEFAULT '[]',
    version INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_rev_path ON revisions(path, version);
CREATE INDEX IF NOT EXISTS idx_rev_blob ON revisions(blob_oid);
CREATE TABLE IF NOT EXISTS tag_by_path (
    tag TEXT NOT NULL,
    path TEXT NOT NULL,
    PRIMARY KEY (tag, path)
);
"""


@dataclass
class MigrationReport:
    """Outcome of a legacy-database migration."""

    source: str
    copy: str
    schema_version: int
    blobs: int = 0
    revisions: int = 0
    aliases: int = 0
    cycles: list[str] = field(default_factory=list)
    missing_parents: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    ok: bool = False


def schema_version(conn: sqlite3.Connection) -> int:
    """Return the stored schema version, or 1 for a pre-revision database."""
    try:
        row = conn.execute(
            "SELECT value FROM nova_meta WHERE key='schema_version'"
        ).fetchone()
        if row:
            return int(row[0])
    except sqlite3.OperationalError:
        pass
    return 1


def ensure_revision_tables(conn: sqlite3.Connection) -> None:
    """Create revision tables on a live connection (idempotent)."""
    conn.executescript(REVISION_DDL)
    try:
        conn.execute("ALTER TABLE aliases ADD COLUMN head_rev TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE aliases ADD COLUMN deleted INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass


def _walk_parent_chain(conn: sqlite3.Connection, start_oid: str) -> tuple[list[str], bool]:
    """Walk objects.parent_oid. Returns (chain oldest-first, cycle_detected)."""
    chain: list[str] = []
    seen: set[str] = set()
    oid: str | None = start_oid
    while oid:
        if oid in seen:
            return chain, True
        seen.add(oid)
        row = conn.execute("SELECT parent_oid FROM objects WHERE oid=?", (oid,)).fetchone()
        if row is None:
            chain.append(oid)
            break
        chain.append(oid)
        oid = row[0]
    chain.reverse()
    return chain, False


def migrate_copy(source_db: str, dest_db: str | None = None) -> MigrationReport:
    """Copy ``source_db`` and migrate the copy. Never writes the original."""
    if not os.path.isfile(source_db):
        raise FileNotFoundError(source_db)
    dest = dest_db or (source_db + ".migrated")
    shutil.copy2(source_db, dest)
    wal = source_db + "-wal"
    shm = source_db + "-shm"
    if os.path.isfile(wal):
        shutil.copy2(wal, dest + "-wal")
    if os.path.isfile(shm):
        shutil.copy2(shm, dest + "-shm")
    report = MigrationReport(source=source_db, copy=dest, schema_version=SCHEMA_VERSION)
    conn = sqlite3.connect(dest)
    conn.row_factory = sqlite3.Row
    try:
        if schema_version(conn) >= SCHEMA_VERSION:
            report.ok = True
            return report
        ensure_revision_tables(conn)
        conn.execute("BEGIN")
        # Blobs: one row per content OID. Preserve 32-hex OIDs.
        for row in conn.execute("SELECT oid, content, size FROM objects"):
            conn.execute(
                "INSERT OR IGNORE INTO blobs (oid, content, size) VALUES (?,?,?)",
                (row["oid"], row["content"], row["size"]),
            )
            report.blobs += 1
        alias_rows = list(conn.execute("SELECT path, oid, is_dir FROM aliases"))
        for alias in alias_rows:
            path, head_oid = alias["path"], alias["oid"]
            chain, cyclic = _walk_parent_chain(conn, head_oid)
            if cyclic:
                report.cycles.append(path)
            parent_rev = None
            version = 0
            last_rev = None
            for blob_oid in chain:
                obj = conn.execute(
                    "SELECT * FROM objects WHERE oid=?", (blob_oid,)
                ).fetchone()
                if obj is None:
                    report.missing_parents.append(f"{path}:{blob_oid}")
                    continue
                version += 1
                rev_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO revisions "
                    "(rev_id,blob_oid,path,parent_rev,kind,meta_json,tags_json,"
                    "links_json,version,created_at,deleted) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,0)",
                    (
                        rev_id, blob_oid, path, parent_rev, obj["kind"],
                        obj["meta_json"], obj["tags_json"], obj["links_json"],
                        version, obj["created_at"],
                    ),
                )
                tags = json.loads(obj["tags_json"] or "[]")
                for tag in tags:
                    conn.execute(
                        "INSERT OR IGNORE INTO tag_by_path (tag, path) VALUES (?,?)",
                        (tag, path),
                    )
                parent_rev = rev_id
                last_rev = rev_id
                report.revisions += 1
            if last_rev is not None:
                conn.execute(
                    "UPDATE aliases SET head_rev=? WHERE path=?",
                    (last_rev, path),
                )
                report.aliases += 1
        if report.cycles or report.missing_parents:
            conn.execute("ROLLBACK")
            conn.close()
            os.remove(dest)
            for extra in (dest + "-wal", dest + "-shm"):
                if os.path.isfile(extra):
                    os.remove(extra)
            report.ok = False
            report.errors.append(
                "irrecoverable legacy history: cycles="
                f"{report.cycles} missing={report.missing_parents}"
            )
            return report
        conn.execute(
            "INSERT OR REPLACE INTO nova_meta(key,value) VALUES ('schema_version',?)",
            (str(SCHEMA_VERSION),),
        )
        conn.execute("COMMIT")
        report.ok = True
        return report
    except Exception as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        conn.close()
        if os.path.isfile(dest):
            os.remove(dest)
        report.ok = False
        report.errors.append(str(exc))
        log.exception("migration failed")
        return report
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
