"""
PyOS NOVA — Python PID 1
=========================
Python as /sbin/init. Mounts /proc, /sys, /dev; runs the NOVA kernel.
"""

#!/usr/bin/env python3
"""
PyOS NOVA — pyinit.py
Python IS /sbin/init. This is PID 1.

When the Linux kernel finishes loading it executes /sbin/init.
We replace that with THIS file. No systemd, no busybox, no Alpine.
Python owns the machine from this point forward.

Boot sequence:
  kernel → mounts rootfs → executes /sbin/init (= this file)
  → mounts /proc /sys /dev
  → sets up TTY
  → starts PyOS kernel
  → drops into PyOS shell
  → on shell exit, cleanly halts or reboots
"""

import os
import sys
import time
import signal
import subprocess

# ─── ensure our packages are findable ──────────────────────────────────────────
NOVA_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, NOVA_ROOT)

# ─── ANSI ──────────────────────────────────────────────────────────────────────
C = lambda c, t: f"\033[{c}m{t}\033[0m"
GREEN  = lambda t: C("32", t)
CYAN   = lambda t: C("36", t)
DIM    = lambda t: C("2",  t)
RED    = lambda t: C("31", t)
YELLOW = lambda t: C("33", t)

BANNER = """
\033[36m
  ███╗   ██╗ ██████╗ ██╗   ██╗ █████╗
  ████╗  ██║██╔═══██╗██║   ██║██╔══██╗
  ██╔██╗ ██║██║   ██║██║   ██║███████║
  ██║╚██╗██║██║   ██║╚██╗ ██╔╝██╔══██║
  ██║ ╚████║╚██████╔╝ ╚████╔╝ ██║  ██║
  ╚═╝  ╚═══╝ ╚═════╝   ╚═══╝  ╚═╝  ╚═╝
\033[0m
  Python Operating System — NOVA edition
  Python is PID 1. There is no Linux userspace.
"""


class PyInit:
    """
    The Python init system.
    Handles: mount → services → shell → reboot/halt
    """

    def __init__(self):
        """Initialise the instance."""
        self.pid        = os.getpid()   # should be 1
        self.reboot_cmd = None          # set by shell on exit
        self._setup_signals()

    # ────────────────────────────────────── signal handling
    def _setup_signals(self):
        """PID 1 must handle SIGCHLD to reap zombies."""
        signal.signal(signal.SIGCHLD, self._reap_children)
        signal.signal(signal.SIGTERM, self._handle_shutdown)
        signal.signal(signal.SIGINT,  self._handle_shutdown)

    def _reap_children(self, signum, frame):
        """Reap all zombie child processes (PID 1 responsibility)."""
        while True:
            try:
                os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break

    def _handle_shutdown(self, signum, frame):
        """Handle shutdown.

            Args:
            signum: Signum.
            frame: Frame.
            """
        print(CYAN("\n  Shutdown signal received. Halting..."))
        self._halt()

    # ────────────────────────────────────── mount virtual filesystems
    def _mount_vfs(self):
        """Mount the virtual kernel filesystems."""
        mounts = [
            ("proc",    "/proc",    "proc",    ""),
            ("sys",     "/sys",     "sysfs",   ""),
            ("devtmpfs","/dev",     "devtmpfs","mode=0755"),
            ("devpts",  "/dev/pts", "devpts",  "gid=5,mode=0620"),
            ("tmpfs",   "/tmp",     "tmpfs",   "mode=1777"),
            ("tmpfs",   "/run",     "tmpfs",   "mode=0755"),
        ]
        for dev, target, fstype, opts in mounts:
            try:
                os.makedirs(target, exist_ok=True)
                args = ["mount", "-t", fstype]
                if opts:
                    args += ["-o", opts]
                args += [dev, target]
                subprocess.run(args, capture_output=True)
            except Exception:
                pass    # best-effort; some may already be mounted

    # ────────────────────────────────────── hostname
    def _set_hostname(self, name: str = "nova"):
        """Set the hostname.

            Args:
            name (str): Name, defaults to 'nova'.
            """
        try:
            with open("/proc/sys/kernel/hostname", "w") as f:
                f.write(name)
        except Exception:
            pass

    # ────────────────────────────────────── environment
    def _setup_env(self):
        """Set up env."""
        os.environ.update({
            "HOME":    "/root",
            "PATH":    f"{NOVA_ROOT}:/usr/local/bin:/usr/bin:/bin",
            "TERM":    "xterm-256color",
            "SHELL":   "/sbin/init",    # this file
            "USER":    "root",
            "NOVA_ROOT": NOVA_ROOT,
            "NOVA_DATA": "/data/nova",
        })
        os.makedirs("/data/nova", exist_ok=True)
        os.makedirs("/root", exist_ok=True)

    # ────────────────────────────────────── background services
    def _start_services(self):
        """Start any background services needed before the shell."""
        # Ollama (optional - don't block if missing)
        try:
            import shutil
            if shutil.which("ollama"):
                subprocess.Popen(
                    ["ollama", "serve"],
                    stdout=open("/tmp/ollama.log", "w"),
                    stderr=subprocess.STDOUT,
                )
        except Exception:
            pass

    # ────────────────────────────────────── main boot
    def boot(self):
        """Boot the operation and start all subsystems."""
        print(BANNER)
        print(DIM(f"  PID: {self.pid}  |  Python {sys.version.split()[0]}  |  NOVA Kernel\n"))

        steps = [
            ("Mounting virtual filesystems",  self._mount_vfs),
            ("Setting hostname (nova)",       lambda: self._set_hostname("nova")),
            ("Configuring environment",       self._setup_env),
            ("Starting background services",  self._start_services),
        ]
        for name, fn in steps:
            try:
                fn()
                print(f"  {GREEN('[ OK ]')}  {name}")
            except Exception as e:
                print(f"  {RED('[FAIL]')}  {name}: {e}")

        print()

        # Now boot the PyOS kernel and drop into the shell
        try:
            from kernel.nova import NovaKernel
            kernel = NovaKernel()
            kernel.boot()
            # First-boot setup wizard — blocks shell until done
            from apps.setup_wizard import is_first_boot, run_setup
            if is_first_boot():
                print("\n  First boot — launching setup wizard...")
                import time; time.sleep(1)
                run_setup(kernel=kernel)

            self.reboot_cmd = kernel.shell.run()
        except Exception as e:
            print(RED(f"\n  NOVA kernel crash: {e}"))
            import traceback; traceback.print_exc()
            self._emergency_shell()

        self._shutdown()

    # ────────────────────────────────────── shutdown
    def _shutdown(self):
        """Shutdown."""
        if self.reboot_cmd == "reboot":
            self._reboot()
        else:
            self._halt()

    def _halt(self):
        """Halt."""
        print(CYAN("\n  Syncing filesystems..."))
        try:
            subprocess.run(["sync"], timeout=5)
        except Exception:
            pass
        print(CYAN("  System halted. Safe to power off."))
        try:
            import ctypes
            LINUX_REBOOT_CMD_POWER_OFF = 0x4321fedc
            ctypes.CDLL("libc.so.6").reboot(LINUX_REBOOT_CMD_POWER_OFF)
        except Exception:
            os._exit(0)

    def _reboot(self):
        """Reboot."""
        print(CYAN("\n  Rebooting..."))
        try:
            subprocess.run(["sync"], timeout=5)
            import ctypes
            LINUX_REBOOT_CMD_RESTART = 0x1234567
            ctypes.CDLL("libc.so.6").reboot(LINUX_REBOOT_CMD_RESTART)
        except Exception:
            os._exit(0)

    def _emergency_shell(self):
        """Last-resort interactive Python shell on crash."""
        print(RED("\n  *** EMERGENCY PYTHON SHELL ***"))
        print(RED("  NOVA crashed. Dropping into raw Python REPL."))
        print(DIM("  Type exit() to halt the system.\n"))
        import code
        code.interact(local={"os": os, "sys": sys, "NOVA_ROOT": NOVA_ROOT})


# ─── entry ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init = PyInit()
    init.boot()
