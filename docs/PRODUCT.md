# DataPy.os — product identity and roadmap

## What this is

DataPy.os is an **innovative data-native OS**:

1. **Storage is the centre** — SOS (content-addressed blobs, revisions, handles,
   tags, links) *is* the filesystem.
2. **Python is the kernel** — NovaKernel owns process/scheduling, shell, AI,
   and policy; application code is Python.
3. **DataPlane is the primary surface** — authorized CRUD; classical folder
   trees are legacy, not the design.

It is **not** a Linux distro with a Python shell bolted on. A Linux kernel or
host CPython may exist underneath so Python can start and talk to hardware.
That layer is **bring-up**, not the product. All product optimizations target
data, Python, and AI — not packaging more Unix utilities.

## Non-goals

- Replacing Debian/Raspberry Pi OS as a general-purpose Linux distribution
- Competing on apt packages, desktop environments, or POSIX folder UX
- Claiming firmware-native UEFI is done because an ELF was converted to PE

## Product stack (order of importance)

```
1. SOS            store/sos.py         blobs, revisions, tags, FTS, history
2. DataPlane      store/dataplane.py   handles, capabilities, find/link
3. NovaKernel     kernel/nova.py       composition, boot/shutdown, syscalls
4. Shell / CLI    shell/nova_shell.py  data * commands, --cmd
5. AI             ai/engine.py         ask over SOS (RAG / local model)
6. Bring-up only  boot/, build/        host, Pi image, experimental UEFI
```

## Roadmap priorities (product first)

### P0 — data OS core

- Revision identity, durable write acks, migration of legacy DBs
- Capability lockdown that fails closed and survives restart
- Shell/CLI/API that never bypasses DataPlane under lockdown
- Search/find/link consistency with authorization-aware limits
- Honest durability and audit lifecycle (commit vs queue)

### P1 — Python kernel experience

- Fast host boot (`--no-ai`, lean subsystem init)
- Interactive shell centred on `data *`, not classical `ls`/`cd`
- AI as interface to SOS (RAG always; local models optional)
- Plugins that cannot escape the capability boundary

### P2 — bring-up (hardware only)

- Pi 5 image that reaches a usable DataPy shell and persistent SOS
- Preserve `NOVA_DATA` across boot-artifact updates
- Emulator/hardware acceptance without claiming “Linux OS”

### Explicitly deferred / experimental

- Firmware-native UEFI without Linux
- Multi-node Raft as durable replicated log (election ≠ committed data)
- Expanding Linux userspace for its own sake

## How to decide a change

Ask: does this make **data storage, DataPlane, or the Python kernel** better?

- Yes → product work.
- Only helps boot Linux / flash media → keep it in `boot/` / `build/`, document
  as bring-up, do not market it as the OS.

## Related docs

- `README.md` — entry point
- `docs/ARCHITECTURE.md` — subsystem map
- `docs/API.md` / `docs/COMMANDS.md` — surfaces
- `docs/remediation/LEDGER.md` — DP-01…DP-18 status
