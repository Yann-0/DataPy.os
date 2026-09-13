"""
PyOS NOVA — ARM64 EFI Builder
================================
Generates BOOTAA64.EFI (the ARM64 equivalent of BOOTX64.EFI).
Pure Python, no compiler, no tools.

Raspberry Pi 3/4/5 all support UEFI boot via the Raspberry Pi UEFI
firmware project. Our BOOTAA64.EFI is placed in /EFI/BOOT/ and
the firmware loads it directly.

PE32+ format is the same — only the machine type changes:
  x86_64  → 0x8664
  aarch64 → 0xAA64

The machine code stub is AArch64 assembly expressed as Python bytes[].
"""

import os, sys, struct, hashlib, argparse

ROOT  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(ROOT, "build")

# ─────────────────────────────────────────────────────────────────────────────
# AArch64 EFI stub — expressed as Python bytes
#
# UEFI AArch64 calling convention:
#   Entry: X0 = EFI_HANDLE ImageHandle
#          X1 = EFI_SYSTEM_TABLE* SystemTable
#
# EFI_SYSTEM_TABLE offsets (same as x86):
#   +0x40  EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL* ConOut
#
# EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL:
#   +0x08  OutputString(This, String)
#
# AArch64 instructions used (all 4 bytes, little-endian):
#   STP  X29, X30, [SP, #-16]!   — save frame pointer + link register
#   MOV  X29, SP                  — set frame pointer
#   MOV  X19, X0                  — save ImageHandle
#   MOV  X20, X1                  — save SystemTable
#   LDR  X21, [X1, #0x40]        — X21 = ConOut
#   LDR  X22, [X21, #0x08]       — X22 = ConOut->OutputString
#   ADR  X1, boot_message         — X1 = &boot_message (UTF-16LE string)
#   MOV  X0, X21                  — X0 = ConOut (This)
#   BLR  X22                      — call OutputString(ConOut, msg)
#   MOV  X0, #0                   — return EFI_SUCCESS
#   LDP  X29, X30, [SP], #16      — restore
#   RET                            — return to UEFI
# ─────────────────────────────────────────────────────────────────────────────

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
    "  PyOS NOVA — Raspberry Pi Edition\r\n"
    "  UEFI Native Boot | Python PID 1 | ARM64\r\n"
    "\r\n"
    "  Starting Python OS...\r\n"
    "\r\n"
)

def _aarch64_stub(msg_offset: int) -> bytes:
    """
    AArch64 machine code for UEFI EFI app entry point.
    msg_offset: byte offset from end of code to boot message.
    Expressed entirely as Python integer arrays — no assembler needed.
    """
    # Each AArch64 instruction is exactly 4 bytes, little-endian
    """I.

        Args:
        n: N.
        """
    def I(n): return struct.pack("<I", n)

    # STP X29, X30, [SP, #-16]!
    stp_x29_x30 = I(0xA9BF7BFD)
    # MOV X29, SP
    mov_x29_sp  = I(0x910003FD)
    # MOV X19, X0  (save ImageHandle)
    mov_x19_x0  = I(0xAA0003F3)
    # MOV X20, X1  (save SystemTable)
    mov_x20_x1  = I(0xAA0103F4)
    # LDR X21, [X1, #0x40]  (ConOut = SystemTable->ConOut)
    ldr_x21_x1_40 = I(0xF9402035)
    # LDR X22, [X21, #0x08]  (OutputString = ConOut->OutputString)
    ldr_x22_x21_08 = I(0xF9400EB6)

    # ADR X1, #offset  — load address of boot message into X1
    # ADR encodes a PC-relative offset in bits [23:5] (imm19) and [30:29] (imm2lo)
    # offset is from the ADR instruction itself
    # We'll compute the correct offset after measuring stub size
    # Placeholder: encode offset=0 first, fix up later
    adr_placeholder = I(0x10000001)   # ADR X1, #0 (placeholder)

    # MOV X0, X21  (This = ConOut)
    mov_x0_x21  = I(0xAA1503E0)
    # BLR X22  (call OutputString)
    blr_x22     = I(0xD63F02C0)
    # MOV X0, #0  (EFI_SUCCESS)
    mov_x0_0    = I(0xD2800000)
    # LDP X29, X30, [SP], #16
    ldp_x29_x30 = I(0xA8C17BFD)
    # RET
    ret_        = I(0xD65F03C0)

    code = (stp_x29_x30 + mov_x29_sp + mov_x19_x0 + mov_x20_x1 +
            ldr_x21_x1_40 + ldr_x22_x21_08 + adr_placeholder +
            mov_x0_x21 + blr_x22 + mov_x0_0 + ldp_x29_x30 + ret_)

    # Find ADR instruction (offset 6 instructions × 4 bytes = 24)
    adr_pos = 24
    assert code[adr_pos:adr_pos+4] == b"\x01\x00\x00\x10", "ADR placeholder not found"

    # ADR encodes: [31]=0, [30:29]=immlo (2 bits), [28:24]=10000, [23:5]=immhi (19 bits), [4:0]=Rd
    # offset from ADR instruction to target
    target_offset = msg_offset - adr_pos   # relative to start of code for msg_offset from end
    # Actually: ADR X1, label means label - PC (where PC = address of ADR)
    # msg starts at len(code) from start, ADR is at adr_pos from start
    # so offset = len(code) - adr_pos + (we'll add msg_offset from end of code below)
    # Let's recalculate: msg is at code_len + extra (for now 0 since msg immediately follows)
    # offset from ADR instruction = code_len - adr_pos (since msg is right after code)

    code_len        = len(code)
    adr_to_msg      = code_len - adr_pos   # positive offset

    # Encode ADR X1, #adr_to_msg
    # ADR: op=0, immlo[1:0] in bits[30:29], immhi[20:2] in bits[23:5], Rd=1 in bits[4:0]
    imm = adr_to_msg
    immlo = imm & 0x3
    immhi = (imm >> 2) & 0x7FFFF
    adr_enc = (0 << 31) | (immlo << 29) | (0b10000 << 24) | (immhi << 5) | 1
    adr_bytes = struct.pack("<I", adr_enc)

    # Patch the ADR instruction
    code = code[:adr_pos] + adr_bytes + code[adr_pos+4:]
    return code


def _compute_aarch64_stub(message: bytes) -> bytes:
    """Build AArch64 stub + message, with correct ADR offset."""
    stub = _aarch64_stub(0)    # compute with any offset first
    # stub size is fixed (no variable-length instructions)
    # ADR offset = len(stub) - adr_pos (24) = len(stub) - 24
    return stub + message


# ─────────────────────────────────────────────────────────────────────────────
# PE32+ builder (ARM64 variant)
# ─────────────────────────────────────────────────────────────────────────────

FILE_ALIGNMENT    = 0x200
SECTION_ALIGNMENT = 0x1000
IMAGE_BASE        = 0x00400000
ENTRY_RVA         = 0x1000

MACHINE_ARM64     = 0xAA64    # ← only difference from x86_64 (0x8664)
SUBSYSTEM_EFI_APP = 0x000A

"""Align.

    Args:
    v: V.
    a: A.
    """
def _align(v, a): return (v + a - 1) & ~(a - 1)

DOS_STUB = (
    b"MZ"                         # 2  e_magic
    + b"\x90\x00"                 # 4  e_cblp
    + b"\x03\x00"                 # 6  e_cp
    + b"\x00\x00"                 # 8  e_crlc
    + b"\x04\x00"                 # 10 e_cparhdr
    + b"\x00\x00"                 # 12 e_minalloc
    + b"\xFF\xFF"                 # 14 e_maxalloc
    + b"\x00\x00"                 # 16 e_ss
    + b"\xB8\x00"                 # 18 e_sp
    + b"\x00\x00"                 # 20 e_csum
    + b"\x00\x00"                 # 22 e_ip
    + b"\x00\x00"                 # 24 e_cs
    + b"\x40\x00"                 # 26 e_lfarlc
    + b"\x00\x00"                 # 28 e_ovno
    + b"\x00" * 8                 # 36 e_res (4 words)
    + b"\x00\x00"                 # 38 e_oemid
    + b"\x00\x00"                 # 40 e_oeminfo
    + b"\x00" * 20                # 60 e_res2 (10 words)
    + struct.pack("<I", 64)       # 64 e_lfanew = 64
)
assert len(DOS_STUB) == 64, f"DOS stub is {len(DOS_STUB)} bytes"


def build_arm64_efi(text_data: bytes) -> bytes:
    """Assemble complete ARM64 PE32+ EFI binary."""
    raw_text  = text_data
    raw_size  = _align(len(raw_text), FILE_ALIGNMENT)
    n_opt_hdr = 240
    n_sects   = 1
    hdr_size  = _align(64 + 4 + 20 + n_opt_hdr + n_sects * 40, FILE_ALIGNMENT)
    virt_img  = _align(ENTRY_RVA + raw_size, SECTION_ALIGNMENT)

    out = bytearray(DOS_STUB)
    out += b"PE\x00\x00"
    out += struct.pack("<HHIIIHH",
        MACHINE_ARM64, n_sects, 0, 0, 0, n_opt_hdr, 0x0002 | 0x0200)

    opt = bytearray()
    opt += struct.pack("<HBB", 0x020B, 0, 0)          # Magic + linker (4)
    opt += struct.pack("<III", raw_size, 0, 0)         # Code/data (12)
    opt += struct.pack("<II",  ENTRY_RVA, ENTRY_RVA)   # Entry/BaseCode (8)
    opt += struct.pack("<Q",   IMAGE_BASE)              # ImageBase (8)
    opt += struct.pack("<II",  SECTION_ALIGNMENT, FILE_ALIGNMENT)  # (8)
    opt += struct.pack("<HHHH",0,0,0,0)                # OS/Image ver (8)
    opt += struct.pack("<HH",  0,0)                    # Subsys ver (4)
    opt += struct.pack("<I",   0)                      # Win32Ver (4)
    opt += struct.pack("<II",  virt_img, hdr_size)     # SizeImage/Hdrs (8)
    opt += struct.pack("<I",   0)                      # CheckSum (4)
    opt += struct.pack("<H",   SUBSYSTEM_EFI_APP)      # Subsystem (2)
    opt += struct.pack("<H",   0)                      # DllChar (2)
    opt += struct.pack("<QQQQ",0,0,0,0)                # Stack/Heap (32)
    opt += struct.pack("<I",   0)                      # LoaderFlags (4)
    opt += struct.pack("<I",   16)                     # NumRvaAndSizes (4)
    opt += b"\x00" * 128                               # Data dirs (128)
    assert len(opt) == n_opt_hdr, f"opt={len(opt)}"
    out += opt

    # Section table
    raw_ptr = hdr_size
    out += struct.pack("<8sIIIIIIHHI",
        b".text\x00\x00\x00", len(raw_text), ENTRY_RVA, raw_size,
        raw_ptr, 0, 0, 0, 0, 0x60000020)

    # Pad headers
    out += b"\x00" * (hdr_size - len(out))
    # .text section
    out += raw_text + b"\x00" * (raw_size - len(raw_text))
    return bytes(out)


def build_arm64_efi_file(output_path: str, verify: bool = True) -> int:
    """Build and return arm64 efi file.

        Args:
        output_path (str): Output path.
        verify (bool): Verify, defaults to True.


        Returns:
            int: Result.
        """
    text   = _compute_aarch64_stub(BOOT_MESSAGE)
    binary = build_arm64_efi(text)
    with open(output_path, "wb") as f:
        f.write(binary)
    if verify:
        _verify(binary)
    return len(binary)


def _verify(data: bytes):
    """Verify the operation and raise on failure.

        Args:
        data (bytes): Data.
        """
    assert data[:2] == b"MZ"
    pe_off = struct.unpack_from("<I", data, 60)[0]
    assert data[pe_off:pe_off+4] == b"PE\x00\x00"
    machine = struct.unpack_from("<H", data, pe_off+4)[0]
    assert machine == MACHINE_ARM64, f"Wrong machine: {machine:#x}"
    sha = hashlib.sha256(data).hexdigest()[:16]
    print(f"  ARM64 EFI OK — {len(data)} bytes  sha256:{sha}...")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="PyOS NOVA ARM64 EFI builder")
    ap.add_argument("--output", "-o", default=os.path.join(BUILD, "BOOTAA64.EFI"))
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()
    os.makedirs(BUILD, exist_ok=True)
    size = build_arm64_efi_file(args.output, verify=True)
    print(f"  Output: {args.output}  ({size} bytes)")
    print(f"  Place at: /EFI/BOOT/BOOTAA64.EFI on FAT32 partition")
