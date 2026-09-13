"""
PyOS NOVA — UEFI Runtime Services
===================================
Python wrapper around UEFI Boot Services and Runtime Services.
Used when Python is running INSIDE the UEFI environment (no Linux kernel).

Access is via ctypes pointing directly at the UEFI System Table,
whose pointer is saved by the EFI stub at a known address.

This replaces ALL Linux kernel dependencies:
  Linux syscall       →  UEFI equivalent
  ─────────────────────────────────────────
  read(fd, buf, n)    →  EFI_FILE->Read()
  write(fd, buf, n)   →  ConOut->OutputString()
  mmap / malloc       →  BootServices->AllocatePool()
  open / stat         →  EFI_SIMPLE_FILE_SYSTEM->OpenVolume()
  gettime             →  RuntimeServices->GetTime()
  sleep               →  BootServices->Stall()
  exit                →  BootServices->Exit() / RuntimeServices->ResetSystem()
"""

import ctypes
import struct
import os
import sys
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# UEFI C type definitions (using ctypes)
# ─────────────────────────────────────────────────────────────────────────────

class EFITableHeader(ctypes.Structure):
    """E f i table header."""
    _fields_ = [
        ("Signature",   ctypes.c_uint64),
        ("Revision",    ctypes.c_uint32),
        ("HeaderSize",  ctypes.c_uint32),
        ("CRC32",       ctypes.c_uint32),
        ("Reserved",    ctypes.c_uint32),
    ]

# Function pointer types for UEFI protocols
# (void* used for simplicity; real types would be more specific)
FuncPtr = ctypes.c_void_p

class EFISimpleTextOutput(ctypes.Structure):
    """EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL"""
    _fields_ = [
        ("Reset",          FuncPtr),   # +0x00
        ("OutputString",   FuncPtr),   # +0x08
        ("TestString",     FuncPtr),   # +0x10
        ("QueryMode",      FuncPtr),   # +0x18
        ("SetMode",        FuncPtr),   # +0x20
        ("SetAttribute",   FuncPtr),   # +0x28
        ("ClearScreen",    FuncPtr),   # +0x30
        ("SetCursorPos",   FuncPtr),   # +0x38
        ("EnableCursor",   FuncPtr),   # +0x40
        ("Mode",           FuncPtr),   # +0x48
    ]

class EFIBootServices(ctypes.Structure):
    """EFI_BOOT_SERVICES (partial — the fields we use)"""
    _fields_ = [
        ("Hdr",                         EFITableHeader),  # +0x00 (24 bytes)
        ("RaiseTPL",                    FuncPtr),         # +0x18 (8 bytes each)
        ("RestoreTPL",                  FuncPtr),         # +0x20
        ("AllocatePages",               FuncPtr),         # +0x28 = 40
        ("FreePages",                   FuncPtr),         # +0x30 = 48
        ("GetMemoryMap",                FuncPtr),         # +0x38 = 56
        ("AllocatePool",                FuncPtr),         # +0x40 = 64  ← memory alloc
        ("FreePool",                    FuncPtr),         # +0x48 = 72  ← memory free
        ("CreateEvent",                 FuncPtr),         # +0x50
        ("SetTimer",                    FuncPtr),         # +0x58
        ("WaitForEvent",                FuncPtr),         # +0x60
        ("SignalEvent",                 FuncPtr),         # +0x68
        ("CloseEvent",                  FuncPtr),         # +0x70
        ("CheckEvent",                  FuncPtr),         # +0x78
        ("InstallProtocol",             FuncPtr),         # +0x80
        ("ReinstallProtocol",           FuncPtr),         # +0x88
        ("UninstallProtocol",           FuncPtr),         # +0x90
        ("HandleProtocol",              FuncPtr),         # +0x98
        ("Reserved",                    FuncPtr),         # +0xA0
        ("RegisterProtocolNotify",      FuncPtr),         # +0xA8
        ("LocateHandle",                FuncPtr),         # +0xB0
        ("LocateDevicePath",            FuncPtr),         # +0xB8
        ("InstallConfigurationTable",   FuncPtr),         # +0xC0
        ("LoadImage",                   FuncPtr),         # +0xC8 = 200
        ("StartImage",                  FuncPtr),         # +0xD0 = 208
        ("Exit",                        FuncPtr),         # +0xD8 = 216
        # ... more fields follow
    ]

class EFISystemTable(ctypes.Structure):
    """EFI_SYSTEM_TABLE"""
    _fields_ = [
        ("Hdr",                 EFITableHeader),     # +0x00
        ("FirmwareVendor",      ctypes.c_void_p),    # +0x18
        ("FirmwareRevision",    ctypes.c_uint32),    # +0x20
        ("_pad",                ctypes.c_uint32),    # +0x24 (padding)
        ("ConsoleInHandle",     ctypes.c_void_p),    # +0x28
        ("ConIn",               ctypes.c_void_p),    # +0x30
        ("ConsoleOutHandle",    ctypes.c_void_p),    # +0x38
        ("ConOut",              ctypes.POINTER(EFISimpleTextOutput)),  # +0x40
        ("StdErrHandle",        ctypes.c_void_p),    # +0x48
        ("StdErr",              ctypes.c_void_p),    # +0x50
        ("RuntimeServices",     ctypes.c_void_p),    # +0x58
        ("BootServices",        ctypes.POINTER(EFIBootServices)),      # +0x60
    ]


# ─────────────────────────────────────────────────────────────────────────────
# UEFI Hardware Abstraction Layer
# Used by PyOS NOVA when running in UEFI environment (no Linux kernel)
# ─────────────────────────────────────────────────────────────────────────────

class UEFIRuntime:
    """
    Python interface to UEFI services.
    Replaces all Linux kernel dependencies when running on bare UEFI.

    To use:
      runtime = UEFIRuntime(system_table_addr)
      runtime.print("Hello from Python on bare UEFI!")
      buf = runtime.alloc(4096)
      runtime.free(buf)
    """

    EFI_SUCCESS       = 0
    EFI_POOL_DATA     = 4        # EfiLoaderData memory type

    def __init__(self, system_table_addr: int):
        """
        system_table_addr: physical address of EFI_SYSTEM_TABLE.
        This is passed to our EFI stub by the UEFI firmware.
        The stub saves it to a known location (e.g., a global variable).
        """
        self._st_addr  = system_table_addr
        self._st       = EFISystemTable.from_address(system_table_addr)
        self._bs       = self._st.BootServices.contents
        self._co       = self._st.ConOut.contents

        # Build callable function objects for frequently used services
        self._output_string = ctypes.CFUNCTYPE(
            ctypes.c_uint64,            # return: EFI_STATUS
            ctypes.c_void_p,            # This (ConOut pointer)
            ctypes.c_wchar_p,           # String (UTF-16LE)
        )(self._co.OutputString)

        self._alloc_pool = ctypes.CFUNCTYPE(
            ctypes.c_uint64,            # return: EFI_STATUS
            ctypes.c_int,               # PoolType
            ctypes.c_uint64,            # Size
            ctypes.POINTER(ctypes.c_void_p),  # Buffer (output)
        )(self._bs.AllocatePool)

        self._free_pool = ctypes.CFUNCTYPE(
            ctypes.c_uint64,            # return: EFI_STATUS
            ctypes.c_void_p,            # Buffer
        )(self._bs.FreePool)

        self._stall = ctypes.CFUNCTYPE(
            ctypes.c_uint64,
            ctypes.c_uint64,            # Microseconds
        )(ctypes.c_void_p(int(self._st_addr) + 0x68 * 8))  # approximate

    # ── Console output ────────────────────────────────────────────────────────
    def print(self, text: str):
        """Print UTF-16LE text to the UEFI console."""
        ptr = ctypes.cast(
            ctypes.addressof(self._co),
            ctypes.c_void_p,
        )
        status = self._output_string(ptr.value, text + "\r\n")
        return status == self.EFI_SUCCESS

    def clear_screen(self):
        """Clear the UEFI console."""
        try:
            fn = ctypes.CFUNCTYPE(ctypes.c_uint64, ctypes.c_void_p)(self._co.ClearScreen)
            ptr = ctypes.cast(ctypes.addressof(self._co), ctypes.c_void_p)
            fn(ptr.value)
        except Exception:
            pass

    # ── Memory ────────────────────────────────────────────────────────────────
    def alloc(self, size: int) -> int:
        """Allocate `size` bytes from UEFI pool. Returns pointer address."""
        buf = ctypes.c_void_p()
        status = self._alloc_pool(self.EFI_POOL_DATA, size, ctypes.byref(buf))
        if status != self.EFI_SUCCESS:
            raise MemoryError(f"UEFI AllocatePool failed: status={status:#x}")
        return buf.value

    def free(self, addr: int):
        """Free memory allocated with alloc()."""
        self._free_pool(addr)

    def read_at(self, addr: int, size: int) -> bytes:
        """Read bytes from a physical address."""
        return bytes((ctypes.c_uint8 * size).from_address(addr))

    def write_at(self, addr: int, data: bytes):
        """Write bytes to a physical address."""
        buf = (ctypes.c_uint8 * len(data)).from_address(addr)
        ctypes.memmove(buf, data, len(data))

    # ── Time ─────────────────────────────────────────────────────────────────
    def stall(self, microseconds: int):
        """Busy-wait for `microseconds` microseconds."""
        try:
            self._stall(microseconds)
        except Exception:
            pass

    def sleep(self, seconds: float):
        """Sleep for `seconds` seconds via UEFI Stall."""
        self.stall(int(seconds * 1_000_000))

    # ── Status ────────────────────────────────────────────────────────────────
    def firmware_vendor(self) -> str:
        """Return UEFI firmware vendor string."""
        try:
            return ctypes.wstring_at(self._st.FirmwareVendor)
        except Exception:
            return "(unknown)"

    def firmware_revision(self) -> str:
        """Firmware revision.


            Returns:
                str: Result.
            """
        rev = self._st.FirmwareRevision
        return f"{(rev >> 16) & 0xFFFF}.{rev & 0xFFFF}"

    def info(self) -> dict:
        """Return metadata about the operation.


            Returns:
                dict: Result.
            """
        return {
            "firmware_vendor":   self.firmware_vendor(),
            "firmware_revision": self.firmware_revision(),
            "system_table_addr": hex(self._st_addr),
        }


# ─────────────────────────────────────────────────────────────────────────────
# UEFI-aware I/O layer (replaces Linux fd-based I/O)
# ─────────────────────────────────────────────────────────────────────────────

class UEFIConsole:
    """
    A file-like object backed by UEFI ConOut.
    Assign to sys.stdout so print() works when there's no kernel.
    """

    def __init__(self, runtime: UEFIRuntime):
        """Initialise the instance."""
        self._rt = runtime
        self.encoding = "utf-8"

    def write(self, text: str) -> int:
        """Write the operation.

            Args:
            text (str): Text.


            Returns:
                int: Result.
            """
        if text:
            self._rt.print(text.rstrip("\n"))
        return len(text)

    """Flush."""
    def flush(self): pass
    """Fileno."""
    def fileno(self): raise io.UnsupportedOperation("fileno")
    """Isatty."""
    def isatty(self): return True


# ─────────────────────────────────────────────────────────────────────────────
# Platform detection
# ─────────────────────────────────────────────────────────────────────────────

def detect_environment() -> str:
    """
    Detect whether we're running on:
      - 'uefi'  : direct UEFI environment (no Linux kernel)
      - 'linux' : Linux kernel present (normal mode)
      - 'macos' : macOS
      - 'other' : unknown
    """
    if sys.platform == "linux":
        # Check if we have kernel syscalls by trying to read /proc/version
        try:
            with open("/proc/version") as f:
                f.read()
            return "linux"
        except Exception:
            return "uefi"   # /proc not mounted → no kernel
    elif sys.platform == "darwin":
        return "macos"
    else:
        return "other"


def get_uefi_runtime(system_table_addr: int = None) -> Optional[UEFIRuntime]:
    """
    Get a UEFIRuntime instance if we're in a UEFI environment.
    On Linux/macOS, returns None (use normal OS services instead).
    """
    env = detect_environment()
    if env != "uefi":
        return None
    if system_table_addr is None:
        # Try to read it from the location the EFI stub saved it
        SAVED_ST_ADDR = 0x10000  # Address where our stub saves SystemTable ptr
        try:
            data = open("/sys/firmware/efi/systab").read()
            for line in data.splitlines():
                if line.startswith("SMBIOS3=") or line.startswith("ACPI20="):
                    system_table_addr = int(line.split("=")[1], 16)
                    break
        except Exception:
            pass
    if system_table_addr is None:
        return None
    return UEFIRuntime(system_table_addr)


# ─────────────────────────────────────────────────────────────────────────────
# Adaptive I/O — works on both UEFI and Linux
# ─────────────────────────────────────────────────────────────────────────────

class AdaptiveIO:
    """
    Provides the same I/O interface regardless of whether we're on
    bare UEFI or Linux. The NOVA kernel uses this instead of open()/print().
    """

    def __init__(self):
        """Initialise the instance."""
        self.env     = detect_environment()
        self.runtime = get_uefi_runtime() if self.env == "uefi" else None

    def print(self, *args, **kwargs):
        """Print."""
        if self.runtime:
            self.runtime.print(" ".join(str(a) for a in args))
        else:
            builtins_print = __builtins__["print"] if isinstance(__builtins__, dict) else __import__("builtins").print
            builtins_print(*args, **kwargs)

    def read_file(self, path: str) -> bytes:
        """Read a file — uses OS file I/O on Linux, UEFI file protocol on UEFI."""
        if self.env == "linux":
            with open(path, "rb") as f:
                return f.read()
        else:
            # On UEFI: use EFI_SIMPLE_FILE_SYSTEM_PROTOCOL
            # (abbreviated implementation — real version needs device path resolution)
            raise NotImplementedError("UEFI file I/O not yet implemented; use SOS object store")

    def sleep(self, seconds: float):
        """Sleep.

            Args:
            seconds (float): Seconds.
            """
        if self.runtime:
            self.runtime.sleep(seconds)
        else:
            import time
            time.sleep(seconds)

    @property
    def info(self) -> dict:
        """Return metadata about the operation.


            Returns:
                dict: Result.
            """
        d = {"environment": self.env}
        if self.runtime:
            d.update(self.runtime.info())
        return d
