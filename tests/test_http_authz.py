"""Loopback HTTP authorization: tokens, denials, no unauthorized writes."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

from net.server import APIServer
from security.capabilities import CapabilityStore
from store.dataplane import DataPlane


def _http(url: str, *, method="GET", token=None, body=None, timeout=5):
    headers = {}
    data = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        payload = json.loads(exc.read().decode())
        return exc.code, payload


def test_http_rejects_unauthenticated_write_without_mutation(sos):
    caps = CapabilityStore(sos)
    plane = DataPlane(sos, caps, enforce=False)
    admin = plane.bootstrap_admin("root")
    srv = APIServer(sos, port=0, host="127.0.0.1", dataplane=plane)
    url = srv.start()
    try:
        status, payload = _http(
            f"{url}/write",
            method="POST",
            body={"handle": "secret", "content": "nope"},
        )
        assert status == 403
        assert sos.resolve("@secret") is None
        status, payload = _http(
            f"{url}/write",
            method="POST",
            token=admin,
            body={"handle": "secret", "content": "ok", "tags": ["keep"]},
        )
        assert status == 201
        assert payload["handle"] == "secret"
        assert sos.read("@secret") == "ok"
        other = caps.grant("@other", {"read", "write"})
        status, _ = _http(
            f"{url}/write",
            method="POST",
            token=other.token,
            body={"handle": "secret", "content": "hijack"},
        )
        assert status == 403
        assert sos.read("@secret") == "ok"
    finally:
        srv.stop()


def test_http_cross_user_sessions_and_oid_read(sos):
    caps = CapabilityStore(sos)
    plane = DataPlane(sos, caps, enforce=False)
    admin = plane.bootstrap_admin("root")
    plane.session(admin, actor="root").put("alpha", "one", tags=["keep"])
    alice = caps.grant("@alpha", {"read"})
    srv = APIServer(sos, port=0, host="127.0.0.1", dataplane=plane)
    url = srv.start()
    try:
        status, health = _http(f"{url}/health")
        assert status == 200
        assert health["status"] == "ok"
        status, _ = _http(f"{url}/aliases/alpha")
        assert status == 403
        oid = sos.resolve("@alpha")
        status, denied = _http(f"{url}/objects/{oid}")
        assert status == 403
        status, got = _http(f"{url}/aliases/alpha", token=alice.token)
        assert status == 200
        assert got["oid"] == oid
        status, listed = _http(f"{url}/tags/keep", token=alice.token)
        assert status == 200
        handles = {row["handle"] for row in listed["results"]}
        assert "alpha" in handles
        huge = {"handle": "big", "content": "x" * (2 * 1024 * 1024)}
        status, payload = _http(
            f"{url}/write", method="POST", token=admin, body=huge
        )
        assert status == 413
    finally:
        srv.stop()


def test_http_simultaneous_sessions(sos):
    caps = CapabilityStore(sos)
    plane = DataPlane(sos, caps, enforce=False)
    admin = plane.bootstrap_admin("root")
    plane.session(admin).put("shared", "v1")
    alice = caps.grant("@alpha", {"write", "read"})
    srv = APIServer(sos, port=0, host="127.0.0.1", dataplane=plane)
    url = srv.start()
    results = {}

    def writer(name, token, handle):
        results[name] = _http(
            f"{url}/write",
            method="POST",
            token=token,
            body={"handle": handle, "content": name},
        )

    try:
        t1 = threading.Thread(target=writer, args=("ok", admin, "shared"))
        t2 = threading.Thread(target=writer, args=("deny", alice.token, "shared"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert results["ok"][0] == 201
        assert results["deny"][0] == 403
        assert sos.read("@shared") == "ok"
    finally:
        srv.stop()
