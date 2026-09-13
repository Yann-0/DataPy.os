"""
DataPy.os — Secure Data Plane
=============================
Primary I/O surface for a non-classical OS: objects are addressed by OID,
tags, kinds, and graph links — not by folder trees.

Classical paths remain optional soft labels (flat handles). Collections are
tag sets and relations, never parent directories.

CRUD is capability-gated when enforcement is enabled.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from security.capabilities import CapabilityStore
    from store.sos import SemanticObjectStore

log = logging.getLogger("nova.dataplane")

# Flat handle: letters, digits, colon, underscore, hyphen, dot. No slashes.
_HANDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.-]{0,127}$")

RIGHT_READ = "read"
RIGHT_WRITE = "write"
RIGHT_DELETE = "delete"


@dataclass
class DataRecord:
    """A resolved data object returned by the plane."""

    oid: str
    handle: str
    kind: str
    content: str
    tags: list[str]
    version: int
    size: int
    created_at: float


class DataPlaneError(Exception):
    """Raised for invalid handles, missing objects, or denied access."""


class DataPlane:
    """Secure, folder-free CRUD over the Semantic Object Store.

    Addressing
    ----------
    * **handle** — flat label (e.g. ``readme``, ``note:sprint``), stored as
      SOS alias ``@handle`` so it never implies a directory tree.
    * **oid** — content hash (32 hex chars).
    * **tag** — find/list by semantic tag.

    Security
    --------
    When ``enforce=True`` and a capability store is attached, every mutate
    and read requires a valid token for the target OID (or admin).
    Root / unset token is allowed only when ``enforce=False`` (dev default
    until the operator enables lockdown).
    """

    ALIAS_PREFIX = "@"

    def __init__(
        self,
        sos: SemanticObjectStore,
        caps: CapabilityStore | None = None,
        *,
        enforce: bool = False,
        audit: Any | None = None,
    ) -> None:
        """Wire SOS, optional capabilities, and optional audit trail."""
        self.sos = sos
        self.caps = caps
        self.enforce = enforce
        self.audit = audit
        self._token: str | None = None
        self._actor = "root"

    # ── session ─────────────────────────────────────────────────────────────

    def use_token(self, token: str | None, actor: str = "root") -> None:
        """Set the capability token used for subsequent CRUD calls."""
        self._token = token
        self._actor = actor

    def lockdown(self, enabled: bool = True) -> None:
        """Enable or disable capability enforcement on the data plane."""
        self.enforce = enabled
        log.info("dataplane lockdown=%s", enabled)

    # ── addressing ──────────────────────────────────────────────────────────

    @classmethod
    def validate_handle(cls, handle: str) -> str:
        """Normalize and validate a flat handle (no path separators)."""
        h = (handle or "").strip().lstrip("@")
        if not h or "/" in h or "\\" in h:
            raise DataPlaneError(
                "handles are flat labels — no folders or slashes "
                f"(got {handle!r})"
            )
        if not _HANDLE_RE.match(h):
            raise DataPlaneError(
                "handle must be 1–128 chars: letters, digits, : _ . -"
            )
        return h

    def alias_of(self, handle: str) -> str:
        """Return the SOS alias key for a handle."""
        return f"{self.ALIAS_PREFIX}{self.validate_handle(handle)}"

    def resolve(self, ref: str) -> tuple[str, str]:
        """Resolve a handle or OID to ``(oid, handle_or_oid)``.

        Args:
            ref: Flat handle, ``@handle``, or 32-char hex OID.

        Returns:
            Tuple of OID and the canonical handle (or OID if unnamed).
        """
        ref = (ref or "").strip()
        if not ref:
            raise DataPlaneError("empty reference")

        if ref.startswith("oid:") or (
            len(ref) == 32 and all(c in "0123456789abcdef" for c in ref.lower())
        ):
            oid = ref[4:] if ref.startswith("oid:") else ref.lower()
            obj = self.sos.get(oid)
            if not obj:
                raise DataPlaneError(f"unknown oid: {oid}")
            return oid, oid

        handle = self.validate_handle(ref)
        alias = self.alias_of(handle)
        oid = self.sos.resolve(alias)
        if not oid:
            raise DataPlaneError(f"unknown handle: {handle}")
        return oid, handle

    # ── capability gate ─────────────────────────────────────────────────────

    def _require(self, right: str, oid: str, handle: str) -> None:
        if not self.enforce or self.caps is None:
            return
        if not self._token:
            raise DataPlaneError(f"capability required for {right} on {handle}")
        ok = self.caps.check(self._token, right, path=self.alias_of(handle))
        if not ok:
            # Also accept token bound to OID after first write.
            cap = self.caps.get(self._token)
            if (
                cap
                and cap.is_valid
                and cap.has_right(right)
                and (cap.target_oid == oid or cap.target_path == self.alias_of(handle))
            ):
                return
            raise DataPlaneError(f"denied: {right} on {handle}")

    def _log(self, action: str, handle: str, oid: str) -> None:
        if self.audit is None:
            return
        try:
            self.audit.append(
                action, self._actor, path=handle, detail=f"oid={oid}"
            )
        except Exception as exc:
            log.debug("audit append failed: %s", exc)

    # ── CRUD ────────────────────────────────────────────────────────────────

    def put(
        self,
        handle: str,
        content: str | bytes,
        *,
        kind: str = "text",
        tags: list[str] | None = None,
        meta: dict | None = None,
    ) -> DataRecord:
        """Create or overwrite an object under a flat handle.

        Returns the new record. Grants the caller write/read/delete on first
        put when a capability store is present and no token is set (bootstrap).
        """
        handle = self.validate_handle(handle)
        alias = self.alias_of(handle)
        existing = self.sos.resolve(alias)
        if existing:
            self._require(RIGHT_WRITE, existing, handle)
        elif self.enforce and self.caps is not None and not self._token:
            raise DataPlaneError(f"capability required to create {handle}")

        tag_set = list({*(tags or []), "data", f"kind:{kind}", f"handle:{handle}"})
        oid = self.sos.write(
            alias,
            content,
            kind=kind,
            tags=tag_set,
            meta=meta or {},
        )
        if (
            self.caps is not None
            and existing is None
            and self._token is None
            and not self.enforce
        ):
            # Dev-mode bootstrap: auto-grant CRUD on newly created object.
            cap = self.caps.grant(
                alias,
                {RIGHT_READ, RIGHT_WRITE, RIGHT_DELETE, "grant"},
                owner=self._actor,
            )
            self._token = cap.token

        self._log("data.put", handle, oid)
        return self.get(handle)

    def get(self, ref: str) -> DataRecord:
        """Read an object by handle or OID."""
        oid, handle = self.resolve(ref)
        self._require(RIGHT_READ, oid, handle if handle != oid else ref)
        obj = self.sos.get(oid)
        if obj is None:
            raise DataPlaneError(f"missing object: {ref}")
        content = obj.content
        if isinstance(content, bytes):
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                text = content.hex()
        else:
            text = str(content)
        tags = list(obj.tags) if hasattr(obj, "tags") else []
        self._log("data.get", handle, oid)
        return DataRecord(
            oid=oid,
            handle=handle,
            kind=getattr(obj, "kind", "text") or "text",
            content=text,
            tags=tags,
            version=int(getattr(obj, "version", 1) or 1),
            size=int(getattr(obj, "size", len(text)) or 0),
            created_at=float(getattr(obj, "created_at", time.time()) or time.time()),
        )

    def update(
        self,
        ref: str,
        content: str | bytes,
        *,
        kind: str | None = None,
        tags: list[str] | None = None,
        meta: dict | None = None,
    ) -> DataRecord:
        """Update existing object; fails if the handle does not exist."""
        oid, handle = self.resolve(ref)
        if handle == oid:
            raise DataPlaneError("update by oid requires a handle; use put")
        self._require(RIGHT_WRITE, oid, handle)
        prev = self.get(handle)
        return self.put(
            handle,
            content,
            kind=kind or prev.kind,
            tags=tags if tags is not None else prev.tags,
            meta=meta,
        )

    def delete(self, ref: str) -> str:
        """Soft-delete the alias (versions retained in SOS). Returns OID."""
        oid, handle = self.resolve(ref)
        if handle == oid:
            raise DataPlaneError("delete by oid requires a handle")
        self._require(RIGHT_DELETE, oid, handle)
        self.sos.remove(self.alias_of(handle))
        self._log("data.delete", handle, oid)
        return oid

    def find(
        self,
        *,
        tag: str | None = None,
        kind: str | None = None,
        query: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Find objects by tag, kind, and/or full-text query (no directories)."""
        results: list[dict[str, Any]] = []
        seen: set[str] = set()

        if tag:
            for path in self.sos.find_by_tag(tag):
                if not str(path).startswith(self.ALIAS_PREFIX):
                    continue
                oid = self.sos.resolve(path)
                if not oid or oid in seen:
                    continue
                seen.add(oid)
                results.append(self._card(path, oid))

        if kind:
            for path in self.sos.find_by_tag(f"kind:{kind}"):
                if not str(path).startswith(self.ALIAS_PREFIX):
                    continue
                oid = self.sos.resolve(path)
                if not oid or oid in seen:
                    continue
                seen.add(oid)
                results.append(self._card(path, oid))

        if query:
            for hit in self.sos.keyword_search(query, limit=limit):
                path = hit.get("path") if isinstance(hit, dict) else None
                if not path:
                    # Some SOS versions return (path, snippet) tuples.
                    path = hit[0] if isinstance(hit, (list, tuple)) else None
                if not path or not str(path).startswith(self.ALIAS_PREFIX):
                    continue
                oid = self.sos.resolve(path)
                if not oid or oid in seen:
                    continue
                seen.add(oid)
                results.append(self._card(path, oid))

        if not tag and not kind and not query:
            # List all data handles.
            try:
                conn = self.sos._pool.get()
                rows = conn.execute(
                    "SELECT path, oid FROM aliases WHERE path LIKE '@%' "
                    "AND is_dir = 0 LIMIT ?",
                    (limit,),
                ).fetchall()
                for row in rows:
                    path, oid = row[0], row[1]
                    if oid in seen:
                        continue
                    seen.add(oid)
                    results.append(self._card(path, oid))
            except Exception as exc:
                log.warning("dataplane list failed: %s", exc)

        return results[:limit]

    def link(self, src: str, dst: str, relation: str = "ref") -> None:
        """Create a graph edge between two flat handles."""
        src_h = self.validate_handle(src)
        dst_h = self.validate_handle(dst)
        src_oid, _ = self.resolve(src_h)
        self._require(RIGHT_WRITE, src_oid, src_h)
        self.sos.relate(self.alias_of(src_h), self.alias_of(dst_h), relation)
        self._log("data.link", f"{src_h}->{dst_h}", src_oid)

    def related(self, ref: str, relation: str | None = None) -> list[tuple]:
        """Return graph neighbours of a handle."""
        handle = self.validate_handle(ref)
        oid, _ = self.resolve(handle)
        self._require(RIGHT_READ, oid, handle)
        return self.sos.related(self.alias_of(handle), relation=relation)

    def _card(self, path: str, oid: str) -> dict[str, Any]:
        handle = str(path)[len(self.ALIAS_PREFIX) :]
        obj = self.sos.get(oid)
        tags = list(getattr(obj, "tags", []) or []) if obj else []
        return {
            "handle": handle,
            "oid": oid,
            "kind": getattr(obj, "kind", "text") if obj else "text",
            "tags": tags,
            "version": int(getattr(obj, "version", 1) or 1) if obj else 1,
            "size": int(getattr(obj, "size", 0) or 0) if obj else 0,
        }

    def seed_core(self) -> None:
        """Seed starter data objects (flat handles + tags), not directories."""
        samples = [
            (
                "welcome",
                (
                    "DataPy.os — data is the filesystem.\n"
                    "Use: data put|get|up|rm|find|link|lock\n"
                    "Handles are flat labels. Collections are tags + links.\n"
                ),
                "text",
                ["docs", "system"],
            ),
            (
                "hello",
                'print("Hello from DataPy.os")\n',
                "code",
                ["python", "example"],
            ),
            (
                "os:manifest",
                (
                    'NAME="DataPy.os"\nVERSION="0.0008"\n'
                    "MODEL=data+python+ai\nNO_CLASSICAL_FOLDERS=1\n"
                ),
                "text",
                ["system", "config"],
            ),
        ]
        for handle, content, kind, tags in samples:
            alias = self.alias_of(handle)
            if self.sos.resolve(alias):
                continue
            tag_set = list({*tags, "data", f"kind:{kind}", f"handle:{handle}"})
            # Direct SOS write avoids recursive patch side-effects during boot.
            self.sos.write(alias, content, kind=kind, tags=tag_set)
