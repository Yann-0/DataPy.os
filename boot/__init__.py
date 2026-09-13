"""
PyOS NOVA — Boot package
=========================
Everything needed to boot NOVA on real hardware or in a virtual machine.

Modules:
    pyinit         Python as /sbin/init (PID 1).  Mounts /proc, /sys, /dev
                   and transfers control to the NOVA kernel.
    efi_builder    Pure Python PE32+ x86-64 EFI application generator.
                   No C compiler, no assembler required.
    nova_vm        QEMU + OVMF launcher for development and testing.
    uefi_services  Python ctypes wrapper for UEFI Boot Services protocol.
"""
