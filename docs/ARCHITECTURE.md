# PyOS NOVA — Architecture Guide

## Overview

PyOS NOVA is a research operating system where Python replaces `/sbin/init`.
On a real machine, `boot/pyinit.py` is the first userspace process (PID 1).
On a developer workstation, `python3 main.py` starts the full kernel in-process.

```
Hardware / UEFI
      ↓
boot/efi_builder.py    PE32+ EFI binary (x86-64 / ARM64 / RISC-V)
      ↓
boot/pyinit.py         PID 1 — mounts filesystems, starts kernel
      ↓
kernel/nova.py         NovaKernel — owns all subsystems
      ↓
shell/nova_shell.py    Interactive shell (120+ commands)
```

---

## Core Subsystems

### Semantic Object Store (SOS)

The SOS is NOVA's filesystem, database, and version-control system in one.
Every object is content-addressed by SHA-256 of its content, stored in
SQLite WAL mode, and automatically versioned.

```
/store/sos.py              Core store: write, read, resolve, search
/store/waq.py              Write-ahead queue (batch writes, ×41 throughput)
/store/events.py           Reactive pub/sub event bus
/store/compression.py      Adaptive compression (zstd/lz4/brotli/gzip)
/store/pipeline.py         Stream processor, Bloom filter, schema validation
/store/innovations.py      Block dedup, tiered storage, graph queries, time-series
/store/prefetch.py         Markov-chain predictive prefetch
```

**Object lifecycle:**
1. `sos.write(path, content)` — content hashed → OID stored → alias created → event emitted
2. `sos.read(path)` — alias lookup → LRU cache check → SQLite fetch
3. `sos.resolve(path)` → OID string (Bloom filter eliminates non-existent paths)

### AI Engine

Tiered inference: the best available tier is selected automatically at boot.

```
Tier 1: llama-cpp-python    Full GGUF model inference (GPU or CPU)
Tier 2: NanoLLM             Tiny embedding-based response generation
Tier 3: RAG                 Retrieval-augmented responses from SOS index
```

```
/ai/engine.py          Tiered dispatcher
/ai/agents.py          Multi-agent dispatch with tool registry
/ai/autonomous.py      ReAct autonomous task runner
/ai/models.py          Model manager: download, bench, hot-swap
/ai/memory.py          Conversation history + context management
/ai/innovations.py     SemanticDiff, LiveRAG, SelfOptimiser
/ai/federated.py       Federated fine-tuning with DP noise
```

### Kernel Scheduler

Every NOVA daemon runs as an asyncio coroutine, not a thread. The
`AsyncScheduler` owns a dedicated event loop in a background thread.
`submit(coro)` is thread-safe and returns a `NovaTask`.

```
/kernel/scheduler.py   AsyncScheduler, Priority, Channel
/kernel/watchdog.py    KernelWatchdog: health monitor + auto-restart
/kernel/namespaces.py  Plan 9-style bind mounts (per-process SOS views)
/kernel/checkpoint.py  Process snapshot → SOS → restore on next boot
/kernel/hotreload.py   Live module replacement without restart
```

### Security

Security is capability-based: every operation requires an unforgeable
capability token. The ZK auth system proves identity without transmitting
secrets. The Merkle-chained ledger provides tamper-evident audit trails.

```
/security/capabilities.py   Capability tokens (unforgeable, revocable)
/security/zkauth.py          Zero-knowledge Pedersen commitment auth
/security/ledger.py          Merkle-chained tamper-evident audit trail
/security/innovations.py     SecretVault, NIDS, MeasuredBoot
/system/enterprise.py        RBAC, multi-tenancy, GDPR compliance
```

### Networking

```
/net/server.py         REST API + SOS file server (HTTP)
/net/ssh_server.py     SSH server (paramiko + TCP fallback)
/net/discovery.py      mDNS service discovery, API gateway, DNS
/net/crdt.py           CRDT-based distributed SOS sync
/net/innovations.py    P2P mesh (content-addressed), WebSocket, TOFU, 2PC
/net/http_client.py    HTTP client, health server, distributed replicator
```

---

## Boot Sequence

```
1. UEFI firmware loads boot/BOOTX64.EFI (PE32+ Python stub)
2. EFI stub launches python3 boot/pyinit.py as PID 1
3. pyinit mounts the SOS database at /nova/data/
4. pyinit checks SOS integrity (WAL recovery if corrupt)
5. pyinit imports kernel/nova.py and calls NovaKernel.boot()
6. NovaKernel initialises subsystems in dependency order:
     SOS → EventBus → WAQ → AI → Scheduler → Watchdog → i18n → ...
7. NovaKernel starts the shell, REST API, SSH, metrics server
8. Control returns to pyinit which enters a wait loop
   (restarting the kernel on crash with exponential back-off)
```

---

## Data Flow: SOS Write

```
shell> write /docs/note.md "Hello"
           ↓
nova_shell._write_cmd()
           ↓
kernel.sos.write(path, content)
           ↓
 ┌─── Bloom filter add(path)
 ├─── WAQ.put(path, content)  [batched write]
 ├─── WAQ flush → SQLite WAL write
 │        objects: (oid, kind, size, content, ...)
 │        aliases: (path, oid, version, ...)
 ├─── LRU cache.set(oid, obj)
 ├─── FTS5 index update
 ├─── EventBus.emit(SOSEvent("write", path, oid))
 │        → prefetch model update
 │        → stream pipeline operators
 │        → WebSocket push to browser clients
 └─── return oid
```

---

## Plugin System

Plugins are Python modules with a `nova.toml` manifest.
The `PluginSandbox` wraps every operation to enforce the declared
permission set (`sos.read`, `sos.write`, `exec`, `net.http`, etc.).

```
/plugins/registry.py   Discovery, installation, sandboxed loading
/plugins/manager.py    Runtime lifecycle management
```

Plugin manifest example:
```toml
[plugin]
name        = "nova-git"
version     = "1.0.0"
permissions = ["sos.read", "sos.write", "fs.read", "exec"]
entry_point = "main.plugin_main"
```

---

## Distributed Architecture

Multiple NOVA nodes form a cluster automatically:

```
Node A                          Node B
  │                               │
  ├── ServiceDiscovery ──UDP──→  ServiceDiscovery
  │   (mDNS broadcast)           (records peer)
  │                               │
  ├── CRDT SOS sync ──HTTP──→   CRDT SOS merge
  │   (eventual consistency)     │
  │                               │
  ├── RaftNode ──HTTP──→         RaftNode
  │   (leader election)          (follow / vote)
  │                               │
  └── P2PMesh ──UDP──→           P2PMesh
      (OID-addressed routing)    (serves cached OIDs)
```

---

## Performance

Key measurements on a 2023 laptop (no GPU, 16 GB RAM):

| Operation | Throughput / Latency |
|---|---|
| SOS write | 800+ writes/sec |
| SOS resolve (cached) | < 0.5 ms |
| WAQ batch (100 writes) | 4.2 ms |
| Vector search (10k vecs) | 2.8 ms |
| Bloom filter check | < 0.1 ms |
| Language detection | < 2 ms |
| Translation (builtin) | < 0.1 ms |
| AI RAG response | 50–200 ms |
| AI llama.cpp (7B Q4) | 8–15 tokens/sec |

---

## File Count Summary

```
120 Python files
~42 000 lines of code
7 git commits
100% module docstring coverage
```
