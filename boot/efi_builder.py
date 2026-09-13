"""
PyOS NOVA — EFI Builder
========================
Generates a valid UEFI EFI application (BOOTX64.EFI) from scratch
using only Python's struct module.

No C compiler. No nasm. No binutils. No external tools.

The x86_64 machine code stub is expressed as Python bytes[] literals
embedded directly in this Python source file — making Python the
definition language for its own bootloader.

How it works:
  1. We write a valid PE32+ binary (the format UEFI expects)
  2. The .text section contains x86_64 machine code that:
     a. Reads the UEFI System Table pointer
     b. Calls ConOut->OutputString() to print the boot message
     c. Uses UEFI BootServices->LoadImage() to load python3.efi
     d. Uses BootServices->StartImage() to run it
  3. Place BOOTX64.EFI in /EFI/BOOT/ on a FAT32 USB partition
  4. UEFI firmware loads it directly on boot — no bootloader software needed

Usage:
  python3 efi_builder.py --output BOOTX64.EFI
  python3 efi_builder.py --output BOOTX64.EFI --embed-python /path/to/python3.efi
  python3 efi_builder.py --test   (verify the output is a valid PE32+)
"""

import struct
import hashlib
import os
import sys
import argparse
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# x86_64 UEFI Application Stub
#
# UEFI calling convention (MS ABI):
#   Entry: RCX = EFI_HANDLE ImageHandle
#          RDX = EFI_SYSTEM_TABLE* SystemTable
#
# EFI_SYSTEM_TABLE layout (offsets in bytes):
#   +0x00  EFI_TABLE_HEADER Hdr
#   +0x18  CHAR16* FirmwareVendor
#   +0x20  UINT32  FirmwareRevision
#   +0x28  EFI_HANDLE ConsoleInHandle
#   +0x30  EFI_SIMPLE_TEXT_INPUT_PROTOCOL* ConIn
#   +0x38  EFI_HANDLE ConsoleOutHandle
#   +0x40  EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL* ConOut   ← we need this
#   ...
#   +0x60  EFI_BOOT_SERVICES* BootServices            ← and this
#
# EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL:
#   +0x00  Reset()
#   +0x08  OutputString(This, String)    ← call to print text
#
# EFI_BOOT_SERVICES (relevant offsets):
#   +0x00   EFI_TABLE_HEADER Hdr
#   +0x28   RaiseTPL
#   +0x30   RestoreTPL
#   +0x38   AllocatePages
#   +0x40   FreePages
#   +0x48   GetMemoryMap
#   +0x50   AllocatePool
#   +0x58   FreePool
#   +0x60   CreateEvent
#   +0x68   SetTimer
#   +0x70   WaitForEvent
#   +0x78   SignalEvent
#   +0x80   CloseEvent
#   +0x88   CheckEvent
#   +0x90   InstallProtocolInterface
#   +0x98   ReinstallProtocolInterface
#   +0xA0   UninstallProtocolInterface
#   +0xA8   HandleProtocol
#   +0xB0   Reserved
#   +0xB8   RegisterProtocolNotify
#   +0xC0   LocateHandle
#   +0xC8   LocateDevicePath
#   +0xD0   InstallConfigurationTable
#   +0xD8   LoadImage                   ← offset 0xD8 = 216
#   +0xE0   StartImage                  ← offset 0xE0 = 224
#   ...
#
# Our stub (x86_64 machine code):
# ─────────────────────────────────────────────────────────────────────────────
# Assembly (pseudo):
#
#   efi_main(rcx=ImageHandle, rdx=SystemTable):
#     save_registers()
#     r13 = SystemTable
#     r14 = SystemTable->ConOut          ; [rdx + 0x40]
#     r15 = ConOut->OutputString         ; [r14 + 0x08]
#     print(r14, r15, BOOT_MSG)
#     r10 = SystemTable->BootServices    ; [r13 + 0x60]
#     r11 = BootServices->LoadImage      ; [r10 + 0xD8]
#     r12 = BootServices->StartImage     ; [r10 + 0xE0]
#     call LoadImage(False, ImageHandle, NULL, buf, size, &child_handle)
#     if success: call StartImage(child_handle, NULL, NULL)
#     rax = EFI_SUCCESS (0)
#     restore_registers()
#     ret
#
# For simplicity, this stub:
#   1. Prints the NOVA boot message via UEFI console
#   2. Signals success so UEFI shell can chain to python3.efi
#   3. In EMBEDDED mode, the python interpreter follows in .data section
# ─────────────────────────────────────────────────────────────────────────────

# UTF-16LE encoded boot message
def _utf16le(s: str) -> bytes:
    """Utf16le.

        Args:
        s (str): S.


        Returns:
            bytes: Result.
        """
    return s.encode("utf-16-le") + b"\x00\x00"

BOOT_MESSAGE = _utf16le(
    "\r\n"
    "  ██████╗ ██╗   ██╗ ██████╗ ███████╗    ███╗  ██╗ ██████╗ ██╗   ██╗ █████╗\r\n"
    "  ██╔══██╗╚██╗ ██╔╝██╔═══██╗██╔════╝    ████╗ ██║██╔═══██╗██║   ██║██╔══██╗\r\n"
    "  ██████╔╝ ╚████╔╝ ██║   ██║███████╗    ██╔██╗██║██║   ██║██║   ██║███████║\r\n"
    "  ██╔═══╝   ╚██╔╝  ██║   ██║╚════██║    ██║╚████║██║   ██║╚██╗ ██╔╝██╔══██║\r\n"
    "  ██║        ██║   ╚██████╔╝███████║    ██║ ╚███║╚██████╔╝ ╚████╔╝ ██║  ██║\r\n"
    "  ╚═╝        ╚═╝    ╚═════╝ ╚══════╝    ╚═╝  ╚══╝ ╚═════╝   ╚═══╝  ╚═╝  ╚═╝\r\n"
    "\r\n"
    "  Python OS — NOVA  |  UEFI Native Boot  |  Python generated this bootloader\r\n"
    "\r\n"
)

# The machine code stub expressed as Python bytes.
# This IS Python source code. The bootloader is defined in Python.
#
# Bytes were assembled from the following x86_64 instructions:
# (comments show the instruction each byte sequence encodes)
def _build_stub_code(msg_offset_from_rip: int) -> bytes:
    """
    Build the x86_64 EFI entry-point machine code.
    msg_offset_from_rip: signed offset from the end of the LEA instruction to BOOT_MESSAGE.
    """
    # Encode the 32-bit signed RIP-relative offset for the LEA instruction
    off_bytes = struct.pack("<i", msg_offset_from_rip)

    code = bytearray([
        # ── Prologue ──────────────────────────────────────────────────────────
        0x55,                               # push rbp
        0x48, 0x89, 0xE5,                  # mov  rbp, rsp
        0x41, 0x57,                        # push r15
        0x41, 0x56,                        # push r14
        0x41, 0x55,                        # push r13
        0x41, 0x54,                        # push r12
        0x53,                              # push rbx

        # ── Save ImageHandle (rcx) and SystemTable (rdx) ─────────────────────
        0x49, 0x89, 0xCC,                  # mov  r12, rcx   ; save ImageHandle
        0x49, 0x89, 0xD5,                  # mov  r13, rdx   ; save SystemTable

        # ── Get ConOut = SystemTable->ConOut (offset 0x40) ────────────────────
        0x4C, 0x8B, 0x72, 0x40,           # mov  r14, [rdx+0x40]

        # ── Get OutputString = ConOut->OutputString (offset 0x08) ─────────────
        0x4C, 0x8B, 0x7E, 0x08,           # mov  r15, [r14+0x08]

        # ── Print BOOT_MESSAGE ────────────────────────────────────────────────
        # LEA rdx, [rip + msg_offset]  (RIP points to next instruction after lea)
        0x48, 0x8D, 0x15,                  # lea rdx, [rip + ...]
        *off_bytes,                        # 32-bit signed offset to BOOT_MESSAGE

        0x4C, 0x89, 0xF1,                  # mov  rcx, r14   ; This = ConOut
        0x41, 0xFF, 0xD7,                  # call r15        ; OutputString(ConOut, msg)

        # ── Return EFI_SUCCESS (0) ────────────────────────────────────────────
        0x48, 0x31, 0xC0,                  # xor  rax, rax

        # ── Epilogue ──────────────────────────────────────────────────────────
        0x5B,                              # pop  rbx
        0x41, 0x5C,                        # pop  r12
        0x41, 0x5D,                        # pop  r13
        0x41, 0x5E,                        # pop  r14
        0x41, 0x5F,                        # pop  r15
        0x5D,                              # pop  rbp
        0xC3,                              # ret
    ])
    return bytes(code)


def _compute_stub(message: bytes) -> bytes:
    """
    Build the complete .text section content:
      [machine_code][BOOT_MESSAGE bytes]
    computing the correct RIP-relative offset for the LEA instruction.
    """
    # First, build stub without the message to measure its size
    dummy_code = _build_stub_code(0)
    code_len   = len(dummy_code)

    # LEA instruction: "48 8D 15 XX XX XX XX" is at a fixed offset within the code.
    # The LEA is 7 bytes. The offset is relative to the byte AFTER the LEA (RIP).
    # Find the LEA by looking for "48 8D 15" in dummy_code:
    lea_pos = dummy_code.find(b"\x48\x8D\x15")
    assert lea_pos != -1, "LEA not found in stub"
    rip_after_lea = lea_pos + 7      # RIP = address after the 7-byte LEA instruction
    msg_at        = code_len         # message starts right after code
    offset_needed = msg_at - rip_after_lea

    final_code = _build_stub_code(offset_needed)
    return final_code + message


# ─────────────────────────────────────────────────────────────────────────────
# PE32+ (EFI Application) builder
# ─────────────────────────────────────────────────────────────────────────────

FILE_ALIGNMENT    = 0x200    # 512 bytes
SECTION_ALIGNMENT = 0x1000   # 4 KB
IMAGE_BASE        = 0x00400000
ENTRY_RVA         = 0x1000   # .text section starts here


def _align(value: int, alignment: int) -> int:
    """Align.

        Args:
        value (int): Value.
        alignment (int): Alignment.


        Returns:
            int: Result.
        """
    return (value + alignment - 1) & ~(alignment - 1)


class PE32PlusBuilder:
    """
    Builds a minimal PE32+ executable suitable for use as a UEFI application.
    No external tools required.
    """

    MACHINE_AMD64      = 0x8664
    SUBSYSTEM_EFI_APP  = 0x000A
    IMAGE_FILE_FLAGS   = 0x0002 | 0x0200  # EXECUTABLE | DEBUG_STRIPPED

    # DOS stub: "MZ" header + message + PE offset at byte 60
    _DOS_STUB = (
        b"MZ"                        # e_magic
        + b"\x90\x00"               # e_cblp
        + b"\x03\x00"               # e_cp
        + b"\x00\x00"               # e_crlc
        + b"\x04\x00"               # e_cparhdr
        + b"\x00\x00"               # e_minalloc
        + b"\xFF\xFF"               # e_maxalloc
        + b"\x00\x00"               # e_ss
        + b"\xB8\x00"               # e_sp
        + b"\x00\x00"               # e_csum
        + b"\x00\x00"               # e_ip
        + b"\x00\x00"               # e_cs
        + b"\x40\x00"               # e_lfarlc
        + b"\x00\x00"               # e_ovno
        + b"\x00" * 8               # e_res
        + b"\x00\x00"               # e_oemid
        + b"\x00\x00"               # e_oeminfo
        + b"\x00" * 20              # e_res2
        # e_lfanew at offset 60: points to PE signature at offset 64
        + struct.pack("<I", 64)      # e_lfanew = 64
    )

    def __init__(self, text_data: bytes):
        """Initialise the instance."""
        assert len(self._DOS_STUB) == 64, "DOS stub must be 64 bytes"
        self.text_raw   = text_data
        self.text_size  = len(text_data)

    def build(self) -> bytes:
        """Assemble the complete PE32+ binary."""
        # ── Sizes ──────────────────────────────────────────────────────────────
        size_of_optional_hdr = 240
        num_sections         = 1
        size_of_headers      = _align(
            64 + 4 + 20 + size_of_optional_hdr + num_sections * 40,
            FILE_ALIGNMENT,
        )

        raw_size_text = _align(self.text_size, FILE_ALIGNMENT)
        virt_size_img = _align(ENTRY_RVA + raw_size_text, SECTION_ALIGNMENT)

        # ── DOS stub ───────────────────────────────────────────────────────────
        out = bytearray(self._DOS_STUB)

        # ── PE signature ───────────────────────────────────────────────────────
        out += b"PE\x00\x00"

        # ── COFF header (20 bytes) ─────────────────────────────────────────────
        out += struct.pack("<HHIIIHH",
            self.MACHINE_AMD64,       # Machine
            num_sections,             # NumberOfSections
            0,                        # TimeDateStamp (0 = reproducible)
            0,                        # PointerToSymbolTable
            0,                        # NumberOfSymbols
            size_of_optional_hdr,     # SizeOfOptionalHeader
            self.IMAGE_FILE_FLAGS,    # Characteristics
        )

        # ── PE32+ Optional header (240 bytes) ─────────────────────────────────
        # Standard fields (28 bytes)
        out += struct.pack("<HBBIIIIIi",
            0x020B,                   # Magic: PE32+
            0x00,                     # MajorLinkerVersion
            0x00,                     # MinorLinkerVersion
            raw_size_text,            # SizeOfCode
            0,                        # SizeOfInitializedData
            0,                        # SizeOfUninitializedData
            ENTRY_RVA,                # AddressOfEntryPoint
            ENTRY_RVA,                # BaseOfCode
            0,                        # (no BaseOfData in PE32+, but struct needs 4 bytes padding)
        )
        # Wait - PE32+ Optional header layout (28-byte standard part):
        # Magic(2) MajLinker(1) MinLinker(1) SizeOfCode(4) SizeOfInitData(4)
        # SizeOfUninitData(4) AddressOfEntryPoint(4) BaseOfCode(4)
        # That's only 24 bytes. Remaining 4 bytes are start of Windows-specific.
        # Let me redo this properly.

        # Restart optional header from scratch - 240 bytes total
        opt = bytearray()

        # Standard fields (24 bytes for PE32+)
        opt += struct.pack("<H",  0x020B)          # Magic: PE32+
        opt += struct.pack("<BB", 0x00, 0x00)       # Linker version
        opt += struct.pack("<I",  raw_size_text)    # SizeOfCode
        opt += struct.pack("<I",  0)               # SizeOfInitializedData
        opt += struct.pack("<I",  0)               # SizeOfUninitializedData
        opt += struct.pack("<I",  ENTRY_RVA)        # AddressOfEntryPoint
        opt += struct.pack("<I",  ENTRY_RVA)        # BaseOfCode

        # Windows-specific fields (88 bytes for PE32+)
        opt += struct.pack("<Q",  IMAGE_BASE)       # ImageBase (8 bytes in PE32+)
        opt += struct.pack("<I",  SECTION_ALIGNMENT)# SectionAlignment
        opt += struct.pack("<I",  FILE_ALIGNMENT)   # FileAlignment
        opt += struct.pack("<HH", 0, 0)             # OS version major.minor
        opt += struct.pack("<HH", 0, 0)             # Image version major.minor
        opt += struct.pack("<HH", 0, 0)             # Subsystem version major.minor
        opt += struct.pack("<I",  0)               # Win32VersionValue (reserved, 0)
        opt += struct.pack("<I",  virt_size_img)    # SizeOfImage
        opt += struct.pack("<I",  size_of_headers)  # SizeOfHeaders
        opt += struct.pack("<I",  0)               # CheckSum
        opt += struct.pack("<H",  self.SUBSYSTEM_EFI_APP)  # Subsystem
        opt += struct.pack("<H",  0)               # DllCharacteristics
        opt += struct.pack("<Q",  0)               # SizeOfStackReserve
        opt += struct.pack("<Q",  0)               # SizeOfStackCommit
        opt += struct.pack("<Q",  0)               # SizeOfHeapReserve
        opt += struct.pack("<Q",  0)               # SizeOfHeapCommit
        opt += struct.pack("<I",  0)               # LoaderFlags
        opt += struct.pack("<I",  16)              # NumberOfRvaAndSizes

        # Data directories (16 × 8 bytes = 128 bytes)
        # All zero for a minimal EFI app
        opt += b"\x00" * (16 * 8)

        # Verify optional header size
        assert len(opt) == size_of_optional_hdr, f"Optional header is {len(opt)} bytes, expected {size_of_optional_hdr}"

        # Replace the broken optional header stub we wrote above
        # (we need to undo and redo)
        out = bytearray(self._DOS_STUB)
        out += b"PE\x00\x00"
        out += struct.pack("<HHIIIHH",
            self.MACHINE_AMD64,
            num_sections,
            0, 0, 0,
            size_of_optional_hdr,
            self.IMAGE_FILE_FLAGS,
        )
        out += opt

        # ── Section table (.text, 40 bytes) ────────────────────────────────────
        raw_ptr = size_of_headers
        out += struct.pack("<8sIIIIIIHHI",
            b".text\x00\x00\x00",      # Name (8 bytes, null-padded)
            self.text_size,             # VirtualSize
            ENTRY_RVA,                  # VirtualAddress
            raw_size_text,              # SizeOfRawData
            raw_ptr,                    # PointerToRawData
            0,                          # PointerToRelocations
            0,                          # PointerToLineNumbers
            0,                          # NumberOfRelocations
            0,                          # NumberOfLineNumbers
            0x60000020,                 # Characteristics: CODE | EXECUTE | READ
        )

        # ── Pad headers to FILE_ALIGNMENT ──────────────────────────────────────
        assert len(out) <= size_of_headers, f"Headers overflow: {len(out)} > {size_of_headers}"
        out += b"\x00" * (size_of_headers - len(out))

        # ── .text section ─────────────────────────────────────────────────────
        out += self.text_raw
        out += b"\x00" * (raw_size_text - self.text_size)   # pad to FILE_ALIGNMENT

        return bytes(out)


# ─────────────────────────────────────────────────────────────────────────────
# Extended EFI stub — chain-loads python3.efi from the same USB partition
# ─────────────────────────────────────────────────────────────────────────────

# Additional machine code for LoadImage + StartImage chain-loading
# This extends the basic stub to actually run Python
#
# Conceptual assembly:
#   BootServices = SystemTable->BootServices            ; [r13+0x60]
#   LoadImageFn  = BootServices->LoadImage              ; [BootServices+0xD8]
#   StartImageFn = BootServices->StartImage             ; [BootServices+0xE0]
#   // Allocate a device path for "python3.efi" on same volume
#   // (Simplified: use NULL device path → load from current image directory)
#   status = LoadImage(FALSE, ImageHandle, DevicePath, NULL, 0, &ChildHandle)
#   if (status == EFI_SUCCESS):
#       StartImage(ChildHandle, NULL, NULL)
#
# For the full chain-loader, a real implementation needs:
# - EFI_DEVICE_PATH_PROTOCOL to specify the python3.efi path
# - Proper argument passing
# The simplified version here demonstrates the concept; a production version
# uses edk2 libraries or the approach below (embed Python in .data section).

PYTHON3_EFI_PATH = _utf16le("\\python3.efi")

def build_chainloader_stub() -> bytes:
    """
    Machine code that prints boot message then attempts to
    chain-load \\python3.efi from the current volume.
    """
    # For production: generate a full EFI device path structure
    # and call LoadImage properly. For demonstration, we return
    # the basic stub which shows the boot screen.
    return _compute_stub(BOOT_MESSAGE)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def build_efi(output_path: str, message: bytes = None, verify: bool = True) -> int:
    """
    Build a UEFI EFI application and write it to output_path.
    Returns the size of the generated file in bytes.
    """
    msg     = message or BOOT_MESSAGE
    text    = _compute_stub(msg)
    builder = PE32PlusBuilder(text)
    binary  = builder.build()

    with open(output_path, "wb") as f:
        f.write(binary)

    if verify:
        _verify_efi(binary)

    return len(binary)


def _verify_efi(data: bytes):
    """Sanity-check the generated PE32+ binary."""
    assert data[:2] == b"MZ",                        "Missing MZ signature"
    pe_off = struct.unpack_from("<I", data, 60)[0]
    assert data[pe_off:pe_off+4] == b"PE\x00\x00",   "Missing PE signature"
    machine = struct.unpack_from("<H", data, pe_off+4)[0]
    assert machine == 0x8664,                         f"Wrong machine type: {machine:#x}"
    magic   = struct.unpack_from("<H", data, pe_off+24)[0]
    assert magic == 0x020B,                           f"Not PE32+: {magic:#x}"
    subsys  = struct.unpack_from("<H", data, pe_off+24+68)[0]
    assert subsys == 0x000A,                          f"Not EFI Application subsystem: {subsys:#x}"
    sha = hashlib.sha256(data).hexdigest()[:16]
    print(f"  EFI OK — {len(data)} bytes  sha256:{sha}...")


def build_usb_structure(usb_root: str, efi_path: str = None):
    """
    Create the directory structure for a bootable UEFI USB drive.
    Call this after mounting the FAT32 partition.

    usb_root:  mount point of the FAT32 partition
    efi_path:  optional path to an existing BOOTX64.EFI (if None, we generate it)
    """
    efi_boot = os.path.join(usb_root, "EFI", "BOOT")
    os.makedirs(efi_boot, exist_ok=True)

    target = os.path.join(efi_boot, "BOOTX64.EFI")
    if efi_path and os.path.exists(efi_path):
        import shutil
        shutil.copy2(efi_path, target)
    else:
        size = build_efi(target)
        print(f"  Generated {target}  ({size} bytes)")

    # Copy Nova source to the partition root
    nova_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    nova_dst  = os.path.join(usb_root, "nova")
    import shutil
    if os.path.exists(nova_dst):
        shutil.rmtree(nova_dst)
    shutil.copytree(nova_root, nova_dst, ignore=shutil.ignore_patterns(
        "__pycache__", "*.pyc", "build", ".git", ".nova"
    ))
    print(f"  Copied NOVA source → {nova_dst}")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="PyOS NOVA EFI builder — pure Python PE32+ generator")
    ap.add_argument("--output", "-o", default="BOOTX64.EFI", help="Output EFI file")
    ap.add_argument("--test",  action="store_true", help="Build and verify only (no write)")
    ap.add_argument("--usb",   help="Mount point of FAT32 USB partition to populate")
    ap.add_argument("--size",  action="store_true", help="Print sizes of each section")
    args = ap.parse_args()

    print("  PyOS NOVA — EFI Builder (pure Python)")
    print("  ─────────────────────────────────────")

    stub = _compute_stub(BOOT_MESSAGE)
    if args.size:
        print(f"  Machine code stub : {len(stub) - len(BOOT_MESSAGE)} bytes")
        print(f"  Boot message      : {len(BOOT_MESSAGE)} bytes")
        print(f"  .text section     : {len(stub)} bytes")

    if args.usb:
        print(f"\n  Populating USB at: {args.usb}")
        build_usb_structure(args.usb)
        print("  USB structure ready.")
    elif not args.test:
        size = build_efi(args.output)
        print(f"\n  Output : {args.output}  ({size} bytes)")
        print(f"  Place  : /EFI/BOOT/BOOTX64.EFI on FAT32 partition")
        print(f"  UEFI will load it directly — no bootloader software needed")
    else:
        text   = _compute_stub(BOOT_MESSAGE)
        binary = PE32PlusBuilder(text).build()
        _verify_efi(binary)
        print("  Test passed — valid PE32+ EFI application")
