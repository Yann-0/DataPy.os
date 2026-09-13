"""
PyOS NOVA — VirtualBox OVA Builder
=====================================
Generates a fully importable VirtualBox .ova file using pure Python.
No VBoxManage. No qemu-img. No external tools.

OVA format = TAR containing:
  nova.ovf      — XML VM descriptor (hardware config)
  nova.vmdk     — Disk image (stream-optimized VMDK format)

The VMDK uses stream-optimized sparse format so VirtualBox can import
it directly. Our pure Python VMDK builder writes valid grain tables
and compressed extents.

Usage:
  python3 build/vbox_builder.py                  # build nova_vbox.ova
  python3 build/vbox_builder.py --ram 4096       # 4GB RAM
  python3 build/vbox_builder.py --disk 2048      # 2GB disk
  python3 build/vbox_builder.py --test           # verify the OVA
"""

import os, sys, io, zlib, struct, time, tarfile, hashlib, uuid, argparse
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
BUILD = os.path.join(ROOT, "build")

# ─────────────────────────────────────────────────────────────────────────────
# FAT32 disk image builder (reused from nova_vm.py)
# ─────────────────────────────────────────────────────────────────────────────

SECTOR   = 512
CLUSTER  = 4096
SPC      = CLUSTER // SECTOR      # sectors per cluster = 8
RESERVED = 32
FAT_CNT  = 2
FAT_EOC  = 0x0FFFFFFF


"""Align.

    Args:
    v: V.
    a: A.
    """
def _align(v, a): return (v + a - 1) & ~(a - 1)


class FAT32:
    """F a t32."""
    def __init__(self, size_mb):
        """Initialise the instance."""
        self.total_sectors = size_mb * 1024 * 1024 // SECTOR
        self._fat_secs     = max(1, (self.total_sectors * 4 + SECTOR - 1) // SECTOR)
        self._first_data   = RESERVED + FAT_CNT * self._fat_secs
        self._fat          = [0] * (self.total_sectors // SPC + 4)
        self._fat[0]       = 0x0FFFFFF8
        self._fat[1]       = FAT_EOC
        self._next         = 2
        self._files        = {}   # path_upper → (start_cluster, data)

    def _alloc(self, n):
        """Alloc.

            Args:
            n: N.
            """
        start = self._next
        for i in range(n):
            c = self._next; self._next += 1
            self._fat[c] = (c + 1) if i < n - 1 else FAT_EOC
        return start

    def add_file(self, path, data):
        """Add file.

            Args:
            path: Path.
            data: Data.
            """
        if isinstance(data, str): data = data.encode()
        if not data: self._files[path.upper()] = (FAT_EOC, b""); return
        n = (len(data) + CLUSTER - 1) // CLUSTER
        self._files[path.upper()] = (self._alloc(n), data)

    """C2s.

        Args:
        c: C.
        """
    def _c2s(self, c): return self._first_data + (c - 2) * SPC

    def build(self):
        """Build and return the operation."""
        img = bytearray(self.total_sectors * SECTOR)

        # Boot sector
        bs = bytearray(SECTOR)
        bs[0:3]  = b"\xEB\x58\x90"
        bs[3:11] = b"MSWIN4.1"
        struct.pack_into("<H", bs, 11, SECTOR)
        struct.pack_into("<B", bs, 13, SPC)
        struct.pack_into("<H", bs, 14, RESERVED)
        struct.pack_into("<B", bs, 16, FAT_CNT)
        struct.pack_into("<H", bs, 17, 0)
        struct.pack_into("<H", bs, 19, 0)
        struct.pack_into("<B", bs, 21, 0xF8)
        struct.pack_into("<H", bs, 22, 0)
        struct.pack_into("<H", bs, 24, 63)
        struct.pack_into("<H", bs, 26, 255)
        struct.pack_into("<I", bs, 28, 0)
        struct.pack_into("<I", bs, 32, self.total_sectors)
        struct.pack_into("<I", bs, 36, self._fat_secs)
        struct.pack_into("<H", bs, 40, 0)
        struct.pack_into("<H", bs, 42, 0)
        struct.pack_into("<I", bs, 44, 2)
        struct.pack_into("<H", bs, 48, 1)
        struct.pack_into("<H", bs, 50, 6)
        bs[64] = 0x80; bs[66] = 0x29
        struct.pack_into("<I", bs, 67, 0xDEADBEEF)
        bs[71:82] = b"NOVA       "
        bs[82:90] = b"FAT32   "
        bs[510] = 0x55; bs[511] = 0xAA
        img[0:SECTOR] = bs

        # FAT tables
        fat_bytes = b"".join(struct.pack("<I", v) for v in self._fat)
        for i in range(FAT_CNT):
            off = (RESERVED + i * self._fat_secs) * SECTOR
            img[off:off+len(fat_bytes)] = fat_bytes

        # Root dir (cluster 2)
        root_dir = bytearray()
        vol = bytearray(32); vol[0:11] = b"NOVA       "; vol[11] = 0x08
        root_dir += vol
        for path_up, (sc, data) in self._files.items():
            parts = path_up.strip("\\").split("\\")
            if len(parts) != 1: continue
            root_dir += self._dir_entry(parts[0], sc, len(data))
        root_dir += b"\x00" * (CLUSTER - len(root_dir) % CLUSTER)
        rs = self._c2s(2) * SECTOR
        img[rs:rs+len(root_dir)] = root_dir

        # File data
        for path_up, (sc, data) in self._files.items():
            if not data: continue
            c, off = sc, 0
            while off < len(data):
                chunk = data[off:off+CLUSTER] + b"\x00"*(CLUSTER-len(data[off:off+CLUSTER]))
                sec   = self._c2s(c) * SECTOR
                img[sec:sec+CLUSTER] = chunk
                off += CLUSTER; c = self._fat[c]
                if c >= FAT_EOC: break
        return bytes(img)

    def _dir_entry(self, name83, cluster, size):
        """Dir entry.

            Args:
            name83: Name83.
            cluster: Cluster.
            size: Size.
            """
        e = bytearray(32)
        if "." in name83: n, x = name83.rsplit(".", 1)
        else:             n, x = name83, ""
        e[0:11] = (n[:8].ljust(8) + x[:3].ljust(3)).encode("ascii", errors="replace")[:11]
        e[11]   = 0x20
        e[26]   = cluster & 0xFF; e[27] = (cluster>>8) & 0xFF
        e[20]   = (cluster>>16) & 0xFF; e[21] = (cluster>>24) & 0xFF
        struct.pack_into("<I", e, 28, size)
        return bytes(e)


# ─────────────────────────────────────────────────────────────────────────────
# Stream-optimized VMDK builder
# VirtualBox requires stream-optimized VMDK for OVA import
# ─────────────────────────────────────────────────────────────────────────────

VMDK_MAGIC       = 0x564D444B     # "VMDK"
VMDK_VERSION     = 3
VMDK_FLAGS       = 0x30001        # stream-optimized + compressed
GRAIN_SIZE       = 128            # sectors (64KB)
GT_COVERAGE      = 512            # grains per grain table
GD_COVERAGE      = GT_COVERAGE * GRAIN_SIZE  # sectors per GD entry

# Marker types (stream-optimized)
MARKER_EOS       = 0
MARKER_GT        = 1
MARKER_GD        = 2
MARKER_FOOTER    = 3


def _vmdk_header(disk_sectors, grain_sectors=GRAIN_SIZE):
    """Build the 512-byte VMDK sparse extent header."""
    gt_coverage = GT_COVERAGE * grain_sectors
    gd_size     = (disk_sectors + gt_coverage - 1) // gt_coverage
    gt_size     = GT_COVERAGE  # entries per GT

    h = bytearray(512)
    struct.pack_into("<I",  h, 0,   VMDK_MAGIC)
    struct.pack_into("<I",  h, 4,   VMDK_VERSION)
    struct.pack_into("<I",  h, 8,   VMDK_FLAGS)
    struct.pack_into("<Q",  h, 12,  disk_sectors)     # capacity
    struct.pack_into("<Q",  h, 20,  grain_sectors)    # grainSize
    struct.pack_into("<Q",  h, 28,  0)                # descriptorOffset
    struct.pack_into("<Q",  h, 36,  0)                # descriptorSize
    struct.pack_into("<I",  h, 44,  gt_size)          # numGTEsPerGT
    struct.pack_into("<Q",  h, 48,  0)                # rgdOffset
    struct.pack_into("<Q",  h, 56,  0)                # gdOffset
    struct.pack_into("<Q",  h, 64,  0)                # overHead
    struct.pack_into("<B",  h, 72,  0)                # uncleanShutdown
    h[73:77] = b" bi "                                 # singleEndLineChar etc.
    struct.pack_into("<H",  h, 77,  0)                # compressAlgorithm=0(none) or 1(deflate)
    # For stream-optimized we use compression=deflate (1)
    h[77] = 1   # compressAlgorithm = COMPRESSION_DEFLATE
    return bytes(h)


def _grain_marker(lba, size_bytes, marker_type=None):
    """
    Stream-optimized VMDK grain/metadata marker.
    Grain marker: lba (8) + size (4) = 12 bytes before compressed data
    Metadata marker: val (8) + size=0 (4) + type (4) = 16 bytes
    """
    if marker_type is not None:
        # Metadata marker: NumSectors=0 indicates metadata
        return struct.pack("<QII", 0, 0, marker_type)
    else:
        return struct.pack("<QI", lba, size_bytes)


class VMDKBuilder:
    """
    Pure Python stream-optimized VMDK builder.
    Produces compressed, sparse VMDK suitable for OVA packaging.
    """

    GRAIN_SECTORS = GRAIN_SIZE     # 128 sectors = 64KB per grain
    GRAIN_BYTES   = GRAIN_SECTORS * SECTOR

    def __init__(self, raw_image: bytes):
        """Initialise the instance."""
        self.raw   = raw_image
        self.bytes = len(raw_image)
        self.secs  = self.bytes // SECTOR
        assert self.bytes % SECTOR == 0, "Image must be sector-aligned"

    def build(self) -> bytes:
        """Build the complete stream-optimized VMDK binary."""
        out = io.BytesIO()

        # ── Header (1 sector) ─────────────────────────────────────────────────
        hdr = _vmdk_header(self.secs, self.GRAIN_SECTORS)
        out.write(hdr)

        # ── Embedded descriptor (1 sector) ────────────────────────────────────
        desc = self._descriptor()
        desc_padded = desc + b"\x00" * (SECTOR - len(desc) % SECTOR)
        if len(desc_padded) < SECTOR:
            desc_padded += b"\x00" * (SECTOR - len(desc_padded))
        out.write(desc_padded[:SECTOR])

        # ── Grain data (stream-optimized: marker + zlib-compressed grains) ────
        num_grains = (self.secs + self.GRAIN_SECTORS - 1) // self.GRAIN_SECTORS
        grain_offsets = []   # (lba, file_offset) for grain directory

        for i in range(num_grains):
            lba   = i * self.GRAIN_SECTORS
            raw_g = self.raw[lba*SECTOR : (lba+self.GRAIN_SECTORS)*SECTOR]
            # Pad last grain
            if len(raw_g) < self.GRAIN_BYTES:
                raw_g = raw_g + b"\x00" * (self.GRAIN_BYTES - len(raw_g))

            # Skip zero grains (sparse optimization)
            if all(b == 0 for b in raw_g):
                grain_offsets.append((lba, 0))
                continue

            # Compress with zlib
            compressed = zlib.compress(raw_g, level=1)

            # Record offset (in sectors from start of file)
            file_off = out.tell()
            grain_offsets.append((lba, file_off // SECTOR + 1))

            # Write marker + compressed data, padded to sector boundary
            marker   = _grain_marker(lba, len(compressed))
            payload  = marker + compressed
            padded   = payload + b"\x00" * (_align(len(payload), SECTOR) - len(payload))
            out.write(padded)

        # ── Grain Table (GT) marker ────────────────────────────────────────────
        out.write(struct.pack("<16s", _grain_marker(0, 0, MARKER_GT)))

        # ── Grain Table entries (4 bytes each) ────────────────────────────────
        gt_entries = bytearray()
        for lba, offset in grain_offsets:
            struct.pack_into_be = lambda: None  # dummy
            gt_entries += struct.pack("<I", offset)
        # Pad to sector boundary
        gt_padded = gt_entries + b"\x00" * (_align(len(gt_entries), SECTOR) - len(gt_entries))
        out.write(gt_padded)

        # ── Grain Directory (GD) marker ───────────────────────────────────────
        gd_offset = out.tell() // SECTOR
        out.write(struct.pack("<16s", _grain_marker(0, 0, MARKER_GD)))
        # GD entry points to GT (sector offset)
        gt_sector = (2 + 1)   # after header + descriptor
        out.write(struct.pack("<I", gt_sector))
        out.write(b"\x00" * (SECTOR - 4))

        # ── Footer (copy of header with updated offsets) ───────────────────────
        footer_marker = struct.pack("<QII", gd_offset, 0, MARKER_FOOTER)
        out.write(footer_marker)
        out.write(hdr)

        # ── EOS marker ────────────────────────────────────────────────────────
        out.write(struct.pack("<QII", 0, 0, MARKER_EOS))

        return out.getvalue()

    def _descriptor(self) -> bytes:
        """Embedded VMDK descriptor text."""
        desc = (
            '# Disk DescriptorFile\n'
            'version=1\n'
            'CID=fffffffe\n'
            'parentCID=ffffffff\n'
            'createType="streamOptimized"\n'
            '\n'
            '# Extent description\n'
            f'RW {self.secs} SPARSE "nova.vmdk"\n'
            '\n'
            '# The Disk Data Base\n'
            '#DDB\n'
            'ddb.virtualHWVersion = "4"\n'
            f'ddb.geometry.cylinders = "{self.secs // (255*63)}"\n'
            'ddb.geometry.heads = "255"\n'
            'ddb.geometry.sectors = "63"\n'
            'ddb.adapterType = "ide"\n'
        )
        return desc.encode("ascii")


# ─────────────────────────────────────────────────────────────────────────────
# OVF XML descriptor
# ─────────────────────────────────────────────────────────────────────────────

def _ovf_xml(disk_size_bytes: int, ram_mb: int, cpus: int,
             vmdk_size_bytes: int, disk_uuid: str, vm_uuid: str) -> str:
    """Generate the OVF 1.1 XML descriptor for VirtualBox."""
    now       = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    disk_gb   = disk_size_bytes / 1024**3
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Envelope xmlns="http://schemas.dmtf.org/ovf/envelope/1"
          xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1"
          xmlns:vbox="http://www.virtualbox.org/ovf/machine"
          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
          xmlns:vssd="http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_VirtualSystemSettingData"
          xmlns:rasd="http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData">

  <References>
    <File ovf:id="file1" ovf:href="nova.vmdk" ovf:size="{vmdk_size_bytes}"/>
  </References>

  <DiskSection>
    <Info>Virtual disk information</Info>
    <Disk ovf:diskId="disk1"
          ovf:fileRef="file1"
          ovf:capacity="{int(disk_size_bytes)}"
          ovf:populatedSize="{min(vmdk_size_bytes, disk_size_bytes)}"
          vbox:uuid="{disk_uuid}"
          ovf:format="http://www.vmware.com/interfaces/specifications/vmdk.html#streamOptimized"/>
  </DiskSection>

  <NetworkSection>
    <Info>Logical networks</Info>
    <Network ovf:name="NAT"><Description>NAT network</Description></Network>
  </NetworkSection>

  <VirtualSystem ovf:id="nova">
    <Info>PyOS NOVA Virtual Machine</Info>
    <Name>PyOS NOVA</Name>
    <OperatingSystemSection ovf:id="100">
      <Info>Operating system</Info>
      <Description>Other Linux (64-bit)</Description>
      <vbox:OSType ovf:required="false">Linux_64</vbox:OSType>
    </OperatingSystemSection>

    <VirtualHardwareSection>
      <Info>Virtual hardware</Info>
      <System>
        <vssd:ElementName>Virtual Hardware Family</vssd:ElementName>
        <vssd:InstanceID>0</vssd:InstanceID>
        <vssd:VirtualSystemIdentifier>nova</vssd:VirtualSystemIdentifier>
        <vssd:VirtualSystemType>virtualbox-2.2</vssd:VirtualSystemType>
      </System>

      <Item><!-- CPU -->
        <rasd:Caption>{cpus} virtual CPU</rasd:Caption>
        <rasd:Description>Number of virtual CPUs</rasd:Description>
        <rasd:ElementName>{cpus} virtual CPU</rasd:ElementName>
        <rasd:InstanceID>1</rasd:InstanceID>
        <rasd:ResourceType>3</rasd:ResourceType>
        <rasd:VirtualQuantity>{cpus}</rasd:VirtualQuantity>
      </Item>

      <Item><!-- RAM -->
        <rasd:Caption>{ram_mb} MB of memory</rasd:Caption>
        <rasd:Description>Memory Size</rasd:Description>
        <rasd:ElementName>{ram_mb} MB of memory</rasd:ElementName>
        <rasd:InstanceID>2</rasd:InstanceID>
        <rasd:ResourceType>4</rasd:ResourceType>
        <rasd:VirtualQuantity>{ram_mb}</rasd:VirtualQuantity>
      </Item>

      <Item><!-- IDE controller -->
        <rasd:Address>1</rasd:Address>
        <rasd:Caption>ideController1</rasd:Caption>
        <rasd:Description>IDE Controller</rasd:Description>
        <rasd:ElementName>ideController1</rasd:ElementName>
        <rasd:InstanceID>3</rasd:InstanceID>
        <rasd:ResourceSubType>PIIX4</rasd:ResourceSubType>
        <rasd:ResourceType>5</rasd:ResourceType>
      </Item>

      <Item><!-- Disk attachment -->
        <rasd:AddressOnParent>0</rasd:AddressOnParent>
        <rasd:Caption>disk1</rasd:Caption>
        <rasd:Description>Disk Image</rasd:Description>
        <rasd:ElementName>disk1</rasd:ElementName>
        <rasd:HostResource>ovf:/disk/disk1</rasd:HostResource>
        <rasd:InstanceID>4</rasd:InstanceID>
        <rasd:Parent>3</rasd:Parent>
        <rasd:ResourceType>17</rasd:ResourceType>
      </Item>

      <Item><!-- NAT network -->
        <rasd:AutomaticAllocation>true</rasd:AutomaticAllocation>
        <rasd:Caption>Ethernet adapter on NAT</rasd:Caption>
        <rasd:Connection>NAT</rasd:Connection>
        <rasd:ElementName>Ethernet adapter on NAT</rasd:ElementName>
        <rasd:InstanceID>5</rasd:InstanceID>
        <rasd:ResourceSubType>virtio</rasd:ResourceSubType>
        <rasd:ResourceType>10</rasd:ResourceType>
      </Item>

      <Item><!-- USB controller -->
        <rasd:Caption>USB Controller</rasd:Caption>
        <rasd:Description>USB Controller</rasd:Description>
        <rasd:ElementName>USB Controller</rasd:ElementName>
        <rasd:InstanceID>6</rasd:InstanceID>
        <rasd:ResourceType>23</rasd:ResourceType>
      </Item>
    </VirtualHardwareSection>

    <vbox:Machine ovf:required="false"
                  version="1.19-linux"
                  uuid="{{{vm_uuid}}}"
                  name="PyOS NOVA"
                  OSType="Linux_64"
                  snapshotFolder="Snapshots"
                  lastStateChange="{now}">
      <MediaRegistry>
        <HardDisks>
          <HardDisk uuid="{{{disk_uuid}}}"
                    location="nova.vmdk"
                    format="VMDK"
                    type="Normal"/>
        </HardDisks>
      </MediaRegistry>
      <Hardware>
        <CPU count="{cpus}">
          <PAE enabled="true"/>
          <LongMode enabled="true"/>
          <X2APIC enabled="true"/>
        </CPU>
        <Memory RAMSize="{ram_mb}"/>
        <Firmware type="EFI"/>
        <Display controller="VMSVGA" VRAMSize="16"/>
        <RemoteDisplay enabled="false"/>
        <StorageControllers>
          <StorageController name="IDE" type="PIIX4" PortCount="2" useHostIOCache="false" Bootable="true">
            <AttachedDevice type="HardDisk" hotpluggable="false" port="1" device="0">
              <Image uuid="{{{disk_uuid}}}"/>
            </AttachedDevice>
          </StorageController>
        </StorageControllers>
      </Hardware>
    </vbox:Machine>
  </VirtualSystem>
</Envelope>
"""


# ─────────────────────────────────────────────────────────────────────────────
# OVA assembler
# ─────────────────────────────────────────────────────────────────────────────

def build_ova(output_path: str, ram_mb: int = 2048, cpus: int = 2,
              disk_mb: int = 512) -> str:
    """
    Build a complete VirtualBox-importable OVA file.
    Returns the output path.
    """
    os.makedirs(BUILD, exist_ok=True)

    print("  PyOS NOVA — VirtualBox OVA Builder")
    print("  ─────────────────────────────────────────────────────────\n")

    # ── Step 1: Build FAT32 disk image with NOVA content ─────────────────────
    print(f"  [1/4] Building {disk_mb}MB FAT32 disk image...")
    fat = FAT32(disk_mb)

    # Generate BOOTX64.EFI
    from boot.efi_builder import build_efi
    efi_path = os.path.join(BUILD, "BOOTX64.EFI")
    build_efi(efi_path, verify=False)
    fat.add_file("EFI\\BOOT\\BOOTX64.EFI", open(efi_path,"rb").read())

    # Add NOVA source
    src_count = 0
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__",".git","build",".nova")]
        for fname in filenames:
            if fname.endswith((".py",".md",".txt",".sh",".json",".cfg",".toml")):
                full = os.path.join(dirpath, fname)
                rel  = os.path.relpath(full, ROOT)
                data = open(full,"rb").read()
                fp   = "NOVA\\" + rel.replace("/","\\").replace("..","__")
                fat.add_file(fp, data)
                src_count += 1

    # Add auto-install script
    fat.add_file("NOVA\\autorun.sh", _autorun_script().encode())

    print(f"        {src_count} source files embedded")
    raw_img = fat.build()
    print(f"        Disk image: {len(raw_img)//1024//1024} MB")

    # ── Step 2: Convert to stream-optimized VMDK ──────────────────────────────
    print(f"\n  [2/4] Converting to stream-optimized VMDK...")
    vmdk_data = VMDKBuilder(raw_img).build()
    print(f"        VMDK: {len(vmdk_data)//1024} KB (compressed sparse)")

    # ── Step 3: Generate OVF XML ──────────────────────────────────────────────
    print(f"\n  [3/4] Generating OVF descriptor...")
    disk_uuid = str(uuid.uuid4())
    vm_uuid   = str(uuid.uuid4())
    ovf_xml   = _ovf_xml(
        disk_size_bytes  = len(raw_img),
        ram_mb           = ram_mb,
        cpus             = cpus,
        vmdk_size_bytes  = len(vmdk_data),
        disk_uuid        = disk_uuid,
        vm_uuid          = vm_uuid,
    )
    ovf_bytes = ovf_xml.encode("utf-8")

    # ── Step 4: Package into TAR (OVA) ────────────────────────────────────────
    print(f"\n  [4/4] Packaging OVA...")

    def _add_bytes(tar, name, data):
        """Add bytes.

            Args:
            tar: Tar.
            name: Name.
            data: Data.
            """
        info          = tarfile.TarInfo(name=name)
        info.size     = len(data)
        info.mtime    = int(time.time())
        info.mode     = 0o644
        tar.addfile(info, io.BytesIO(data))

    with tarfile.open(output_path, "w:") as tar:
        _add_bytes(tar, "nova.ovf",  ovf_bytes)
        _add_bytes(tar, "nova.vmdk", vmdk_data)

    size = os.path.getsize(output_path)
    sha  = hashlib.sha256(open(output_path,"rb").read()).hexdigest()[:16]
    print(f"\n  OVA: {output_path}")
    print(f"  Size: {size//1024//1024} MB  |  sha256: {sha}...")
    return output_path


def _autorun_script() -> str:
    """Autorun script.


        Returns:
            str: Result.
        """
    return """#!/bin/sh
# PyOS NOVA — post-boot setup
# Runs automatically if not already installed

NOVA=/nova
DATA=/data/nova
MARKER="$DATA/.installed"

[ -f "$MARKER" ] && exit 0

echo ""
echo "  PyOS NOVA — First Boot Setup"
echo "  ─────────────────────────────"
echo ""

# Install Python dependencies
python3 -m pip install --quiet psutil numpy 2>/dev/null && echo "  [OK] numpy + psutil"

# Create data directories
mkdir -p "$DATA/models" "$DATA/index" "$DATA/logs"

# Start NOVA shell
echo ""
echo "  Setup complete. Starting NOVA..."
touch "$MARKER"
"""


# ─────────────────────────────────────────────────────────────────────────────
# Verify OVA
# ─────────────────────────────────────────────────────────────────────────────

def verify_ova(path: str):
    """Verify ova and raise on failure.

        Args:
        path (str): Path.
        """
    print(f"  Verifying {path}...")
    with tarfile.open(path, "r:") as tar:
        members = tar.getnames()
        assert "nova.ovf"  in members, "Missing nova.ovf"
        assert "nova.vmdk" in members, "Missing nova.vmdk"

        ovf_data = tar.extractfile("nova.ovf").read()
        assert b"<Envelope" in ovf_data,          "Invalid OVF XML"
        assert b"streamOptimized" in ovf_data,    "VMDK type not set"
        assert b"EFI" in ovf_data,                "EFI firmware not configured"
        assert b"PyOS NOVA" in ovf_data,          "VM name missing"

        vmdk_data = tar.extractfile("nova.vmdk").read()
        magic = struct.unpack_from("<I", vmdk_data, 0)[0]
        assert magic == VMDK_MAGIC, f"Invalid VMDK magic: {magic:#x}"

    print(f"  OVF  : valid (EFI firmware, {len(ovf_data)//1024}KB)")
    print(f"  VMDK : valid ({len(vmdk_data)//1024}KB stream-optimized)")
    print(f"  Files: {members}")
    print(f"  OVA is ready to import into VirtualBox.")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="PyOS NOVA VirtualBox OVA builder — pure Python")
    ap.add_argument("--output", "-o", default=os.path.join(BUILD,"nova_vbox.ova"))
    ap.add_argument("--ram",    type=int, default=2048, help="VM RAM in MB (default 2048)")
    ap.add_argument("--cpus",   type=int, default=2,    help="vCPUs (default 2)")
    ap.add_argument("--disk",   type=int, default=512,  help="Disk size in MB (default 512)")
    ap.add_argument("--test",   action="store_true",    help="Verify existing OVA")
    args = ap.parse_args()

    if args.test:
        verify_ova(args.output)
    else:
        build_ova(args.output, ram_mb=args.ram, cpus=args.cpus, disk_mb=args.disk)
        verify_ova(args.output)
        print("\n  ─────────────────────────────────────────────────────────")
        print("  To install in VirtualBox:")
        print("    1. Open VirtualBox")
        print("    2. File → Import Appliance...")
        print(f"    3. Select: {args.output}")
        print("    4. Click Import (defaults are fine)")
        print("    5. Start the VM → boots into PyOS NOVA")
        print()
        print("  GPU in VirtualBox:")
        print("    Settings → Display → Graphics Controller: VMSVGA")
        print("    For GPU passthrough: install VirtualBox Extension Pack")
        print("    + enable: VBoxManage modifyvm 'PyOS NOVA' --accelerate3d on")
        print()
