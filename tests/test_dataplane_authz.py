"""Authorization, listing, delegation and no-side-effect denials."""

from __future__ import annotations

import time

import pytest

from security.capabilities import CapabilityStore
from store.dataplane import DataPlane, DataPlaneError


def test_garbage_token_does_not_create_handle(sos):
    caps = CapabilityStore(sos)
    plane = DataPlane(sos, caps, enforce=True)
    plane.use_token("not-a-real-token")
    with pytest.raises(DataPlaneError):
        plane.put("secret", "nope")
    assert sos.resolve("@secret") is None


def test_fail_closed_without_caps(sos):
    plane = DataPlane(sos, None, enforce=True)
    with pytest.raises(DataPlaneError):
        plane.put("x", "nope")
    assert sos.resolve("@x") is None


def test_find_intersects_and_hides_unauthorized(sos):
    caps = CapabilityStore(sos)
    owner = DataPlane(sos, caps, enforce=False)
    owner.put("alpha", "1", tags=["keep"], kind="text")
    owner.put("beta", "2", tags=["keep"], kind="code")
    locked = DataPlane(sos, caps, enforce=True)
    cap = caps.grant("@alpha", {"read", "write"})
    locked.use_token(cap.token)
    rows = locked.find(tag="keep", kind="text")
    handles = {r["handle"] for r in rows}
    assert "alpha" in handles
    assert "beta" not in handles


def test_oid_read_requires_authority(sos):
    caps = CapabilityStore(sos)
    plane = DataPlane(sos, caps, enforce=False)
    rec = plane.put("named", "body")
    locked = DataPlane(sos, caps, enforce=True)
    with pytest.raises(DataPlaneError):
        locked.get(rec.oid)
    cap = caps.grant(plane.alias_of("named"), {"read"})
    locked.use_token(cap.token)
    assert locked.get(rec.oid).content == "body"


def test_lockdown_persists_and_restart_is_not_unrestricted(sos):
    caps = CapabilityStore(sos)
    first = DataPlane(sos, caps, enforce=False)
    token = first.bootstrap_admin("root")
    restarted = DataPlane(sos, caps, enforce=False)
    restarted.load_policy()
    assert restarted.enforce is True
    with pytest.raises(DataPlaneError):
        restarted.put("ghost", "nope")
    assert sos.resolve("@ghost") is None
    restarted.use_token(token)
    rec = restarted.put("ghost", "yes")
    assert rec.handle == "ghost"


def test_revoked_token_cannot_read(sos):
    caps = CapabilityStore(sos)
    plane = DataPlane(sos, caps, enforce=False)
    plane.put("doc", "body")
    cap = caps.grant("@doc", {"read", "write"})
    locked = DataPlane(sos, caps, enforce=True)
    locked.use_token(cap.token)
    assert locked.get("doc").content == "body"
    caps.revoke(cap.token)
    with pytest.raises(DataPlaneError):
        locked.get("doc")


def test_child_invalid_when_parent_expires(sos):
    caps = CapabilityStore(sos)
    parent = caps.grant(
        "@doc", {"read", "write", "grant"}, ttl=0.2, delegate_depth=2
    )
    child = caps.delegate(parent.token, {"read"}, owner="alice", ttl=3600)
    assert child is not None
    assert caps.check(child.token, "read", path="@doc")
    time.sleep(0.25)
    assert not caps.check(parent.token, "read", path="@doc")
    assert not caps.check(child.token, "read", path="@doc")

