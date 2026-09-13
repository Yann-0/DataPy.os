#!/usr/bin/env bash
# PyOS NOVA — Docker USB Image Builder
# ======================================
# Runs inside the nova-builder container with all tools available.
# Mounts the NOVA source as /workspace, outputs nova.img there.
#
# Tools available inside container (from Dockerfile.build):
#   grub-mkimage  — real GRUB2 EFI binary
#   mtools        — mcopy/mmd for FAT32 manipulation
#   mkfs.fat      — FAT32 formatting
#   linux kernel  — /boot/vmlinuz from debian:12

set -euo pipefail

GRN='\033[32m'; CYN='\033[36m'; YLW='\033[33m'; RED='\033[31m'; RST='\033[0m'
log()  { echo -e "${CYN}[NOVA-BUILD]${RST} $*"; }
ok()   { echo -e "${GRN}[  OK  ]${RST} $*"; }
warn() { echo -e "${YLW}[ WARN ]${RST} $*"; }
die()  { echo -e "${RED}[ FAIL ]${RST} $*"; exit 1; }

NOVA_DIR="${NOVA_DIR:-/workspace}"
OUTPUT="${NOVA_OUTPUT:-nova.img}"
SIZE="${NOVA_SIZE:-512M}"
WORK="/tmp/nova_build_$$"
mkdir -p "$WORK"
trap "rm -rf $WORK" EXIT

# ── Sizes ─────────────────────────────────────────────────────────────────────
# Parse size: 512M → 536870912, 2G → 2147483648
parse_size() {
    local s="${1^^}"
    if [[ $s == *G ]]; then echo $(( ${s%G} * 1024 * 1024 * 1024 ))
    elif [[ $s == *M ]]; then echo $(( ${s%M} * 1024 * 1024 ))
    else echo "$s"; fi
}

TOTAL_BYTES=$(parse_size "$SIZE")
TOTAL_MB=$(( TOTAL_BYTES / 1024 / 1024 ))
EFI_MB=64
DATA_MB=$(( TOTAL_MB - EFI_MB - 2 ))

log "PyOS NOVA USB Image Builder (Docker edition)"
log "Output: ${NOVA_DIR}/${OUTPUT}  Size: ${TOTAL_MB} MB"

# ──────────────────────────────────────────────────────────────── 1. Initramfs

log "Building initramfs..."
INITRD="${WORK}/nova_initrd.cpio.gz"
STAGING="${WORK}/initrd_staging"
mkdir -p "$STAGING"/{sbin,bin,usr/bin,usr/lib,dev,proc,sys,tmp,run,mnt,etc,lib64,nova}

# Copy Python
PY=$(command -v python3)
PY_VER=$($PY -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
cp "$PY" "$STAGING/usr/bin/python3"
chmod +x "$STAGING/usr/bin/python3"
ln -sf /usr/bin/python3 "$STAGING/bin/python3"
ln -sf /usr/bin/python3 "$STAGING/bin/python"

# Python stdlib
PY_STDLIB=$($PY -c 'import sysconfig; print(sysconfig.get_path("stdlib"))')
if [[ -d "$PY_STDLIB" ]]; then
    log "Copying Python stdlib from $PY_STDLIB..."
    # Copy all .py files (skip __pycache__, test dirs)
    find "$PY_STDLIB" -name "*.py" \
        ! -path "*/test/*" ! -path "*/tests/*" ! -path "*/__pycache__/*" \
        | while read -r f; do
            REL="${f#$PY_STDLIB/}"
            DST="$STAGING/usr/lib/python${PY_VER}/${REL}"
            mkdir -p "$(dirname "$DST")"
            cp "$f" "$DST"
        done
fi

# Copy shared libraries Python needs
for lib in $(ldd "$PY" 2>/dev/null | grep -o '/[^ ]*\.so[^ ]*'); do
    [[ -f "$lib" ]] || continue
    mkdir -p "$STAGING$(dirname $lib)"
    cp "$lib" "$STAGING$lib" 2>/dev/null || true
done

# Copy PyOS NOVA source
log "Embedding NOVA source..."
cd "$NOVA_DIR"
find . -name "*.py" \
    ! -path "./__pycache__/*" ! -path "./.git/*" ! -path "./tests/*" \
    | while read -r f; do
    DST="$STAGING/nova/${f#./}"
    mkdir -p "$(dirname "$DST")"
    cp "$f" "$DST"
done

# /sbin/init
cat > "$STAGING/sbin/init" << 'INIT'
#!/usr/bin/env python3
"""PyOS NOVA PID 1"""
import os, sys
sys.path.insert(0, "/nova")
sys.path.insert(0, f"/usr/lib/python{sys.version_info.major}.{sys.version_info.minor}")
exec(open("/nova/boot/pyinit_usb.py").read())
INIT
chmod +x "$STAGING/sbin/init"
ln -sf /sbin/init "$STAGING/init"

# /nova/boot/pyinit_usb.py (enhanced boot init)
python3 "$NOVA_DIR/build/usb_image.py" --emit-pyinit > "$STAGING/nova/boot/pyinit_usb.py" 2>/dev/null || \
    cp "$NOVA_DIR/boot/pyinit.py" "$STAGING/nova/boot/pyinit_usb.py"

# System config files
echo "nova"          > "$STAGING/etc/hostname"
echo "127.0.0.1 localhost nova" > "$STAGING/etc/hosts"
cat > "$STAGING/etc/os-release" << 'EOF'
NAME="PyOS NOVA"
VERSION="0.0008"
ID=nova
PRETTY_NAME="PyOS NOVA 0.0008"
EOF

# /dev/console (required by kernel before devtmpfs is mounted)
mknod -m 600 "$STAGING/dev/console" c 5 1 2>/dev/null || \
    touch "$STAGING/dev/console"

# Build cpio archive
log "Packing initramfs..."
(cd "$STAGING" && find . | cpio -o -H newc 2>/dev/null | gzip -9 > "$INITRD")
INITRD_MB=$(( $(stat -c%s "$INITRD") / 1024 / 1024 ))
ok "Initramfs: ${INITRD_MB} MB"

# ──────────────────────────────────────────────────────────────── 2. Kernel

log "Locating Linux kernel..."
KERNEL=""
for k in /boot/vmlinuz-* /boot/vmlinuz; do
    [[ -f "$k" ]] && KERNEL="$k" && break
done
[[ -z "$KERNEL" ]] && die "No kernel found in /boot. Install linux-image-amd64."
KERNEL_MB=$(( $(stat -c%s "$KERNEL") / 1024 / 1024 ))
ok "Kernel: $KERNEL (${KERNEL_MB} MB)"

# ──────────────────────────────────────────────────────────────── 3. GRUB EFI

log "Building GRUB2 EFI binary..."
GRUB_MODS_DIR=""
for d in /usr/lib/grub/x86_64-efi /usr/lib/grub2/x86_64-efi; do
    [[ -d "$d" ]] && GRUB_MODS_DIR="$d" && break
done
[[ -z "$GRUB_MODS_DIR" ]] && die "GRUB2 EFI modules not found"

EFI_OUT="${WORK}/BOOTX64.EFI"
GRUB_EARLY="${WORK}/early.cfg"
cat > "$GRUB_EARLY" << 'GCFG'
set prefix=(memdisk)/boot/grub
set root=(memdisk)
source $prefix/grub.cfg
GCFG

grub-mkimage \
    -d "$GRUB_MODS_DIR" \
    -O x86_64-efi \
    -o "$EFI_OUT" \
    -p "(memdisk)/boot/grub" \
    --memdisk "$GRUB_EARLY" \
    fat part_gpt part_msdos normal boot linux configfile \
    search_fs_uuid search loadenv echo test true \
    all_video video_fb gfxterm font gfxmenu terminal minicmd \
    2>/dev/null

EFI_KB=$(( $(stat -c%s "$EFI_OUT") / 1024 ))
ok "GRUB2 EFI: ${EFI_KB} KB"

# ──────────────────────────────────────────────────────────────── 4. Disk image

log "Creating disk image (${TOTAL_MB} MB)..."
IMG="${NOVA_DIR}/${OUTPUT}"
dd if=/dev/zero of="$IMG" bs=1M count="$TOTAL_MB" status=none
ok "Blank image created"

# Partition table (GPT)
log "Writing GPT partition table..."
parted -s "$IMG" mklabel gpt
parted -s "$IMG" mkpart "EFI System" fat32 1MiB "$(( EFI_MB + 1 ))MiB"
parted -s "$IMG" set 1 esp on
parted -s "$IMG" mkpart "NOVA Data"  ext4  "$(( EFI_MB + 2 ))MiB" "$(( TOTAL_MB - 1 ))MiB"
ok "GPT written (2 partitions)"

# ──────────────────────────────────────────────────────────────── 5. EFI partition

log "Formatting EFI partition..."
EFI_FAT="${WORK}/efi.fat"
dd if=/dev/zero of="$EFI_FAT" bs=1M count="$EFI_MB" status=none
mkfs.fat -F 32 -n "EFI" "$EFI_FAT" > /dev/null

log "Writing EFI files..."
MCOPY_ENV="MTOOLS_SKIP_CHECK=1"

env $MCOPY_ENV mmd -i "$EFI_FAT" ::EFI
env $MCOPY_ENV mmd -i "$EFI_FAT" ::EFI/BOOT
env $MCOPY_ENV mmd -i "$EFI_FAT" ::boot

# BOOTX64.EFI
env $MCOPY_ENV mcopy -i "$EFI_FAT" "$EFI_OUT" ::EFI/BOOT/BOOTX64.EFI

# grub.cfg
GRUB_CFG="${WORK}/grub.cfg"
cat > "$GRUB_CFG" << 'GCFG'
# PyOS NOVA GRUB2 Boot Menu
set default=0
set timeout=3

menuentry "PyOS NOVA" {
    echo "Loading kernel..."
    linux /boot/vmlinuz root=/dev/ram0 rw quiet loglevel=3 \
          init=/sbin/init nova_boot=1 console=ttyS0,115200n8 console=tty0
    echo "Loading initramfs..."
    initrd /boot/nova_initrd.cpio.gz
    boot
}

menuentry "PyOS NOVA (verbose)" {
    linux /boot/vmlinuz root=/dev/ram0 rw \
          init=/sbin/init nova_boot=1 nova_loglevel=debug \
          console=ttyS0,115200n8 console=tty0
    initrd /boot/nova_initrd.cpio.gz
    boot
}

menuentry "PyOS NOVA (recovery)" {
    linux /boot/vmlinuz root=/dev/ram0 rw \
          init=/sbin/init nova_boot=1 nova_recovery=1 \
          console=ttyS0,115200n8 console=tty0
    initrd /boot/nova_initrd.cpio.gz
    boot
}
GCFG
env $MCOPY_ENV mcopy -i "$EFI_FAT" "$GRUB_CFG" ::EFI/BOOT/grub.cfg

# kernel + initrd
env $MCOPY_ENV mcopy -i "$EFI_FAT" "$KERNEL"  ::boot/vmlinuz
env $MCOPY_ENV mcopy -i "$EFI_FAT" "$INITRD"  ::boot/nova_initrd.cpio.gz

ok "EFI partition contents:"
env $MCOPY_ENV mdir -i "$EFI_FAT" :: 2>/dev/null || true
env $MCOPY_ENV mdir -i "$EFI_FAT" ::EFI/BOOT/ 2>/dev/null || true
env $MCOPY_ENV mdir -i "$EFI_FAT" ::boot/ 2>/dev/null || true

# ──────────────────────────────────────────────────────────────── 6. Inject

log "Injecting EFI partition into disk image..."
EFI_OFFSET=$(( 1 * 1024 * 1024 ))   # 1 MB offset
dd if="$EFI_FAT" of="$IMG" bs=1M seek=1 conv=notrunc status=none
ok "EFI partition injected at offset 1 MB"

# ──────────────────────────────────────────────────────────────── 7. Summary

FINAL_MB=$(( $(stat -c%s "$IMG") / 1024 / 1024 ))

echo ""
echo -e "${GRN}╔════════════════════════════════════════════════════╗${RST}"
echo -e "${GRN}║       PyOS NOVA USB Image — Build Complete         ║${RST}"
echo -e "${GRN}╠════════════════════════════════════════════════════╣${RST}"
echo -e "${GRN}║${RST}  Image:      ${OUTPUT} (${FINAL_MB} MB)            "
echo -e "${GRN}║${RST}  Kernel:     ${KERNEL_MB} MB"
echo -e "${GRN}║${RST}  Initramfs:  ${INITRD_MB} MB (Python + NOVA source)"
echo -e "${GRN}║${RST}  Bootloader: GRUB2 EFI (x86-64)"
echo -e "${GRN}║${RST}  Python PID1: boot/pyinit.py"
echo -e "${GRN}╠════════════════════════════════════════════════════╣${RST}"
echo -e "${GRN}║${RST}  Write to USB:"
echo -e "${GRN}║${RST}    sudo dd if=${OUTPUT} of=/dev/sdX bs=4M status=progress"
echo -e "${GRN}║${RST}"
echo -e "${GRN}║${RST}  Test in QEMU:"
echo -e "${GRN}║${RST}    qemu-system-x86_64 \\"
echo -e "${GRN}║${RST}      -bios /usr/share/ovmf/OVMF.fd \\"
echo -e "${GRN}║${RST}      -drive file=${OUTPUT},format=raw \\"
echo -e "${GRN}║${RST}      -m 2G -serial stdio -nographic"
echo -e "${GRN}╚════════════════════════════════════════════════════╝${RST}"
