"""Durability acknowledgements, audit flush, and tamper detection."""

from __future__ import annotations

from security.ledger import AuditTrail, CHAIN_PATH
from store.sos import WriteAck


def test_write_ack_is_durable_commit(sos):
    oid = sos.write("/d/a", "payload")
    ack = sos.last_ack
    assert isinstance(ack, WriteAck)
    assert ack.oid == oid
    assert ack.durable is True
    assert ack.queued is False
    assert ack.noop is False


def test_audit_flush_and_tamper_detected(sos):
    audit = AuditTrail(sos)
    audit.append("write", "root", path="@note", detail="oid=abc")
    audit.flush()
    ok, msg = audit.verify()
    assert ok, msg
    raw = sos.read(CHAIN_PATH)
    tampered = (raw.decode() if isinstance(raw, bytes) else raw).replace(
        "root", "attacker", 1
    )
    sos.write(CHAIN_PATH, tampered, tags=["audit-chain"])
    ok, msg = audit.verify()
    assert ok is False
    assert "hash" in msg.lower() or "chain" in msg.lower() or "mismatch" in msg.lower()
