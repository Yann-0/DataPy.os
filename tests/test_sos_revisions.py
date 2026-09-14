"""Revision identity, dedup, reopen and migration regressions (DP-09)."""

from __future__ import annotations

import os
import sqlite3

from store.migration import migrate_copy
from store.sos import SemanticObjectStore, WriteAck, _oid


def test_identical_content_different_handles(sos):
    a = sos.write("/h/a", "same-bytes", tags=["one"])
    b = sos.write("/h/b", "same-bytes", tags=["two"])
    assert a == b
    assert "one" in sos.get_tags("/h/a")
    assert "two" in sos.get_tags("/h/b")
    assert "two" not in sos.get_tags("/h/a")


def test_aba_history_survives_reopen(tmp_path):
    db = str(tmp_path / "rev.db")
    sos = SemanticObjectStore(db_path=db)
    sos.write("/cycle.txt", "A")
    sos.write("/cycle.txt", "B")
    sos.write("/cycle.txt", "A")
    hist = sos.history("/cycle.txt")
    assert [h.text for h in hist] == ["A", "B", "A"]
    assert hist[0].version == 3
    assert hist[0].parent_oid == _oid(b"B")
    ack = sos.last_ack
    assert isinstance(ack, WriteAck)
    assert ack.durable and not ack.noop
    sos2 = SemanticObjectStore(db_path=db)
    hist2 = sos2.history("/cycle.txt")
    assert [h.text for h in hist2] == ["A", "B", "A"]
    assert hist2[0].version == 3


def test_noop_same_value_write(sos):
    oid1 = sos.write("/n/x", "once", tags=["k"])
    ack1 = sos.last_ack
    oid2 = sos.write("/n/x", "once", tags=["k"])
    assert oid1 == oid2
    assert sos.last_ack.noop
    assert sos.last_ack.rev_id == ack1.rev_id
    assert len(sos.history("/n/x")) == 1


def test_concurrent_writes_distinct_handles(sos):
    errors = []

    def worker(i: int) -> None:
        try:
            sos.write(f"/c/h{i}", f"body-{i}")
        except Exception as exc:
            errors.append(exc)

    threads = [__import__("threading").Thread(target=worker, args=(i,))
               for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sos.read("/c/h0") == "body-0"
    assert sos.read("/c/h7") == "body-7"


def test_keyword_search_limit_alias(sos):
    for i in range(8):
        sos.write(f"/s/f{i}.txt", f"needle unique {i}")
    assert len(sos.keyword_search("needle", limit=3)) <= 3


def test_migration_copy_does_not_touch_original(tmp_path):
    src = str(tmp_path / "legacy.db")
    sos = SemanticObjectStore(db_path=src)
    sos.write("/m/a", "v1")
    sos.write("/m/a", "v2")
    orig_stat = os.stat(src)
    dest = str(tmp_path / "legacy.db.migrated")
    report = migrate_copy(src, dest)
    assert report.ok
    assert os.stat(src).st_mtime == orig_stat.st_mtime
    assert os.path.isfile(dest)
