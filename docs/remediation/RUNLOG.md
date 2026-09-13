# Remediation run log

Commands are recorded with exact interpreters. Do not treat planned
commands as executed.

## Environment

- Host OS: Windows 10 build 26200
- Host Python: CPython 3.12.6 (`C:\Python312\python.exe`) via worktree `.venv`
- Also installed: Python 3.13 (not used)
- WSL: Ubuntu 24.04, Python 3.12.3, `mkfs.ext4`, `losetup`
- Docker Desktop: stopped (not started)
- QEMU aarch64: not on PATH (host or WSL)
- `gh`: not installed at start of this run
- Python 3.11: **not installed** (host `py -0p` and WSL)

## Source identity (worktree)

```
branch:  agent/datapy-remediation-2026-09-14
tracks:  origin/agent/datapy-boot-review-2026-09-14 (PR #1)
HEAD:    bdea1ad9cb764f815687e9174e6475cbf53e0f3f  (before local commits)
PR #1:   https://github.com/Yann-0/DataPy.os/pull/1
main:    db99f6d5b7b921db978865fc16845f8a44a9235a
```

Original checkout `C:\Users\yannortodoro\source\datapy.os` was left on
`main` with unrelated local downloads; it was not reset or cleaned.

## Isolation

```
NOVA_NO_AI=1
NOVA_DATA=<worktree>\.test-nova-data
TMP/TEMP=<worktree>\.pytest-tmp
```

## Executed

| Command | Result |
| --- | --- |
| `.venv\Scripts\python.exe -m pytest tests/ -q --timeout=30 --ignore=tests/bench.py` | **363 passed** in 22.04s (CPython 3.12.6) |
| `.venv\Scripts\python.exe -m compileall -q -x "(\.venv\|site-packages\|downloads\|\.pytest-tmp)" .` | exit 0 |
| `wsl -d Ubuntu -- python3 -m compileall` on store/boot/kernel/main.py | exit 0 (Python 3.12.3) |
| `.venv\Scripts\python.exe -m pip wheel . -w dist --no-deps` | `datapy_os-0.0.8-py3-none-any.whl` |
| Install wheel in `%TEMP%\datapy-wheel-verify` (outside repo) | `datapy.exe --version` → `PyOS NOVA 0.0008`; `import boot.linux` OK when cwd is not another checkout |
| `python build/build_pi5_nova.py --output artifacts/nova_pi5.img --cache downloads` | exit 0 in 225s; 770 MB image; ext4 `NOVA_DATA` at LBA 526336; FAT32 boot |
| Image SHA256 | `622273e1d32fe41c016f2de8e80e7336fa0195df33b2a8fed984c297b7429f34` |
| `qemu-system-aarch64` | missing |
| Physical Pi flash | not authorised; not attempted |

## Hardware / emulator blocker

To finish Pi acceptance, on a machine with QEMU or an authorised Pi 5:

```
qemu-system-aarch64 ...   # generic ARM is not Pi 5 firmware certification
# or serial console on a named device after explicit confirmation
```

Then: boot → shell → identify `NOVA_DATA` → put/tag/link → authorize one
principal / reject another → update/history → CLI → shutdown → restart
→ same data and lockdown policy.
