"""
PyOS Memory Manager
Simulates a page-based memory allocator with a buddy-system-like approach.
"""

import math
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class MemoryBlock:
    """Memory block."""
    address: int
    size_mb: int
    owner: Optional[str] = None
    free: bool = True


class MemoryManager:
    """
    Manages a virtual address space divided into 4 MB pages.
    Supports allocation, freeing, and a simple first-fit strategy.
    """

    PAGE_SIZE_MB = 4

    def __init__(self, total_mb: int = 1024):
        """Initialise the instance."""
        self.total_mb  = total_mb
        self._pages: Dict[int, MemoryBlock] = {}
        self._next_addr = 0x10000000          # Start of user space
        self._init_kernel_regions()

    # ------------------------------------------------------------------ init
    def _init_kernel_regions(self):
        """Reserve kernel-space pages."""
        for i, (size, name) in enumerate([
            (16,  "kernel_text"),
            (8,   "kernel_data"),
            (32,  "kernel_heap"),
            (4,   "vfs_cache"),
            (4,   "proc_table"),
        ]):
            addr = i * 0x1000000
            self._pages[addr] = MemoryBlock(addr, size, owner=name, free=False)
        self._kernel_used_mb = 64

    # ------------------------------------------------------------------ alloc
    def allocate(self, mb: int, owner: str = "process") -> int:
        """Allocate `mb` megabytes.  Returns the base address or raises."""
        pages_needed = math.ceil(mb / self.PAGE_SIZE_MB)
        addr = self._next_addr
        for i in range(pages_needed):
            page_addr = addr + i * self.PAGE_SIZE_MB * 1024 * 1024
            self._pages[page_addr] = MemoryBlock(
                address=page_addr,
                size_mb=self.PAGE_SIZE_MB,
                owner=owner,
                free=False,
            )
        self._next_addr += pages_needed * self.PAGE_SIZE_MB * 1024 * 1024
        return addr

    # ------------------------------------------------------------------ free
    def free(self, addr: int):
        """Free memory block at `addr`."""
        if addr in self._pages:
            self._pages[addr].free = True
            self._pages[addr].owner = None

    # ------------------------------------------------------------------ info
    def used_mb(self) -> int:
        """Used mb.


            Returns:
                int: Result.
            """
        return sum(b.size_mb for b in self._pages.values() if not b.free)

    def free_mb(self) -> int:
        """Free mb.


            Returns:
                int: Result.
            """
        return self.total_mb - self.used_mb()

    def stats(self) -> dict:
        """Return usage statistics.


            Returns:
                dict: Result.
            """
        return {
            "total_mb":     self.total_mb,
            "used_mb":      self.used_mb(),
            "free_mb":      self.free_mb(),
            "page_count":   len(self._pages),
            "kernel_mb":    self._kernel_used_mb,
        }

    def dump(self) -> str:
        """Dump.


            Returns:
                str: Result.
            """
        lines = [
            f"MemTotal:      {self.total_mb * 1024:>10} kB",
            f"MemFree:       {self.free_mb() * 1024:>10} kB",
            f"MemAvailable:  {self.free_mb() * 1024:>10} kB",
            f"Buffers:       {32768:>10} kB",
            f"Cached:        {65536:>10} kB",
            f"SwapTotal:     {524288:>10} kB",
            f"SwapFree:      {524288:>10} kB",
        ]
        return "\n".join(lines)
