# DataPy.os

> **Data is the filesystem. Python is the runtime. AI is the interface.**
>
> Not a classical folder OS. Objects are addressed by content (OID), flat
> handles, tags, and graph links — collections are semantic, not directories.

```
Host / Pi / UEFI
    → Python runtime
    → NovaKernel
    → DataPlane (secure CRUD) + SOS (SQLite WAL)
    → Shell / AI / APIs
```

## Quick start

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
pytest tests/ -v
```

## Model

| Concept | Role |
|---------|------|
| **OID** | SHA-256 content address (immutable blob) |
| **Handle** | Flat label (`note`, `sprint:backlog`) — **no slashes** |
| **Tags** | Collections and filters (`docs`, `kind:code`) |
| **Links** | Typed graph edges between handles |
| **Capability** | Unforgeable token for read/write/delete when lockdown is on |

Primary shell surface: `data put|get|up|rm|find|link|lock|grant`.

Legacy path commands (`ls`, `cd`, …) remain for migration but are not the model.

## Security

- Content-addressed store (tamper-evident identity)
- Capability tokens (`security/capabilities.py`) gated by `data lock on`
- Hash-chained audit trail on data-plane mutations
- Soft delete retains version history

## Version

**0.0008** — published as [Yann-0/DataPy.os](https://github.com/Yann-0/DataPy.os)
