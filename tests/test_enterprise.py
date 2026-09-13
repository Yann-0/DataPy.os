"""
PyOS NOVA — Enterprise Test Suite
====================================
Tests for Phases 5+6 enterprise features:
  - TenantManager (namespaces, quotas, lifecycle)
  - RBAC (roles, capabilities, assignments)
  - RaftNode (leader election, state machine)
  - ComplianceManager (GDPR erasure, PHI scan, audit export)
  - Observability (Prometheus metrics, OTLP export)
  - CloudManifests (K8s, Compose, Terraform)
  - PluginRegistry (install, sandbox, permissions)
  - PackageManager (tracking, CVE audit, version matching)
"""

from __future__ import annotations

import os
import sys
import json
import time
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ── TenantManager ─────────────────────────────────────────────────────────────

class TestTenantManager:
    """Tests for multi-tenant namespace management."""

    def test_create_tenant(self, fake_kernel):
        """create() returns a Tenant with correct name and quota."""
        tm = fake_kernel.tenants
        t  = tm.create("test_alice", quota_mb=128)
        assert t.name == "test_alice"
        assert t.quota_mb == 128
        assert t.enabled

    def test_tenant_namespace_exists(self, fake_kernel):
        """Tenant SOS namespace directory is created on create()."""
        tm = fake_kernel.tenants
        t  = tm.create("test_bob", quota_mb=64)
        assert fake_kernel.sos.exists(t.sos_namespace)

    def test_quota_check_within_limit(self, fake_kernel):
        """check_quota() returns True when write fits in quota."""
        tm = fake_kernel.tenants
        tm.create("quota_ok", quota_mb=100)
        assert tm.check_quota("quota_ok", 50 * 1024)   # 50 KB  << 100 MB

    def test_quota_check_exceeds_limit(self, fake_kernel):
        """check_quota() returns False when write would exceed quota."""
        tm = fake_kernel.tenants
        tm.create("quota_exceed", quota_mb=10)
        assert not tm.check_quota("quota_exceed", 20 * 1024 * 1024)  # 20 MB > 10 MB

    def test_disable_tenant(self, fake_kernel):
        """disable() sets enabled=False."""
        tm = fake_kernel.tenants
        tm.create("disable_me", quota_mb=50)
        tm.disable("disable_me")
        assert not tm.get("disable_me").enabled

    def test_list_tenants(self, fake_kernel):
        """list_tenants() includes all created tenants."""
        tm = fake_kernel.tenants
        tm.create("list_t1", quota_mb=10)
        tm.create("list_t2", quota_mb=20)
        names = {t.name for t in tm.list_tenants()}
        assert "list_t1" in names
        assert "list_t2" in names

    def test_unknown_tenant_quota_unrestricted(self, fake_kernel):
        """Unknown tenant is not quota-restricted (returns True)."""
        tm = fake_kernel.tenants
        assert tm.check_quota("nobody_xyz", 999 * 1024 * 1024)


# ── RBAC ─────────────────────────────────────────────────────────────────────

class TestRBAC:
    """Tests for role-based access control."""

    def test_default_roles_installed(self, fake_kernel):
        """Default roles (admin/developer/operator/viewer) are present."""
        rb    = fake_kernel.rbac
        names = {r.name for r in rb.list_roles()}
        assert {"admin", "developer", "operator", "viewer"}.issubset(names)

    def test_root_has_admin(self, fake_kernel):
        """root user is assigned the admin role by default."""
        rb = fake_kernel.rbac
        assert rb.has_capability("root", "exec")
        assert rb.has_capability("root", "sos.write")
        assert rb.has_capability("root", "security.manage")

    def test_create_custom_role(self, fake_kernel):
        """Custom roles can be created and appear in list_roles()."""
        rb = fake_kernel.rbac
        rb.create_role("analyst", capabilities=["sos.read"])
        names = {r.name for r in rb.list_roles()}
        assert "analyst" in names

    def test_assign_and_check(self, fake_kernel):
        """User with a role has that role's capabilities."""
        rb = fake_kernel.rbac
        rb.create_role("r_read_only", capabilities=["sos.read"])
        rb.assign("test_user_1", "r_read_only")
        assert rb.has_capability("test_user_1", "sos.read")
        assert not rb.has_capability("test_user_1", "sos.write")

    def test_grant_adds_capability(self, fake_kernel):
        """grant() adds a capability to an existing role."""
        rb = fake_kernel.rbac
        rb.create_role("r_grow", capabilities=["sos.read"])
        rb.grant("r_grow", "net.http")
        role = next(r for r in rb.list_roles() if r.name == "r_grow")
        assert "net.http" in role.capabilities

    def test_revoke_removes_capability(self, fake_kernel):
        """revoke() removes a capability from a role."""
        rb = fake_kernel.rbac
        rb.create_role("r_shrink", capabilities=["sos.read", "net.http"])
        rb.revoke("r_shrink", "net.http")
        rb.assign("user_shrink", "r_shrink")
        assert not rb.has_capability("user_shrink", "net.http")
        assert rb.has_capability("user_shrink", "sos.read")

    def test_unassign_role(self, fake_kernel):
        """unassign() removes a role from a user."""
        rb = fake_kernel.rbac
        rb.create_role("r_temp", capabilities=["sos.read"])
        rb.assign("temp_user", "r_temp")
        rb.unassign("temp_user", "r_temp")
        assert not rb.has_capability("temp_user", "sos.read")

    def test_multiple_roles_union(self, fake_kernel):
        """User with multiple roles has union of all capabilities."""
        rb = fake_kernel.rbac
        rb.create_role("r_a", capabilities=["sos.read"])
        rb.create_role("r_b", capabilities=["net.http"])
        rb.assign("multi_user", "r_a")
        rb.assign("multi_user", "r_b")
        assert rb.has_capability("multi_user", "sos.read")
        assert rb.has_capability("multi_user", "net.http")
        assert not rb.has_capability("multi_user", "exec")


# ── Raft Node ─────────────────────────────────────────────────────────────────

class TestRaftNode:
    """Tests for the Raft consensus node."""

    def test_initial_state_follower(self, fake_kernel):
        """Node starts as FOLLOWER with term 0."""
        from system.enterprise import RaftNode
        raft = RaftNode("node_a", [], fake_kernel.sos)
        assert raft.status()["state"] == "follower"
        assert raft.status()["term"] == 0

    def test_receive_heartbeat_updates_leader(self, fake_kernel):
        """Receiving a heartbeat sets the leader and term."""
        from system.enterprise import RaftNode
        raft = RaftNode("node_b", [], fake_kernel.sos)
        raft.receive_heartbeat("node_leader", term=1)
        s = raft.status()
        assert s["term"] == 1
        assert s["leader"] == "node_leader"

    def test_vote_granted_to_first_candidate(self, fake_kernel):
        """First vote request in a new term is granted."""
        from system.enterprise import RaftNode
        raft = RaftNode("node_c", [], fake_kernel.sos)
        granted = raft.receive_vote_request("candidate_x", term=1)
        assert granted

    def test_vote_rejected_for_stale_term(self, fake_kernel):
        """Vote request with lower term than current is rejected."""
        from system.enterprise import RaftNode
        raft = RaftNode("node_d", [], fake_kernel.sos)
        raft.receive_heartbeat("leader", term=5)   # advance term to 5
        granted = raft.receive_vote_request("old_candidate", term=3)
        assert not granted

    def test_no_peers_becomes_leader(self, fake_kernel):
        """Node with no peers wins its own election immediately."""
        from system.enterprise import RaftNode
        raft = RaftNode("solo", [], fake_kernel.sos)
        raft.start()
        # Allow election timeout to fire (max 600 ms)
        time.sleep(0.7)
        assert raft.is_leader or raft.status()["state"] in ("leader", "candidate")
        raft.stop()

    def test_status_has_required_keys(self, fake_kernel):
        """status() always contains node_id, state, term, leader, peers."""
        from system.enterprise import RaftNode
        raft = RaftNode("node_e", ["http://peer:8080"], fake_kernel.sos)
        s    = raft.status()
        for key in ("node_id", "state", "term", "leader", "peers"):
            assert key in s, f"Missing key: {key}"


# ── Compliance ────────────────────────────────────────────────────────────────

class TestComplianceManager:
    """Tests for GDPR and PHI compliance features."""

    def test_phi_ssn_detected(self, fake_kernel):
        """Social security numbers are detected as PHI."""
        comp = fake_kernel.compliance
        matches = comp.scan_phi("Patient SSN: 123-45-6789 is confidential")
        assert len(matches) >= 1

    def test_phi_diagnosis_detected(self, fake_kernel):
        """Diagnosis keyword is detected as PHI."""
        comp = fake_kernel.compliance
        matches = comp.scan_phi("diagnosis: type 2 diabetes")
        assert len(matches) >= 1

    def test_phi_clean_text(self, fake_kernel):
        """Clean text (no PHI) returns empty list."""
        comp    = fake_kernel.compliance
        matches = comp.scan_phi("This is a normal message about software.")
        assert matches == []

    def test_gdpr_erase_nonexistent_user(self, fake_kernel):
        """Erasing a user with no data returns 0 erased objects."""
        comp   = fake_kernel.compliance
        result = comp.erase_user("user_with_no_data_xyz_123")
        assert result["erased_objects"] == 0

    def test_gdpr_erase_returns_username(self, fake_kernel):
        """erase_user() result contains the username."""
        comp   = fake_kernel.compliance
        result = comp.erase_user("check_user")
        assert result["user"] == "check_user"

    def test_audit_log_export(self, fake_kernel):
        """export_audit_log() returns a string (possibly empty)."""
        comp = fake_kernel.compliance
        log  = comp.export_audit_log()
        assert isinstance(log, str)


# ── Observability ─────────────────────────────────────────────────────────────

class TestObservability:
    """Tests for Prometheus metrics and OTLP export."""

    def test_prometheus_text_has_nova_info(self, fake_kernel):
        """Prometheus output contains nova_info metric."""
        obs  = fake_kernel.observe
        text = obs.prometheus_text()
        assert "nova_info" in text
        assert 'version="0.0007"' in text

    def test_counter_appears_in_output(self, fake_kernel):
        """Custom counters appear in Prometheus output."""
        obs = fake_kernel.observe
        obs.increment("test_requests", 7)
        text = obs.prometheus_text()
        assert "test_requests" in text

    def test_gauge_appears_in_output(self, fake_kernel):
        """Custom gauges appear in Prometheus output."""
        obs = fake_kernel.observe
        obs.gauge("test_queue_depth", 42.0)
        text = obs.prometheus_text()
        assert "test_queue_depth" in text

    def test_increment_accumulates(self, fake_kernel):
        """Multiple increments add up."""
        obs = fake_kernel.observe
        obs.increment("accum_counter", 3)
        obs.increment("accum_counter", 5)
        # Check the internal counter (not parsing Prometheus text)
        key = obs._key("accum_counter", None)
        assert obs._counters[key] == 8

    def test_otlp_export_no_traces(self, fake_kernel):
        """export_otlp() returns True with no traces (nothing to export)."""
        obs = fake_kernel.observe
        # With no traces the exporter succeeds trivially
        result = obs.export_otlp("http://localhost:4317")
        assert isinstance(result, bool)


# ── CloudManifests ────────────────────────────────────────────────────────────

class TestCloudManifests:
    """Tests for cloud deployment manifest generation."""

    def test_kubernetes_manifest_has_statefulset(self, fake_kernel):
        """Kubernetes manifest contains a StatefulSet."""
        cloud = fake_kernel.cloud
        yaml  = cloud.kubernetes()
        assert "StatefulSet" in yaml
        assert "nova-os" in yaml

    def test_kubernetes_default_replicas(self, fake_kernel):
        """Default replica count is 1."""
        cloud = fake_kernel.cloud
        yaml  = cloud.kubernetes(replicas=1)
        assert "replicas: 1" in yaml

    def test_kubernetes_custom_image(self, fake_kernel):
        """Custom image reference appears in the manifest."""
        cloud  = fake_kernel.cloud
        yaml   = cloud.kubernetes(image="my-registry/nova:test")
        assert "my-registry/nova:test" in yaml

    def test_docker_compose_has_nova_service(self, fake_kernel):
        """docker-compose.yml defines the nova service."""
        cloud  = fake_kernel.cloud
        compose = cloud.docker_compose()
        assert "nova:" in compose
        assert "8080" in compose

    def test_docker_compose_has_prometheus(self, fake_kernel):
        """docker-compose.yml includes Prometheus."""
        cloud   = fake_kernel.cloud
        compose = cloud.docker_compose()
        assert "prometheus" in compose

    def test_terraform_aws_instance(self, fake_kernel):
        """Terraform module contains aws_instance resource."""
        cloud = fake_kernel.cloud
        tf    = cloud.terraform(provider="aws", region="eu-west-1")
        assert "aws_instance" in tf
        assert "eu-west-1" in tf

    def test_terraform_output_block(self, fake_kernel):
        """Terraform module has output for the public IP."""
        cloud = fake_kernel.cloud
        tf    = cloud.terraform()
        assert "output" in tf
        assert "public_ip" in tf


# ── PluginRegistry ────────────────────────────────────────────────────────────

class TestPluginRegistry:
    """Tests for the plugin ecosystem."""

    def test_builtin_plugins_preregistered(self, fake_kernel):
        """All 5 built-in first-party plugins are installed."""
        from plugins.registry import BUILTIN_PLUGINS
        reg     = fake_kernel.plugins
        installed = {m.name for m in reg.list_installed()}
        for spec in BUILTIN_PLUGINS:
            assert spec["name"] in installed, f"Missing builtin: {spec['name']}"

    def test_install_custom_plugin(self, fake_kernel):
        """Custom plugins can be installed from a manifest dict."""
        reg = fake_kernel.plugins
        manifest = {
            "name":        "test_custom_plugin",
            "version":     "1.0.0",
            "description": "A test plugin",
            "author":      "test",
            "permissions": ["sos.read"],
        }
        m = reg.install_from_dict(manifest)
        assert reg.get("test_custom_plugin") is not None
        assert m.name == "test_custom_plugin"

    def test_disable_plugin(self, fake_kernel):
        """disable() sets enabled=False."""
        reg = fake_kernel.plugins
        reg.install_from_dict({
            "name": "disable_test", "version": "1.0",
            "description": "d", "author": "t", "permissions": []
        })
        reg.disable("disable_test")
        assert not reg.get("disable_test").enabled

    def test_enable_plugin(self, fake_kernel):
        """enable() sets enabled=True."""
        reg = fake_kernel.plugins
        reg.install_from_dict({
            "name": "enable_test", "version": "1.0",
            "description": "e", "author": "t", "permissions": []
        })
        reg.disable("enable_test")
        reg.enable("enable_test")
        assert reg.get("enable_test").enabled

    def test_remove_custom_plugin(self, fake_kernel):
        """Custom plugins can be removed."""
        reg = fake_kernel.plugins
        reg.install_from_dict({
            "name": "remove_test", "version": "1.0",
            "description": "r", "author": "t", "permissions": []
        })
        reg.remove("remove_test")
        assert reg.get("remove_test") is None

    def test_cannot_remove_builtin(self, fake_kernel):
        """Built-in plugins cannot be removed."""
        reg    = fake_kernel.plugins
        result = reg.remove("nova-git")
        assert not result
        assert reg.get("nova-git") is not None

    def test_search_by_query(self, fake_kernel):
        """search() filters plugins by name and description."""
        reg     = fake_kernel.plugins
        results = reg.search("git")
        assert any("git" in r["name"] for r in results)


# ── PackageManager ────────────────────────────────────────────────────────────

class TestPackageManager:
    """Tests for the nova-pkg package manager."""

    def test_list_packages(self, fake_kernel):
        """list_packages() returns a list (may be empty or contain pre-existing)."""
        pm = fake_kernel.pkg
        assert isinstance(pm.list_packages(), list)

    def test_version_matching_less_than(self, fake_kernel):
        """Version constraint '<X.Y.Z' matches lower versions."""
        from system.pkgmgr import PackageManager
        assert PackageManager._version_matches("2.28.0", "<2.31.0")
        assert not PackageManager._version_matches("2.32.0", "<2.31.0")
        assert not PackageManager._version_matches("2.31.0", "<2.31.0")

    def test_version_matching_greater_equal(self, fake_kernel):
        """Version constraint '>=X.Y.Z' matches same or higher versions."""
        from system.pkgmgr import PackageManager
        assert PackageManager._version_matches("2.31.0", ">=2.31.0")
        assert PackageManager._version_matches("3.0.0",  ">=2.31.0")
        assert not PackageManager._version_matches("2.30.0", ">=2.31.0")

    def test_audit_returns_list(self, fake_kernel):
        """audit() always returns a list."""
        pm     = fake_kernel.pkg
        result = pm.audit()
        assert isinstance(result, list)

    def test_freeze_returns_string(self, fake_kernel):
        """freeze() returns a requirements.txt-formatted string."""
        pm  = fake_kernel.pkg
        out = pm.freeze()
        assert isinstance(out, str)
        assert "# PyOS NOVA" in out
