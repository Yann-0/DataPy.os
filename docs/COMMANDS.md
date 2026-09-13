# PyOS NOVA — Complete Command Reference

> **159 commands** across all subsystems.

> Run `help <command>` in the shell for details.


## SOS Operations

- **`branch`** — Create or manage cmd.
- **`lineage`** — Show data provenance and lineage.

## Search

- **`find`** — Find SOS objects using natural language.
- **`nl`** — Find SOS objects using natural language.

## AI

- **`agents`** — Agents cmd.
- **`model`** — model list|pull <repo> [file]|use <n>|rm <n>|bench [n]|recommended.
- **`finetune`** — finetune start [model]|status|list.

## Developer

- **`debug`** — debug <script> [--break <loc>] — interactive debugger.
- **`breakpoints`** — Manage debugger breakpoints.
- **`make`** — make [task] [--list|--dry-run|--clean|--graph] — build system.
- **`profile`** — profile run <script> | profile list | profile report <id>.
- **`rec`** — rec / rec stop — session recorder.
- **`cast`** — cast list | cast export <id>.
- **`play`** — play <session_id> [--speed N] — replay a recorded session.
- **`lint`** — lint [path] — ruff check.
- **`repl`** — repl [--quiet] — launch the rich Python REPL.
- **`ci`** — ci run [job] [--dry-run]|status|history|log <id>|init.
- **`bench`** — bench [--sos|--search|--ai|--save] — run performance benchmarks.

## Shell

- **`alias`** — Alias cmd.
- **`source`** — source <path> — execute a .nova script from SOS.
- **`streams`** — Reactive stream processor.

## Apps

- **`voice`** — Voice cmd.

## Security

- **`cap`** — Manage capability-based access tokens.
- **`capability`** — Manage capability-based access tokens.
- **`audit`** — Audit cmd.

## Network

- **`http`** — http GET|POST|PUT|DELETE <url> [--json <body>] [--verbose].
- **`sshd`** — sshd [port] | sshd stop | sshd status.
- **`discover`** — discover list|announce.
- **`gateway`** — gateway list|add <path> <upstream>|remove <path>.
- **`dns`** — dns start [port]|stop|record <n> <type> <value>|list.
- **`trust`** — Process reputation and trust scoring.
- **`vpn`** — vpn start [port]|stop|connect <peer> <key>|status.
- **`replicate`** — replicate enable [n]|disable|add-peer <url>|status.
- **`health`** — health — show /health /ready /version status.

## AI Model

- **`model`** — model list|pull <repo> [file]|use <n>|rm <n>|bench [n]|recommended.
- **`finetune`** — finetune start [model]|status|list.

## Storage

- **`compress`** — Adaptive object compression management.
- **`eventsrc`** — eventsrc log [n] | eventsrc rebuild.
- **`schema`** — schema add <path> <schema_name> | schema validate <path> | schema register <n> <json>.
- **`bloom`** — bloom status | bloom rebuild.
- **`view`** — view define <n> <sql> | view refresh <n> | view list | view query <n>.
- **`mvcc`** — mvcc begin | mvcc read <path> | mvcc write <path> <content> | mvcc commit.
- **`pipeline`** — pipeline list | pipeline run <def>.

## Language

- **`lang`** — lang [set <code>|detect <text>|list|auto|install <from> <to>].
- **`translate`** — translate <text> [--to <code>] — translate text to current or specified language.

## Watchdog

- **`watchdog`** — watchdog status|restart <n>|quarantine|resume <n>.

## Plugins

- **`plugin`** — plugin list|search|install|remove|enable|disable|info.
- **`pkg`** — pkg install|remove|list|search|audit|update|freeze.

## Enterprise

- **`tenant`** — tenant create <n>|list|quota <n> <mb>|disable <n>.
- **`rbac`** — rbac role create|list|grant|revoke  assign <user> <role>  check <user> <cap>.
- **`ha`** — ha status|elect|peers.
- **`gdpr`** — gdpr erase <user>|export-audit [since]|scan-phi <path>.
- **`metrics`** — metrics — show Prometheus metrics snapshot.
- **`deploy`** — deploy k8s|compose|terraform — generate deployment manifests.

## System

- **`sandbox`** — Run code in isolated sandbox.
- **`memory`** — Memory cmd.
- **`reload`** — Live kernel hot-reload without reboot.

## Tracing

- **`trace`** — Distributed trace viewer.
- **`traces`** — Distributed trace viewer.

## Chaos

- **`chaos`** — chaos inject <fault> | chaos stop [fault] | chaos status.

## Session

- **`exit`**
- **`shutdown`** — Halt cmd.

## Other (44)

- **`app`** — App cmd.
- **`circuit`** — Circuit breaker management.
- **`classify`** — Classify objects by data sensitivity.
- **`context`** — context status|clear|compress|pin <n>|save [name]|import <name>.
- **`crdt`** — CRDT distributed SOS synchronisation.
- **`cron`** — Cron cmd.
- **`crypto`** — Crypto cmd.
- **`es`** — Event sourcing log and rewind.
- **`events`** — Manage event bus subscriptions.
- **`eventsource`** — Event sourcing log and rewind.
- **`federated`** — Federated model learning.
- **`fix`**
- **`git`** — git mount <url> <path> | git unmount <path> | git mounts.
- **`gpu`** — Gpu cmd.
- **`halt`**
- **`immutable`** — Manage the immutable append-only partition.
- **`kvcache`** — LLM KV attention cache management.
- **`loadtest`** — loadtest <url> [--n N] [--c C] — HTTP load test.
- **`mail`** — mail send <to> <subject> <body>|inbox [user]|read <id>.
- **`mesh`** — mesh add <n> <url>|list|proxy <n> <path>|status.
- **`net`** — Net cmd.
- **`notify`**
- **`on`** — Run a shell command when a pattern matches.
- **`prefetch`** — Manage AI predictive cache prefetch.
- **`privacy`** — Differential privacy for SQL queries.
- **`ratelimit`** — REST API rate limiter.
- **`reboot`** — Reboot cmd.
- **`record`** — rec / rec stop — session recorder.
- **`replay`** — Deterministic replay debugger.
- **`resilience`** — resilience status | resilience test <name>.
- **`scriptcheck`** — scriptcheck <path> — syntax-check a .nova script.
- **`spec`** — Speculative command pre-execution.
- **`sql`** — Sql cmd.
- **`stream`** — Reactive stream processor.
- **`timelock`** — Manage time-locked and expiring objects.
- **`tl`** — Manage time-locked and expiring objects.
- **`tutorial`** — tutorial [N|next] — interactive NOVA tutorial.
- **`type`** — type [path] [--ai] — mypy type-check, or AI annotation suggestions.
- **`vbox`** — Vbox cmd.
- **`waq`** — Write-ahead batch queue management.
- **`wasm`** — WebAssembly runtime.
- **`watch`** — Stream live SOS events matching a pattern.
- **`workers`** — Multiprocessing worker pool management.
- **`zk`** — Zero-knowledge authentication commands.