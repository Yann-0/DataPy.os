"""Platform claim tests: experimental firmware is not a working image."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_uefi_nolinux_builder_is_experimental():
    text = (ROOT / "build" / "usb_nolinux.py").read_text(encoding="utf-8")
    assert "EXPERIMENTAL" in text
    assert "working-image" in text or "working image" in text.lower()
    assert "Build Complete!" not in text
