"""
PyOS NOVA — Network Tests  (v0.0008)
======================================
Tests for API Gateway, DNS Server, and Service Discovery.
"""
from __future__ import annotations
import json
import pytest


class TestAPIGateway:
    @pytest.fixture
    def gw(self, sos):
        from net.discovery import APIGateway
        return APIGateway(sos)

    def test_add_and_route(self, gw):
        gw.add_route("/api/v1", "http://backend:8080")
        route = gw.route("/api/v1/objects", "GET")
        assert route is not None
        assert route.upstream == "http://backend:8080"

    def test_longest_prefix_wins(self, gw):
        gw.add_route("/api",      "http://general:8080")
        gw.add_route("/api/v2",   "http://v2:8081")
        route = gw.route("/api/v2/search", "GET")
        assert route.upstream == "http://v2:8081"

    def test_no_match_returns_none(self, gw):
        assert gw.route("/unknown/path", "GET") is None

    def test_method_filter(self, gw):
        gw.add_route("/readonly", "http://srv:8080", methods=["GET"])
        assert gw.route("/readonly/x", "GET") is not None
        assert gw.route("/readonly/x", "POST") is None

    def test_remove_route(self, gw):
        gw.add_route("/temp", "http://tmp:8080")
        assert gw.route("/temp/x", "GET") is not None
        gw.remove_route("/temp")
        assert gw.route("/temp/x", "GET") is None

    def test_openapi_spec_structure(self, gw):
        gw.add_route("/api/v1", "http://backend:8080")
        spec = gw.generate_openapi()
        assert spec["openapi"] == "3.0.0"
        assert "/api/v1" in spec["paths"]

    def test_list_routes(self, gw):
        gw.add_route("/a", "http://a:8080")
        gw.add_route("/b", "http://b:8080")
        routes = gw.list_routes()
        patterns = [r["pattern"] for r in routes]
        assert "/a" in patterns
        assert "/b" in patterns


class TestDNSServer:
    @pytest.fixture
    def dns(self, sos, free_port):
        from net.discovery import DNSServer
        return DNSServer(sos, port=free_port)

    def test_add_and_resolve_a_record(self, dns):
        dns.add_record("nova.local", "A", "192.168.1.100")
        assert dns.resolve_local("nova.local", "A") == "192.168.1.100"

    def test_add_and_resolve_txt_record(self, dns):
        dns.add_record("_nova._tcp.local", "TXT", "version=0.0008")
        assert dns.resolve_local("_nova._tcp.local", "TXT") == "version=0.0008"

    def test_resolve_missing_returns_none(self, dns):
        assert dns.resolve_local("nothere.local", "A") is None

    def test_overwrite_record(self, dns):
        dns.add_record("update.local", "A", "1.2.3.4")
        dns.add_record("update.local", "A", "5.6.7.8")
        assert dns.resolve_local("update.local", "A") == "5.6.7.8"

    def test_list_records(self, dns):
        dns.add_record("r1.local", "A", "10.0.0.1")
        dns.add_record("r2.local", "A", "10.0.0.2")
        records = dns.list_records()
        names = [r["name"] for r in records]
        assert "r1.local" in names
        assert "r2.local" in names

    def test_dns_query_handling(self, dns):
        """DNS query packet for an A record returns a valid response."""
        import struct
        dns.add_record("query.local", "A", "10.10.10.10")
        # Build minimal DNS query packet
        tx_id   = b"\x00\x01"
        flags   = b"\x01\x00"
        counts  = b"\x00\x01\x00\x00\x00\x00\x00\x00"
        qname   = b"\x05query\x05local\x00"
        qtype   = b"\x00\x01"   # A
        qclass  = b"\x00\x01"   # IN
        packet  = tx_id + flags + counts + qname + qtype + qclass
        response = dns._handle_query(packet)
        assert response is not None
        assert response[:2] == tx_id


class TestKernelWatchdog:
    def test_healthy_subsystem(self, fake_kernel):
        from kernel.watchdog import KernelWatchdog
        wd = KernelWatchdog(fake_kernel)
        wd.register("always_ok", health_check=lambda: True)
        wd._check_all()
        st = {s["name"]: s["state"] for s in wd.status()}
        assert st["always_ok"] == "healthy"

    def test_failing_subsystem(self, fake_kernel):
        from kernel.watchdog import KernelWatchdog
        wd = KernelWatchdog(fake_kernel)
        wd.register("always_bad", health_check=lambda: False)
        wd._check_all()
        st = {s["name"]: s["state"] for s in wd.status()}
        assert st["always_bad"] in ("degraded", "quarantined", "restarting")

    def test_restart_called_on_threshold(self, fake_kernel):
        from kernel.watchdog import KernelWatchdog, FAILURE_THRESHOLD
        restarts = []
        wd = KernelWatchdog(fake_kernel)
        wd.register("fragile",
                     health_check=lambda: False,
                     restart_fn=lambda: restarts.append(1))
        for _ in range(FAILURE_THRESHOLD + 1):
            wd._check_all()
        assert len(restarts) >= 1

    def test_quarantine_after_too_many_restarts(self, fake_kernel):
        from kernel.watchdog import KernelWatchdog, QUARANTINE_THRESHOLD
        wd = KernelWatchdog(fake_kernel)
        wd.register("doomed",
                     health_check=lambda: False,
                     restart_fn=lambda: None)
        rec = wd._records["doomed"]
        rec.restart_count = QUARANTINE_THRESHOLD
        rec.consecutive_failures = 5
        wd._attempt_restart(rec)
        assert rec.state.value == "quarantined"

    def test_resume_clears_quarantine(self, fake_kernel):
        from kernel.watchdog import KernelWatchdog, SubsystemState
        wd = KernelWatchdog(fake_kernel)
        wd.register("recoverable", health_check=lambda: False,
                     restart_fn=lambda: None)
        wd._records["recoverable"].state = SubsystemState.QUARANTINED
        wd.resume("recoverable")
        assert wd._records["recoverable"].state != SubsystemState.QUARANTINED

    def test_sos_integrity_check(self, tmp_dir):
        from kernel.watchdog import check_and_repair_sos
        import os
        db = os.path.join(tmp_dir, "test.db")
        # Fresh (non-existent) DB is fine
        ok, msg = check_and_repair_sos(db)
        assert ok


class TestPluginRegistry:
    def test_builtins_registered(self, fake_kernel):
        from plugins.registry import PluginRegistry, BUILTIN_PLUGINS
        reg = PluginRegistry(fake_kernel)
        assert len(reg.list_installed()) == len(BUILTIN_PLUGINS)

    def test_search_finds_builtin(self, fake_kernel):
        from plugins.registry import PluginRegistry
        reg     = PluginRegistry(fake_kernel)
        results = reg.search("git")
        assert any("nova-git" in r["name"] for r in results)

    def test_install_custom(self, fake_kernel):
        from plugins.registry import PluginRegistry
        reg = PluginRegistry(fake_kernel)
        reg.install_from_dict({
            "name": "test-custom", "version": "0.1",
            "description": "test", "author": "test",
            "permissions": ["sos.read"],
        })
        assert reg.get("test-custom") is not None

    def test_disable_enable(self, fake_kernel):
        from plugins.registry import PluginRegistry
        reg = PluginRegistry(fake_kernel)
        reg.install_from_dict({
            "name": "toggled", "version": "1.0",
            "description": "d", "author": "a",
            "permissions": [],
        })
        reg.disable("toggled")
        assert not reg.get("toggled").enabled
        reg.enable("toggled")
        assert reg.get("toggled").enabled

    def test_remove_custom(self, fake_kernel):
        from plugins.registry import PluginRegistry
        reg = PluginRegistry(fake_kernel)
        reg.install_from_dict({
            "name": "removable", "version": "1.0",
            "description": "d", "author": "a",
            "permissions": [],
        })
        reg.remove("removable")
        assert reg.get("removable") is None

    def test_cannot_remove_builtin(self, fake_kernel):
        from plugins.registry import PluginRegistry
        reg = PluginRegistry(fake_kernel)
        ok  = reg.remove("nova-git")
        assert not ok
