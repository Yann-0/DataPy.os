# PyOS NOVA — REST API Reference

Base URL: `http://localhost:8080`

All endpoints return JSON. Authentication via capability token in
`Authorization: Bearer <token>` header.

---

## Objects (SOS)

### `GET /objects/<path>`
Read a SOS object.

**Response**
```json
{
  "path":    "/home/root/notes.md",
  "oid":     "a3f2b1c4d5e6...",
  "kind":    "text",
  "size":    1024,
  "content": "# My notes\n...",
  "version": 3,
  "tags":    ["markdown", "note"],
  "created": 1713123456.0,
  "modified": 1713124000.0
}
```

### `PUT /objects/<path>`
Write a SOS object.

**Body**
```json
{
  "content": "file contents",
  "kind":    "text",
  "tags":    ["note"]
}
```

### `DELETE /objects/<path>`
Delete a SOS object (soft delete — version retained).

### `GET /objects/<path>/history`
Get version history for a path.

**Response**
```json
[
  {"version": 3, "oid": "a3f2...", "size": 1024, "modified": 1713124000.0},
  {"version": 2, "oid": "b4c3...", "size": 980,  "modified": 1713123500.0},
  {"version": 1, "oid": "c5d4...", "size": 800,  "modified": 1713123000.0}
]
```

---

## Search

### `GET /search?q=<query>&k=10`
Full-text + vector search.

**Response**
```json
{
  "query":   "machine learning",
  "results": [
    {"path": "/docs/ml.md", "score": 0.94, "snippet": "...machine learning..."},
    {"path": "/notes/ai.md","score": 0.87, "snippet": "...deep learning..."}
  ]
}
```

---

## AI

### `POST /ai/ask`
Query the AI engine.

**Body**
```json
{"prompt": "Summarise the SOS architecture", "max_tokens": 500}
```

**Response**
```json
{"response": "The SOS is a content-addressed...", "tier": "llama", "tokens": 120}
```

### `POST /ai/agent`
Run an autonomous agent task.

**Body**
```json
{"task": "Create a Python web scraper for Hacker News", "dry_run": false}
```

**Response**
```json
{
  "run_id": "a1b2c3d4",
  "status": "running",
  "plan":   "1. Install requests 2. Write scraper..."
}
```

### `GET /ai/agent/<run_id>`
Check agent run status.

---

## System

### `GET /health`
Liveness probe.

```json
{"status": "ok", "version": "0.0008", "uptime_s": 3600}
```

### `GET /ready`
Readiness probe (all subsystems healthy).

```json
{"ready": true, "subsystems": {"sos": "ok", "ai": "ok", "search": "ok"}}
```

### `GET /metrics`
Prometheus metrics (text/plain format).

```
nova_info{version="0.0008"} 1
nova_cpu_percent 12.4
nova_ram_bytes 268435456
nova_sos_objects 15432
```

---

## Capabilities

### `POST /auth/token`
Obtain a capability token.

**Body**
```json
{"username": "alice", "proof": "<ZK proof>"}
```

**Response**
```json
{"token": "nova_cap_...", "expires": 1713210000.0, "permissions": ["sos.read"]}
```

---

## WebSocket Events

Connect to `ws://localhost:8082/events` to receive live SOS events.

**Subscribe message** (sent by client on connect):
```json
{"pattern": "/home/**"}
```

**Event messages** (pushed by server):
```json
{"type": "write", "path": "/home/root/file.txt", "oid": "a1b2...", "ts": 1713124000.0}
```
