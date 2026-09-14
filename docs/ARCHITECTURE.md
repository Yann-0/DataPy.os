# DataPy.os — Architecture Guide

## Overview

DataPy.os is a **data-native OS**: **data + Python + AI**. It is not another
Linux distribution. Product identity and roadmap: `docs/PRODUCT.md`.

The OS is defined by three layers (in priority order):

1. **SOS** — content-addressed blobs, unique revisions, tags, links (the filesystem)
2. **DataPlane** — authorized CRUD over flat handles (primary I/O surface)
3. **NovaKernel** — Python kernel that owns subsystems, shell, and AI

Bring-up (host CPython, Pi Linux kernel + Python PID-1, experimental UEFI)
only starts Python. Optimizations belong in SOS / DataPlane / kernel / AI.

```
NovaKernel (kernel/nova.py)
      ↓
DataPlane (store/dataplane.py)   ← primary surface
      ↓
SOS (store/sos.py)               ← the filesystem
      ↓
Shell / AI / APIs
```

Host development: `python3 main.py`. Pi image: Linux is a thin host so
`boot/pyinit.py` can run as PID-1. Firmware-native UEFI remains experimental.

---

## Core Subsystems

### Semantic Object Store (SOS)

The SOS is DataPy's filesystem, database, and version-control system in one.
Blobs are content-addressed; each write creates a unique revision. Handles
point at revision heads. Collections are tags and graph links — not folders.

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
/net/server.py         REST API via DataPlane (loopback default)
/net/ssh_server.py     SSH server (paramiko required; no TCP fallback)
/net/discovery.py      mDNS service discovery, API gateway, DNS
/net/crdt.py           CRDT-based distributed SOS sync (not a Raft log)
/net/innovations.py    P2P mesh (content-addressed), WebSocket, TOFU, 2PC
/net/http_client.py    HTTP client, health server, distributed replicator
```

---

## Bring-up (not the product)

### Host (primary development)

```
python main.py --no-ai
  → NovaKernel → SOS + DataPlane → shell / --cmd / optional REST
```

Persistent application data is always under `NOVA_DATA` via SOS, never ad-hoc
`open()` for OS objects.

### Pi 5 (hardware bring-up only)

```
1. Firmware loads a Linux kernel + initramfs (transport only)
2. PID 1 is Python (boot/pyinit.py)
3. Mount labelled NOVA_DATA, start NovaKernel
4. SOS / DataPlane / shell — same product as on the host
```

Firmware-native UEFI is experimental and is not a supported runtime.

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
