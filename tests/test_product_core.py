"""Product-path regressions: flush barrier, SOS RAG, lean AI tier."""

from __future__ import annotations

import os

from ai.engine import AIEngine, RAGEngine
from security.capabilities import CapabilityStore
from store.dataplane import DataPlane


def test_sos_flush_barrier_and_dataplane_ack(sos):
    oid = sos.write("/p/a", "payload-a")
    ack = sos.flush(barrier=True)
    assert ack is not None
    assert ack.durable is True
    assert ack.oid == oid
    caps = CapabilityStore(sos)
    plane = DataPlane(sos, caps, enforce=False)
    plane.put("note", "hello data os", tags=["ideas"])
    assert plane.last_ack is not None
    assert plane.last_ack.durable is True


def test_rag_answers_from_sos_handles(sos):
    caps = CapabilityStore(sos)
    plane = DataPlane(sos, caps, enforce=False)
    plane.put("sprint", "ship the data-native kernel this week", tags=["plan"])
    rag = RAGEngine(sos=sos, dataplane=plane)
    ans = "".join(rag.complete("sprint kernel"))
    assert "From DataPy SOS" in ans
    assert "@sprint" in ans


def test_nova_no_ai_forces_rag_tier(monkeypatch):
    monkeypatch.setenv("NOVA_NO_AI", "1")
    engine = AIEngine()
    assert engine.tier == "rag"
    assert "RAG" in engine.model_name
