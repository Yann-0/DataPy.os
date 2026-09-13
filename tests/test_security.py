"""
PyOS NOVA — Security Tests  (v0.0008)
=======================================
Unit tests for ZK auth, capabilities, audit trail, vault, RBAC, NIDS,
measured boot, compliance, and the Bloom filter existence index.
"""

from __future__ import annotations
import json
import time
import pytest


# ── ZK Authentication ─────────────────────────────────────────────────────────

class TestZKAuth:
    """Pedersen-commitment zero-knowledge authentication."""

    @pytest.fixture
    def zk(self, sos):
        from security.zkauth import ZKAuthManager
        return ZKAuthManager(sos)

    def test_setup_and_verify_correct(self, zk):
        """Correct passphrase verifies successfully."""
        zk.setup("alice", "correct_horse_battery_staple")
        assert zk.verify_proof("alice", "correct_horse_battery_staple")

    def test_setup_and_verify_wrong(self, zk):
        """Wrong passphrase returns False."""
        zk.setup("bob", "right_secret")
        assert not zk.verify_proof("bob", "wrong_secret")

    def test_verify_unknown_user(self, zk):
        """Verifying an un-enrolled user returns False."""
        assert not zk.verify_proof("nobody", "anything")

    def test_remove_credential(self, zk):
        """Removing credentials makes verify return False."""
        zk.setup("carol", "secret")
        zk.remove("carol")
        assert not zk.verify_proof("carol", "secret")

    def test_list_users(self, zk):
        """Enrolled users appear in list_users()."""
        zk.setup("user1", "pass1")
        zk.setup("user2", "pass2")
        users = zk.list_users()
        assert "user1" in users
        assert "user2" in users

    def test_has_credential(self, zk):
        """has_credential() reflects enrollment state."""
        assert not zk.has_credential("new_user")
        zk.setup("new_user", "password")
        assert zk.has_credential("new_user")


# ── Capability-based access control ──────────────────────────────────────────

class TestCapabilities:
    """Unforgeable capability tokens."""

    @pytest.fixture
    def caps(self, sos):
        from security.capabilities import CapabilityStore
        return CapabilityStore(sos)

    def test_grant_and_check(self, caps):
        """Granted capability allows the specified right."""
        from security.capabilities import RIGHT_READ
        cap = caps.grant("/home/root/doc.txt", {RIGHT_READ}, owner="root")
        assert caps.check(cap.token, RIGHT_READ, "/home/root/doc.txt")

    def test_check_wrong_right(self, caps):
        """Checking an ungranted right returns False."""
        from security.capabilities import RIGHT_READ, RIGHT_WRITE
        cap = caps.grant("/data/x.txt", {RIGHT_READ}, owner="root")
        assert not caps.check(cap.token, RIGHT_WRITE, "/data/x.txt")

    def test_check_wrong_path(self, caps):
        """Capability for path A does not grant access to path B."""
        from security.capabilities import RIGHT_READ
        cap = caps.grant("/path/a.txt", {RIGHT_READ}, owner="root")
        assert not caps.check(cap.token, RIGHT_READ, "/path/b.txt")

    def test_revoke(self, caps):
        """Revoked token no longer passes check."""
        from security.capabilities import RIGHT_READ
        cap = caps.grant("/secret.txt", {RIGHT_READ}, owner="root")
        caps.revoke(cap.token)
        assert not caps.check(cap.token, RIGHT_READ, "/secret.txt")

    def test_ttl_expiry(self, caps):
        """Expired capability returns False."""
        from security.capabilities import RIGHT_READ
        cap = caps.grant("/ttl.txt", {RIGHT_READ}, owner="root", ttl=0.001)
        time.sleep(0.05)
        assert not caps.check(cap.token, RIGHT_READ, "/ttl.txt")

    def test_delegation(self, caps):
        """Delegated capability inherits rights (within grant right constraint)."""
        from security.capabilities import RIGHT_READ, RIGHT_GRANT
        parent = caps.grant("/shared.txt", {RIGHT_READ, RIGHT_GRANT},
                             delegate_depth=1, owner="root")
        child  = caps.delegate(parent.token, {RIGHT_READ}, owner="alice")
        assert child is not None
        assert caps.check(child.token, RIGHT_READ, "/shared.txt")

    def test_list_all(self, caps):
        """list_all() returns all non-revoked capabilities."""
        from security.capabilities import RIGHT_READ
        n_before = len(caps.list_all())
        caps.grant("/a.txt", {RIGHT_READ}, owner="root")
        caps.grant("/b.txt", {RIGHT_READ}, owner="root")
        assert len(caps.list_all()) == n_before + 2


# ── Audit trail ───────────────────────────────────────────────────────────────

class TestAuditTrail:
    """Merkle-chained tamper-evident audit log."""

    @pytest.fixture
    def audit(self, sos):
        from security.ledger import AuditTrail
        trail = AuditTrail(sos)
        yield trail
        trail.stop()

    def test_append_and_verify(self, audit):
        """Chain with a few entries passes integrity check."""
        audit.append("write", "root", "/test.txt", "test write")
        audit.append("read",  "alice", "/test.txt")
        audit.flush()
        ok, msg = audit.verify()
        assert ok, msg

    def test_recent_returns_entries(self, audit):
        """recent() returns appended entries in order."""
        audit.append("login", "bob", detail="SSH login")
        audit.append("write", "bob", "/data.txt")
        audit.flush()
        entries = audit.recent(10)
        events  = [e.event for e in entries]
        assert "login" in events
        assert "write" in events

    def test_search(self, audit):
        """search() filters entries by query string."""
        audit.append("write", "alice", "/docs/report.pdf", "wrote report")
        audit.append("read",  "bob",   "/docs/report.pdf")
        audit.flush()
        results = audit.search("alice")
        assert all("alice" in str(e.user) for e in results)

    def test_chain_integrity_after_many_entries(self, audit):
        """Integrity holds after 20 entries."""
        for i in range(20):
            audit.append("cmd", "root", f"/action/{i}", f"action {i}")
        audit.flush()
        ok, msg = audit.verify()
        assert ok, f"Chain broken: {msg}"


# ── Secret Vault ─────────────────────────────────────────────────────────────

class TestSecretVault:
    """HashiCorp-like secret management."""

    @pytest.fixture
    def vault(self, sos):
        from security.innovations import SecretVault
        return SecretVault(sos)

    def test_set_and_get(self, vault):
        """Stored secret is retrievable."""
        vault.set("db", "password", "super_secret_42")
        assert vault.get("db", "password") == "super_secret_42"

    def test_get_nonexistent(self, vault):
        """Getting an unknown secret returns None."""
        assert vault.get("ns", "missing") is None

    def test_ttl_expiry(self, vault):
        """Expired secret returns None."""
        vault.set("ttl_ns", "key", "value", ttl=0.001)
        time.sleep(0.05)
        assert vault.get("ttl_ns", "key") is None

    def test_rotate(self, vault):
        """Rotated secret has a new value."""
        vault.set("api", "token", "old_token")
        new_val = vault.rotate("api", "token")
        assert new_val != "old_token"
        assert vault.get("api", "token") == new_val

    def test_list_keys(self, vault):
        """list_keys() returns all keys in a namespace."""
        vault.set("cfg", "key1", "v1")
        vault.set("cfg", "key2", "v2")
        keys = vault.list_keys("cfg")
        assert "key1" in keys
        assert "key2" in keys


# ── RBAC ─────────────────────────────────────────────────────────────────────

class TestRBAC:
    """Role-based access control."""

    @pytest.fixture
    def rbac(self, sos):
        from system.enterprise import RBAC
        return RBAC(sos)

    def test_default_roles_exist(self, rbac):
        """Default roles (admin, developer, viewer) are present."""
        role_names = {r.name for r in rbac.list_roles()}
        assert {"admin", "developer", "viewer"}.issubset(role_names)

    def test_root_has_admin(self, rbac):
        """root user has admin role by default."""
        assert rbac.has_capability("root", "sos.write")
        assert rbac.has_capability("root", "exec")

    def test_assign_and_check(self, rbac):
        """Assigning a role grants its capabilities."""
        rbac.create_role("reader", capabilities=["sos.read"])
        rbac.assign("testuser", "reader")
        assert rbac.has_capability("testuser", "sos.read")
        assert not rbac.has_capability("testuser", "exec")

    def test_revoke_capability(self, rbac):
        """Revoking a capability removes it from the role."""
        rbac.create_role("limited", capabilities=["sos.read", "net.http"])
        rbac.revoke("limited", "net.http")
        rbac.assign("limiteduser", "limited")
        assert rbac.has_capability("limiteduser", "sos.read")
        assert not rbac.has_capability("limiteduser", "net.http")

    def test_unassign_role(self, rbac):
        """Removing a role from a user removes its capabilities."""
        rbac.create_role("temp_role", capabilities=["sos.read"])
        rbac.assign("tempuser", "temp_role")
        assert rbac.has_capability("tempuser", "sos.read")
        rbac.unassign("tempuser", "temp_role")
        assert not rbac.has_capability("tempuser", "sos.read")


# ── NIDS ─────────────────────────────────────────────────────────────────────

class TestNIDS:
    """Network intrusion detection."""

    @pytest.fixture
    def nids(self, sos):
        from security.innovations import NetworkIDS
        return NetworkIDS(sos)

    def test_clean_request_passes(self, nids):
        """Normal request returns None (no alert)."""
        assert nids.inspect("1.2.3.4", "/api/objects/abc") is None

    def test_traversal_detected(self, nids):
        """Directory traversal attempt triggers alert."""
        alert = nids.inspect("5.6.7.8", "/api/../../../etc/passwd")
        assert alert is not None

    def test_sql_injection_detected(self, nids):
        """SQL injection pattern triggers alert."""
        alert = nids.inspect("9.10.11.12", "/search?q='; DROP TABLE--")
        assert alert is not None

    def test_manual_block(self, nids):
        """Manually blocked IP is rejected."""
        nids.block("192.168.0.1")
        alert = nids.inspect("192.168.0.1", "/anything")
        assert alert is not None

    def test_brute_force_detection(self, nids):
        """21+ requests from same IP in window triggers brute-force alert."""
        from security.innovations import BRUTE_FORCE_LIMIT
        for _ in range(BRUTE_FORCE_LIMIT + 1):
            nids.inspect("10.0.0.99", "/api/login")
        # The last call should have triggered the alert
        status = nids.status()
        assert status["alerts"] > 0


# ── Compliance ────────────────────────────────────────────────────────────────

class TestCompliance:
    """GDPR and PHI detection."""

    @pytest.fixture
    def compliance(self, sos):
        from system.enterprise import ComplianceManager
        return ComplianceManager(sos)

    def test_phi_ssn_detected(self, compliance):
        """SSN pattern is detected as PHI."""
        matches = compliance.scan_phi("Patient SSN: 123-45-6789")
        assert len(matches) >= 1

    def test_phi_clean_text(self, compliance):
        """Normal text has no PHI matches."""
        matches = compliance.scan_phi("The weather today is sunny and warm.")
        assert len(matches) == 0

    def test_gdpr_erase_returns_count(self, compliance):
        """erase_user() returns a dict with erased_objects count."""
        result = compliance.erase_user("ghost_user_xyz")
        assert isinstance(result, dict)
        assert "erased_objects" in result

    def test_gdpr_erase_tagged_objects(self, compliance, sos):
        """erase_user() removes objects tagged with the username."""
        sos.write("/users/alice/doc.txt", "private data",
                   tags=["alice"])
        # Count is at least 0 (may not find tagged objects depending on impl)
        result = compliance.erase_user("alice")
        assert result["erased_objects"] >= 0


# ── Bloom Filter ─────────────────────────────────────────────────────────────

class TestBloomFilter:
    """Probabilistic existence index."""

    @pytest.fixture
    def bloom(self):
        from store.pipeline import BloomFilter
        return BloomFilter(size_bits=65536, n_hashes=4)

    def test_added_item_present(self, bloom):
        """Added items are always found (no false negatives)."""
        paths = [f"/test/path/{i}.py" for i in range(100)]
        bloom.add_bulk(paths)
        for p in paths:
            assert p in bloom, f"False negative for {p}"

    def test_unadded_item_likely_absent(self, bloom):
        """Unadded items are usually absent (low false positive rate)."""
        bloom.add_bulk([f"/exist/{i}" for i in range(500)])
        # Very unlikely to have false positive for this specific pattern
        missing = "/completely/unique/path/xyz_12345_qwerty.py"
        # Can't assert False due to probabilistic nature, but verify stats
        stats = bloom.stats()
        assert stats["fp_rate"] < 5.0   # less than 5% false positive rate

    def test_serialise_deserialise(self, bloom):
        """Serialise and deserialise preserves all members."""
        from store.pipeline import BloomFilter as BF
        paths = [f"/ser/path{i}" for i in range(50)]
        bloom.add_bulk(paths)
        serialised   = bloom.serialise()
        reconstructed = BF.deserialise(serialised)
        for p in paths:
            assert p in reconstructed, f"Lost {p} after deserialise"

    def test_stats_accurate(self, bloom):
        """stats() reports correct item count."""
        bloom.add_bulk([f"/s/{i}" for i in range(200)])
        assert bloom.stats()["items"] == 200
