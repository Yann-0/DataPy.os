# DataPy.os

> **Data is the filesystem. Python is the runtime. AI is the interface.**

DataPy.os is **not** another Linux distribution. It is a data-native operating
system: objects live in a content-addressed store; Python is the kernel;
collections are tags and links, not folders.

```
NovaKernel (Python)
    → DataPlane   secure CRUD — primary surface
    → SOS         content-addressed SQLite WAL (the filesystem)
    → Shell / AI / APIs
```

Host CPython, a thin Pi bring-up kernel, or experimental firmware are only
**how Python starts**. They are not the product. See `docs/PRODUCT.md`.

## Model (the OS)

| Concept | Role |
|---------|------|
| **OID** | Immutable blob identity (content hash) |
| **Revision** | Unique history step for a handle (predecessor chain) |
| **Handle** | Flat label (`note`, `sprint:backlog`) — **no slashes** |
| **Tags** | Semantic collections (`docs`, `kind:code`) |
| **Links** | Typed graph edges between handles |
| **Capability** | Unforgeable token for read/write/delete when lockdown is on |

Primary shell surface: `data put|get|up|rm|find|link|lock|grant`.

Legacy path commands (`ls`, `cd`, …) exist for migration; they are not the model.

## Quick start (host — primary development surface)

```bash
pip install -e ".[dev]"
python main.py --no-ai
# then:
#   data put note "hello world" --tag ideas
#   data get note
#   data find tag=ideas
#   data lock on
```

```bash
python main.py --cmd "data find"
pytest tests/ --ignore=tests/bench.py --timeout=30
```

Set `NOVA_DATA` to an isolated directory in tests. Never point tests at `~/.nova`.

## Security

- Content-addressed store (tamper-evident blob identity)
- Capability tokens gated by `data lock on`; lockdown persists across restart
- Hash-chained local audit trail on DataPlane mutations (not an external anchor)
- Soft delete retains revision history; erasure reports incomplete removal honestly

## Bring-up (not the product)

| Surface | Role |
| --- | --- |
| Host `python main.py` | Primary way to run and develop the OS |
| Pi 5 image builder | Hardware bring-up so Python can be PID-1; **boot not certified** |
| Firmware-native UEFI | Experimental research — not a working image |

```bash
python build/build_pi5_nova.py --output artifacts/nova_pi5.img
```

Do not guess `/dev/sdX`. Do not overwrite an existing data partition.
Optimizations belong in SOS, DataPlane, kernel, and AI — not in growing a
Linux userspace.

## Version

Product series **0.0008**. Packaging/PEP 440 version is **0.0.8**.

Published as [Yann-0/DataPy.os](https://github.com/Yann-0/DataPy.os)

- Product identity & roadmap: `docs/PRODUCT.md`
- Architecture: `docs/ARCHITECTURE.md`
- Remediation ledger: `docs/remediation/LEDGER.md`
