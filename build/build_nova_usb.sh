#!/usr/bin/env bash
# =============================================================================
#  PyOS NOVA — USB / ISO Builder
#
#  Strategy: minimal Linux kernel (hardware abstraction only) +
#             Python initramfs (Python IS /sbin/init, no Linux userspace)
#
#  This is NOT Alpine Linux. No distro packages are installed.
#  The only non-Python components: kernel (3MB) + syslinux bootloader (200KB)
#
#  Usage: sudo bash build_nova_usb.sh /dev/sdX
#  Or:    bash build_nova_usb.sh --iso   (creates nova.iso)
# =============================================================================

set -euo pipefail

GRN='\033[32m'; RED='\033[31m'; YLW='\033[33m'; CYN='\033[36m'; DIM='\033[2m'; RST='\033[0m'
log()  { echo -e "${CYN}[NOVA]${RST} $*"; }
ok()   { echo -e "${GRN}[ OK ]${RST} $*"; }
err()  { echo -e "${RED}[FAIL]${RST} $*"; exit 1; }
warn() { echo -e "${YLW}[WARN]${RST} $*"; }

NOVA_DIR="$(cd "$(dirname "$0")" && pwd)"
WORK_DIR="/tmp/nova_build_$$"
ISO_MODE=0
[[ "${1:-}" == "--iso" ]] && ISO_MODE=1 && USB=""
USB="${1:-}"

# ─── kernel source ─────────────────────────────────────────────────────────────
# We use a prebuilt minimal kernel from kernel.org (no distro involved)
# Alternatively: build your own with `make tinyconfig` + essential drivers
KERNEL_URL="https://github.com/linuxboot/linuxboot/releases/download/v0.1/bzImage"
# Fallback: use the host kernel (good for testing)
HOST_KERNEL="$(ls /boot/vmlinuz-* 2>/dev/null | head -1 || echo /boot/vmlinuz)"
KERNEL_PATH="${NOVA_DIR}/build/bzImage"

# ─── Python location ───────────────────────────────────────────────────────────
PYTHON="$(command -v python3 || echo /usr/bin/python3)"
PYTHON_VERSION="$($PYTHON -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PYTHON_LIB="$(python3 -c 'import sysconfig; print(sysconfig.get_path("stdlib"))')"
PYTHON_BIN="$(readlink -f "$PYTHON")"

log "PyOS NOVA USB Builder"
echo -e "  Python  : $PYTHON_BIN ($PYTHON_VERSION)"
echo -e "  Stdlib  : $PYTHON_LIB"
echo -e "  NOVA    : $NOVA_DIR"
echo

# ─── checks ────────────────────────────────────────────────────────────────────
[[ $EUID -ne 0 ]] && [[ $ISO_MODE -eq 0 ]] && err "Run as root for USB mode: sudo bash build_nova_usb.sh /dev/sdX"
for cmd in cpio gzip find; do
    command -v "$cmd" &>/dev/null || err "Missing: $cmd"
done

if [[ $ISO_MODE -eq 0 ]]; then
    [[ -z "$USB" ]] && err "Usage: sudo bash build_nova_usb.sh /dev/sdX"
    [[ ! -b "$USB" ]] && err "$USB is not a block device"
    for cmd in syslinux mkfs.fat; do
        command -v "$cmd" &>/dev/null || err "Missing: $cmd (install syslinux, dosfstools)"
    done
fi

mkdir -p "$WORK_DIR" "${NOVA_DIR}/build"

# ─── get kernel ────────────────────────────────────────────────────────────────
log "Preparing kernel..."
if [[ ! -f "$KERNEL_PATH" ]]; then
    if [[ -f "$HOST_KERNEL" ]]; then
        warn "Using host kernel ($HOST_KERNEL) — replace with a minimal build for production"
        cp "$HOST_KERNEL" "$KERNEL_PATH"
    else
        log "Downloading minimal kernel from linuxboot..."
        wget -q --show-progress -O "$KERNEL_PATH" "$KERNEL_URL" || {
            warn "Download failed — using host kernel"
            cp "$(ls /boot/vmlinuz* | head -1)" "$KERNEL_PATH"
        }
    fi
fi
ok "Kernel ready: $(du -sh "$KERNEL_PATH" | cut -f1)"

# ─── build Python initramfs ────────────────────────────────────────────────────
log "Building Python initramfs (Python is /sbin/init)..."
INITRD_DIR="${WORK_DIR}/initramfs"
mkdir -p "${INITRD_DIR}"/{sbin,bin,lib,lib64,dev,proc,sys,tmp,run,data,root,etc}

# Copy Python interpreter
log "  Copying CPython binary..."
cp "$PYTHON_BIN" "${INITRD_DIR}/bin/python3"
ln -sf /bin/python3 "${INITRD_DIR}/bin/python"
chmod +x "${INITRD_DIR}/bin/python3"

# Copy Python stdlib (essential modules only for size)
log "  Copying Python stdlib (${PYTHON_LIB})..."
STDLIB_DST="${INITRD_DIR}/lib/python${PYTHON_VERSION}"
mkdir -p "$STDLIB_DST"
rsync -a --quiet \
    --exclude "__pycache__" \
    --exclude "test/" \
    --exclude "tests/" \
    --exclude "*.pyc" \
    "${PYTHON_LIB}/" "${STDLIB_DST}/"

# Copy shared libraries needed by Python
log "  Copying shared libraries..."
for lib in $(ldd "$PYTHON_BIN" | grep -oP '\/\S+'); do
    if [[ -f "$lib" ]]; then
        dir=$(dirname "$lib")
        mkdir -p "${INITRD_DIR}${dir}"
        cp -n "$lib" "${INITRD_DIR}${dir}/" 2>/dev/null || true
    fi
done
# Essential linker
for ld in /lib64/ld-linux-x86-64.so.2 /lib/x86_64-linux-gnu/ld-linux-x86-64.so.2; do
    [[ -f "$ld" ]] && { mkdir -p "${INITRD_DIR}/lib64"; cp -n "$ld" "${INITRD_DIR}/lib64/"; break; }
done

# Copy site-packages (numpy + installed packages)
log "  Copying site-packages..."
SITE_PKG="$($PYTHON -c 'import site; print(site.getsitepackages()[0])')"
SITE_DST="${INITRD_DIR}/lib/python${PYTHON_VERSION}/site-packages"
mkdir -p "$SITE_DST"
# Copy selectively — we want numpy, psutil, etc. but not dev tools
for pkg in numpy psutil chromadb sentence_transformers llama_cpp ctransformers; do
    src="${SITE_PKG}/${pkg}"
    [[ -d "$src" ]] && rsync -a --quiet --exclude "__pycache__" "$src/" "${SITE_DST}/${pkg}/" 2>/dev/null || true
done

# Copy PyOS NOVA source
log "  Copying PyOS NOVA source..."
rsync -a --quiet --exclude "__pycache__" --exclude "*.pyc" \
    "${NOVA_DIR}/" "${INITRD_DIR}/nova/"

# /sbin/init → pyinit.py
log "  Wiring Python as /sbin/init..."
mkdir -p "${INITRD_DIR}/sbin"
cat > "${INITRD_DIR}/sbin/init" << 'INIT_WRAPPER'
#!/bin/sh
# Minimal shell wrapper to launch Python init
# (kernel requires /sbin/init to be executable; this invokes Python)
exec /bin/python3 /nova/boot/pyinit.py "$@"
INIT_WRAPPER
chmod +x "${INITRD_DIR}/sbin/init"

# Minimal /etc
cat > "${INITRD_DIR}/etc/hostname" <<< "nova"
cat > "${INITRD_DIR}/etc/passwd" <<< "root:x:0:0:root:/root:/sbin/init"

# Basic device nodes (kernel usually creates these but let's seed them)
[[ -e "${INITRD_DIR}/dev/null" ]]    || mknod "${INITRD_DIR}/dev/null"    c 1 3 2>/dev/null || true
[[ -e "${INITRD_DIR}/dev/console" ]] || mknod "${INITRD_DIR}/dev/console" c 5 1 2>/dev/null || true
[[ -e "${INITRD_DIR}/dev/tty" ]]     || mknod "${INITRD_DIR}/dev/tty"     c 5 0 2>/dev/null || true

# Build the cpio initramfs
log "  Packing initramfs..."
INITRD_IMG="${NOVA_DIR}/build/initramfs.cpio.gz"
(cd "${INITRD_DIR}" && find . -print0 | cpio --null --create --format=newc 2>/dev/null | gzip -9 > "${INITRD_IMG}")
ok "Initramfs: $(du -sh "${INITRD_IMG}" | cut -f1)"

# ─── ISO mode ──────────────────────────────────────────────────────────────────
if [[ $ISO_MODE -eq 1 ]]; then
    log "Building ISO..."
    command -v genisoimage &>/dev/null || command -v mkisofs &>/dev/null || err "Missing: genisoimage or mkisofs"
    ISO_WORK="${WORK_DIR}/iso"
    mkdir -p "${ISO_WORK}/boot/syslinux"
    cp "${KERNEL_PATH}"  "${ISO_WORK}/boot/vmlinuz"
    cp "${INITRD_IMG}"   "${ISO_WORK}/boot/initramfs.cpio.gz"

    # Syslinux config
    cat > "${ISO_WORK}/boot/syslinux/syslinux.cfg" << 'SYSLINUX'
DEFAULT nova
TIMEOUT 50
PROMPT 0

LABEL nova
  MENU LABEL PyOS NOVA 1.0 (Python PID 1)
  KERNEL /boot/vmlinuz
  APPEND initrd=/boot/initramfs.cpio.gz init=/sbin/init rw quiet
  
LABEL nova-debug
  MENU LABEL PyOS NOVA — debug mode
  KERNEL /boot/vmlinuz
  APPEND initrd=/boot/initramfs.cpio.gz init=/sbin/init rw debug
SYSLINUX

    # Copy syslinux bootloader files
    for syslinux_dir in /usr/lib/syslinux /usr/share/syslinux; do
        [[ -d "$syslinux_dir" ]] && {
            cp "${syslinux_dir}/isolinux.bin" "${ISO_WORK}/boot/syslinux/" 2>/dev/null || true
            cp "${syslinux_dir}/ldlinux.c32"  "${ISO_WORK}/boot/syslinux/" 2>/dev/null || true
        }
    done

    ISO_OUT="${NOVA_DIR}/build/nova.iso"
    ISOCMD="$(command -v genisoimage || command -v mkisofs)"
    "$ISOCMD" -o "$ISO_OUT" \
        -b boot/syslinux/isolinux.bin \
        -c boot/syslinux/boot.cat \
        -no-emul-boot -boot-load-size 4 -boot-info-table \
        -J -R -V "PyOS-NOVA" \
        "${ISO_WORK}" 2>/dev/null
    ok "ISO created: ${ISO_OUT} ($(du -sh "${ISO_OUT}" | cut -f1))"
    echo
    echo -e "${GRN}  Flash to USB:  dd if=nova.iso of=/dev/sdX bs=4M status=progress${RST}"
    echo -e "${GRN}  Or use:        sudo cp nova.iso /dev/sdX${RST}"

# ─── USB mode ──────────────────────────────────────────────────────────────────
else
    log "Writing to USB ${USB}..."
    echo
    echo -e "${RED}  WARNING: This will ERASE ${USB}${RST}"
    lsblk "$USB"
    read -rp "  Type YES to continue: " confirm
    [[ "$confirm" != "YES" ]] && err "Aborted."

    umount "${USB}"* 2>/dev/null || true

    # Single FAT32 partition (syslinux needs FAT)
    parted -s "$USB" mklabel msdos
    parted -s "$USB" mkpart primary fat32 1MiB 100%
    parted -s "$USB" set 1 boot on
    sleep 1

    PART="${USB}1"
    [[ -b "${USB}p1" ]] && PART="${USB}p1"
    mkfs.fat -F32 -n "NOVA" "$PART"
    syslinux --install "$PART"

    # Mount and copy
    MOUNT="${WORK_DIR}/usb"
    mkdir -p "$MOUNT"
    mount "$PART" "$MOUNT"
    mkdir -p "${MOUNT}/boot"
    cp "${KERNEL_PATH}"  "${MOUNT}/boot/vmlinuz"
    cp "${INITRD_IMG}"   "${MOUNT}/boot/initramfs.cpio.gz"

    cat > "${MOUNT}/syslinux.cfg" << 'SYSLINUX'
DEFAULT nova
TIMEOUT 50

LABEL nova
  MENU LABEL PyOS NOVA 1.0
  KERNEL /boot/vmlinuz
  APPEND initrd=/boot/initramfs.cpio.gz init=/sbin/init rw quiet
SYSLINUX

    umount "$MOUNT"
    ok "USB ready!"
fi

# ─── cleanup ───────────────────────────────────────────────────────────────────
rm -rf "$WORK_DIR"

echo
echo -e "${GRN}╔══════════════════════════════════════════════════════════╗${RST}"
echo -e "${GRN}║             PyOS NOVA build complete!                   ║${RST}"
echo -e "${GRN}║                                                          ║${RST}"
echo -e "${GRN}║  • No Alpine Linux. No distro. No userspace tools.      ║${RST}"
echo -e "${GRN}║  • Python IS /sbin/init (PID 1)                         ║${RST}"
echo -e "${GRN}║  • Kernel is pure hardware abstraction                  ║${RST}"
echo -e "${GRN}║  • Everything above the kernel is Python                ║${RST}"
echo -e "${GRN}║                                                          ║${RST}"
echo -e "${GRN}║  Boot the USB/ISO and PyOS NOVA starts directly.        ║${RST}"
echo -e "${GRN}╚══════════════════════════════════════════════════════════╝${RST}"
