"""Erasure reports verified effects, not OID/path mix-ups."""

from __future__ import annotations

from system.enterprise import ComplianceManager


def test_erase_user_uses_paths_and_counts_verified(sos):
    sos.write("/users/alice/note", "private", tags=["alice"])
    sos.write("/users/bob/note", "other", tags=["bob"])
    cm = ComplianceManager(sos)
    report = cm.erase_user("alice")
    assert report["erased"] >= 1
    assert report["complete_erasure"] is False
    assert not sos.exists("/users/alice/note")
    assert sos.exists("/users/bob/note")
    assert "soft_delete" in report
