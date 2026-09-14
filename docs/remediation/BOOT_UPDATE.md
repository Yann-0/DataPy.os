# Safe boot-artifact update (preserve existing data)

The Pi 5 image has two partitions:

1. `NOVABOOT` — FAT32 boot (firmware, kernel, initramfs, source)
2. `NOVA_DATA` — ext4 labelled `NOVA_DATA` (SOS database and user data)

## Replace boot files without wiping data

1. Do **not** `dd` a full image over a stick that already holds a populated
   `NOVA_DATA` partition.
2. Confirm the device identity yourself. Never guess `/dev/sdX`.
3. Mount the FAT boot partition only.
4. Replace `kernel_2712.img`, `initramfs_nova.gz`, `config.txt`,
   `cmdline.txt`, and firmware files from a newly built image.
5. Leave partition 2 untouched. Verify it still has ext4 magic `0xEF53`
   and label `NOVA_DATA` before writing anything to it.

## First-time image write

Only write the full `artifacts/nova_pi5.img` to a blank or disposable
device after the operator names that device explicitly.

## Rebuild

```bash
python build/build_pi5_nova.py --output artifacts/nova_pi5.img --cache downloads
```

The builder formats ext4 inside a **new regular file**, never a live
block device. Compare `docs/remediation/IMAGE_MANIFEST.json` after each
build.
