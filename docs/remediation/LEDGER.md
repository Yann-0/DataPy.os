# DataPy.os remediation ledger (DP-01 … DP-18)

Working language: English. Continuation of
[PR #1](https://github.com/Yann-0/DataPy.os/pull/1)
(`bdea1ad9cb764f815687e9174e6475cbf53e0f3f`, tree
`81865f199bfda72f2f8769c7954309a773e027bf`) on branch
`agent/datapy-remediation-2026-09-14`.

Historical audited `main`: `db99f6d5b7b921db978865fc16845f8a44a9235a`.
PR #1 remained draft and unmerged at handoff.

Status keys: **open**, **implemented**, **verified**, **blocked**.

| ID | Title | Reproduction | Implementing work | Tests | Result | Residual risk | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| DP-01 | Bootstrap streams | `DEVNULL` before `/dev/null` | PR #1 + `boot/linux.py` mount fallback | `tests/test_boot_initramfs.py` | 8 host tests passed (3.12.6) | Not Pi boot | verified (host) |
| DP-02 | CPIO console 5:1 / null 1:3 | Archive decode | PR #1 CPIO writers | `tests/test_boot_initramfs.py` | passed | Host only | verified (host) |
| DP-03 | Utilities / Python closure | Missing mount/loader/stdlib prefix | `boot/linux.py`, musl loader, `python_prefix()` | `tests/test_boot_runtime.py`; initramfs lists 1429 stdlib files + musl loader | host/image assembled | No ARM boot of `mount`/`blkid` | implemented |
| DP-04 | FAT32 allocation / LFN | Every file cluster 3 | `build/fat32.py` | `tests/test_remediation_contract.py`; used in image | passed; image FAT type field `FAT32` | Firmware FAT quirks | verified (host) |
| DP-05 | Persistent filesystem | Type 0x83 with no FS | `build/persist_fs.py` + WSL `mkfs.ext4` | `tests/test_persist_image.py`; image p2 label `NOVA_DATA` | ext4 verified at LBA 526336 | Hardware mount | verified (host image) |
| DP-06 | Install / syntax / CI | 3.11 f-string; package discovery | PR #1 + `pyproject` 0.0.8; compileall; CI timeouts | wheel `datapy_os-0.0.8`; `compileall` 3.12.6 and WSL 3.12.3 | 3.11 interpreter **not installed** | Lint still a separate gate | implemented |
| DP-07 | Unauthorized creation | Write then deny | authorize-before-write | `tests/test_dataplane_authz.py` | passed | Plugin surfaces | verified (host) |
| DP-08 | Bypassing entry points | Raw SOS / HTTP / OID | DataPlane HTTP; kernel sys_* fail closed | `tests/test_http_authz.py` | loopback HTTP passed | File server still SOS when unlocked | verified (host) |
| DP-09 | Revisions vs blob dedup | A→B→A cache cycle | `store/sos.py` + `store/migration.py` | `tests/test_sos_revisions.py` | passed including reopen | Dirty legacy DBs | verified (host) |
| DP-10 | Erasure correctness | `erase_user` path/OID mix | `ComplianceManager.erase_user` | `tests/test_erasure.py` | reports `complete_erasure=False` | Replicas | implemented |
| DP-11 | Listing / search / limits | `limit` vs `n`; metadata leak | find intersects then authz | `tests/test_dataplane_authz.py`, HTTP tags | passed | Pagination edges | verified (host) |
| DP-12 | Capability delegation | Child lives after parent expiry | `chain_valid()` | `tests/test_dataplane_authz.py` | passed | Clock skew | verified (host) |
| DP-13 | NIDS deadlock | `_alert` while holding lock | `threading.RLock` | `tests/test_remediation_contract.py` | 8 threads × 20 inspect, no deadlock | Other lock orders | verified (host) |
| DP-14 | CLI / daemon | `run_command` missing | `NovaShell.execute`; `--cmd` exit codes | `tests/test_cli.py` | put/get across processes | Daemon HTTP not process-tested | implemented |
| DP-15 | Durability / audit | Queue ack vs commit | `WriteAck`; audit flush/tamper | `tests/test_durability.py` | durable commit + tamper detected | Crash injection incomplete | implemented |
| DP-16 | SSH / sandbox isolation | TCP fallback; host `exec()` | refuse plaintext; sandbox reject | `tests/test_isolation.py` | passed | No Linux namespaces on Windows | implemented |
| DP-17 | Raft majority / consensus | `(peers+2)//2`; fake log | `majority = n//2+1`; `append_entries` raises | `tests/test_remediation_contract.py` | even clusters correct | Not a supported multi-node | implemented (fail closed) |
| DP-18 | Platform / readiness claims | Working-image labels | README/ARCHITECTURE; UEFI experimental | `tests/test_platforms.py` | UEFI banner is experimental | Pi hardware | implemented; Pi **blocked** |

## Handoff identity

- Original workspace left on `main` at `db99f6d` with unrelated local
  downloads and an uncommitted musl experiment. That tree was not overwritten.
- Implementation worktree: `C:/Users/yannortodoro/source/datapy.os-remediation`

## Acceptance

Host suite (CPython 3.12.6, `NOVA_NO_AI=1`, isolated `NOVA_DATA`):
**363 passed**, `tests/bench.py` ignored, `--timeout=30`.

Image: `artifacts/nova_pi5.img` SHA256
`622273e1d32fe41c016f2de8e80e7336fa0195df33b2a8fed984c297b7429f34`
(not in git). Partition 2 ext4 label `NOVA_DATA` verified. Pi 5 / QEMU
aarch64 boot **not run** (`qemu-system-aarch64` absent; no authorised
device). The Pi target is **not** marked working.

Python 3.11: not installed on host or WSL. Syntax compiled on 3.12.6
and WSL Ubuntu 3.12.3. CI still declares 3.11.

Packaging: product label remains `0.0008`; wheel/PEP 440 version is
`0.0.8`. Clean-env install of `dist/datapy_os-0.0.8-py3-none-any.whl`
provides `datapy.exe` and imports `boot.linux` when not shadowed by
another checkout on `sys.path`.
