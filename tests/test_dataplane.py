"""Tests for the folder-free DataPlane CRUD surface."""

from __future__ import annotations

import pytest

from store.dataplane import DataPlane, DataPlaneError
from security.capabilities import CapabilityStore


@pytest.fixture
def plane(sos):
    """Return an unlocked DataPlane on an isolated SOS."""
    caps = CapabilityStore(sos)
    return DataPlane(sos, caps, enforce=False)


def test_put_get_roundtrip(plane):
    rec = plane.put("note", "hello", kind="text", tags=["ideas"])
    assert rec.handle == "note"
    assert plane.get("note").content == "hello"
    assert "ideas" in plane.get("note").tags


def test_rejects_folder_handles(plane):
    with pytest.raises(DataPlaneError):
        plane.put("docs/readme", "nope")


def test_update_and_delete(plane):
    plane.put("x", "v1")
    assert plane.update("x", "v2").version >= 2
    oid = plane.delete("x")
    assert oid
    with pytest.raises(DataPlaneError):
        plane.get("x")


def test_find_by_tag(plane):
    plane.put("a", "1", tags=["alpha"])
    plane.put("b", "2", tags=["beta"])
    rows = plane.find(tag="alpha")
    handles = {r["handle"] for r in rows}
    assert "a" in handles
    assert "b" not in handles


def test_link_related(plane):
    plane.put("src", "s")
    plane.put("dst", "d")
    plane.link("src", "dst", "mentions")
    related = plane.related("src")
    assert any("mentions" in str(r) for r in related)


def test_lockdown_requires_token(sos):
    caps = CapabilityStore(sos)
    plane = DataPlane(sos, caps, enforce=True)
    with pytest.raises(DataPlaneError):
        plane.put("secret", "nope")
    plane.lockdown(False)
    plane.put("secret", "ok")
    plane.use_token(None)  # drop bootstrap token
    plane.lockdown(True)
    with pytest.raises(DataPlaneError):
        plane.get("secret")
    cap = caps.grant(plane.alias_of("secret"), {"read", "write", "delete"})
    plane.use_token(cap.token)
    assert plane.get("secret").content == "ok"


def test_seed_core(plane):
    plane.seed_core()
    assert plane.get("welcome").kind == "text"
