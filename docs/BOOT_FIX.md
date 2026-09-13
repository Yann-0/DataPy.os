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

This fix removes the reported bootstrap dependency. It does not certify the
current image builder as producing a fully working OS:

1. The Pi Python builder does not bundle `mount`, `blkid`, `reboot`, or `halt`.
   Inherit/checking streams can reveal `FileNotFoundError: mount` next. Bundle
   target-compatible utilities and their dependencies, or deliberately adopt
   and test a Linux-specific bootstrap backend.
2. The configured GNU/Linux Python distribution is described as static, but
   its runtime dependency closure and extracted directory layout are not
   validated or reliably copied. Validate the ELF interpreter, libraries,
   extension modules and standard-library paths in the actual archive.
3. The Pi image's second partition is reserved and marked type `0x83` but is
   not formatted or labelled. Creating a partition entry is not creating a
   persistent filesystem. Never format an existing user's partition as an
   implicit boot-time repair.
4. The fallback FAT writer reuses cluster 3 for every file, does not allocate
   multi-cluster chains, and truncates required long names. Require a real
   FAT implementation and fail image creation if file readback fails.
5. The source ZIP input is not produced automatically from the current
   checkout. Add a source manifest to prevent stale code entering an image.

Acceptance must include an actual target boot, verified required mounts,
interactive input, a DataPlane write, clean shutdown, restart, and readback of
the same data from the identified persistent partition. A successful archive
build or eight passing host tests does not establish those outcomes.

## Installation and CI repairs included

The adjacent verification repairs declare explicit Python package discovery
and the `main` console module, remove the Python 3.11-incompatible f-string,
and make the test workflow propagate compilation failure. They do not change
the remaining failing application tests or remove lint checks.
