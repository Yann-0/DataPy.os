# DataPy.os

> **Data is the filesystem. Python is the runtime. AI is the interface.**
>
> Not a classical folder OS. Objects are addressed by content (OID), flat
> handles, tags, and graph links — collections are semantic, not directories.

## Supported targets

| Target | Status |
| --- | --- |
| Host development (`python main.py --no-ai`) | Supported on Python 3.11+ |
| Raspberry Pi 5 ARM64 Linux userspace / PID-1 | Supported design; image builder exists; **hardware boot is not certified** until a real Pi or authorised emulator run succeeds |
| Firmware-native UEFI (no Linux kernel) | **Experimental research only** — not a working image or supported release |

```
Host Python  /  Pi 5 Linux userspace (supported)
    → Python runtime
    → NovaKernel
    → DataPlane (secure CRUD) + SOS (SQLite WAL)
    → Shell / AI / APIs
```

Firmware-native UEFI is a separate, unfinished path. Converting a host ELF
to PE or printing a banner does not implement a native runtime.

## Quick start (host)

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

Pi image (host/WSL/Linux; does **not** flash a device):

```bash
python build/build_pi5_nova.py --output artifacts/nova_pi5.img
```

Do not guess `/dev/sdX`. Do not overwrite an existing data partition.

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
- Lockdown persists across restart; a restart is not unrestricted
- Hash-chained local audit trail on data-plane mutations (not an external anchor)
- Soft delete retains version history; erasure reports incomplete removal

## Version

Product series **0.0008**. Packaging/PEP 440 version is **0.0.8**
(`datapy_os-0.0.8-py3-none-any.whl`). `0.0008` normalizes to `0.8` and
is not used as the wheel version.

Published as [Yann-0/DataPy.os](https://github.com/Yann-0/DataPy.os)

Remediation status: `docs/remediation/LEDGER.md`.
