# Early boot device fix

The Pi screenshot reports `FileNotFoundError: /dev/null` in the generated
`mount()` helper. `subprocess.DEVNULL` opens that device in the parent Python
process before executing the utility. The initial archive did not contain a
usable null device, and `/dev` had not yet been mounted.

This change makes the Pi and USB boot helpers inherit console streams and
check the mount command's exit status. Both Python CPIO writers now support
character-device major/minor fields and include these bootstrap nodes:

| Path | Type | Major | Minor | Permissions |
| --- | --- | --- | --- | --- |
| `/dev/console` | character | 5 | 1 | `0600` |
| `/dev/null` | character | 1 | 3 | `0666` |

The previous console entry used character-device mode with device number
`0:0`. Merely setting mode bits does not create the correct device.

## Verification

Run from the repository root:

```bash
python -m pytest tests/test_boot_initramfs.py -q
```

The eight regressions exercise real Python subprocess stream setup while
`os.devnull` points to an absent temporary path, propagate an actual failing
child exit, decode serialized newc headers, and inspect a generated compressed
Pi archive using a tiny runtime fixture. They do not mount filesystems or
modify host device nodes. All eight fail on the original source and pass with
the fix. These are host regressions, not physical Pi boot acceptance.

## Applying the fix to boot media

Editing the repository does not change an existing USB image. The generated
`/nova/boot/pyinit_rpi5.py` lives inside `initramfs_nova.gz`; the updated builder
must regenerate that archive and the image. In the current image pipeline,
the explicit NOVA source ZIP must also contain the intended current source.
Do not overwrite a populated data partition while replacing boot artifacts.

The exact boot media used for the screenshot was not available for inspection.
The traceback matches `PYINIT_RPI5` in `build/build_pi5_nova.py`, but the image
cannot be attributed to a specific commit from the screenshot alone.

## Remaining image blockers

PR #1 plus this continuation implement host regressions for streams, CPIO
nodes, FAT allocation, labelled ext4 in a new image file, musl Python prefix
normalization, and current-checkout source packaging. They do **not** certify
a Pi 5 boot.

Still required before marking the Pi target working:

1. Target-compatible `mount`/`blkid`/loader closure on a real ARM64 boot.
2. Verified kernel modules/firmware for storage, console, and network.
3. Actual emulator or authorised hardware boot, interactive shell, write,
   clean shutdown, restart, and readback from the labelled `NOVA_DATA`
   partition.
4. Do not flash without an explicit device. Do not guess `/dev/sdX`.

A successful archive build or passing host tests does not establish those
outcomes.

## Installation and CI repairs included

The adjacent verification repairs declare explicit Python package discovery
and the `main` console module, remove the Python 3.11-incompatible f-string,
and make the test workflow propagate compilation failure. They do not change
the remaining failing application tests or remove lint checks.
