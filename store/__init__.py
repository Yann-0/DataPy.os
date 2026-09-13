"""
PyOS NOVA — Store package
==========================
The Semantic Object Store (SOS) — the heart of PyOS NOVA's data layer.

The SOS replaces the conventional filesystem.  Every piece of data is a
content-addressed, immutable, versioned, semantically-linked object.

Modules:
    sos       Core object store backed by SQLite.  Provides POSIX-style
              path aliases, versioning, tagging, FTS5 search, and a
              REST-compatible query interface.
    branches  Git-like branches on the SOS object DAG.  Supports
              create, checkout, merge (three-way), and diff.
    crypto    Per-object AES-256-GCM encryption.  Objects tagged
              ``secret`` are encrypted transparently.
"""
