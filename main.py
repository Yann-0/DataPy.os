"""
PyOS NOVA — Entry Point
========================
``python3 main.py`` starts the full operating system.

This module is deliberately thin: it parses command-line flags, validates
the environment, and hands control to :class:`kernel.nova.NovaKernel`.  All
real work happens inside the kernel and its subsystems.

Supported invocation modes
--------------------------
* **Interactive (default)** – ``python3 main.py``
  Runs the setup wizard on first boot, then drops into the NOVA shell.

* **Daemon mode** – ``python3 main.py --daemon``
  Starts REST API + SSH server; no interactive shell.  Suitable for
  ``systemd`` or Docker.

* **Single command** – ``python3 main.py --cmd "ls /home"``
  Executes one shell command and exits.  Used by CI pipelines.

* **UEFI mode** – called with ``--efi`` by ``boot/pyinit_efi.py``
  Skips TTY setup; UEFI console is already configured.

Environment variables
---------------------
``NOVA_DATA``
    Directory for the SOS SQLite database and configuration.
    Defaults to ``~/.nova``.
``NOVA_SRC``
    Root of the NOVA Python source tree (auto-detected).
``NOVA_MODEL``
    Path to a ``*.gguf`` model file for local LLM inference.
``NOVA_NO_AI``
    Set to ``1`` to disable AI features entirely (faster startup).
``NOVA_LOG``
    Log level: ``debug``, ``info``, ``warning``, ``error`` (default ``info``).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

# ── Version ────────────────────────────────────────────────────────────────────
__version__ = "0.0008"

# ── Logging setup ──────────────────────────────────────────────────────────────
# Configured before any subsystem imports so that early messages are captured.
_LOG_LEVEL = os.environ.get("NOVA_LOG", "info").upper()
logging.basicConfig(
    level    = getattr(logging, _LOG_LEVEL, logging.INFO),
    format   = "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt  = "%H:%M:%S",
)
log = logging.getLogger("nova.main")


def _ensure_python_version() -> None:
    """Abort with a clear message if Python < 3.11 is used.

    NOVA relies on:
    - ``tomllib`` (3.11+) for ``pyproject.toml`` parsing.
    - Match statements used in several modules (3.10+).
    - ``asyncio.TaskGroup`` for concurrent boot tasks (3.11+).
    """
    major, minor = sys.version_info[:2]
    if (major, minor) < (3, 11):
        print(
            f"\033[31m[FAIL]\033[0m Python 3.11+ required "
            f"(found {major}.{minor})\n"
            f"  Install:  sudo apt install python3.11\n"
            f"  Or:       pyenv install 3.12.0",
            file=sys.stderr,
        )
        sys.exit(1)


def _ensure_nova_src() -> Path:
    """Locate the NOVA source directory and add it to ``sys.path``.

    Search order:
    1. ``NOVA_SRC`` environment variable.
    2. Directory containing this file.
    3. ``/nova`` (UEFI EFI partition mount point).

    Returns
    -------
    Path
        Absolute path to the NOVA source root.
    """
    candidates = [
        os.environ.get("NOVA_SRC"),
        str(Path(__file__).resolve().parent),
        "/nova",
        "/workspace",
    ]
    for candidate in candidates:
        if candidate and Path(candidate, "kernel", "nova.py").exists():
            src = Path(candidate).resolve()
            if str(src) not in sys.path:
                sys.path.insert(0, str(src))
            os.environ.setdefault("NOVA_SRC", str(src))
            return src
    print(
        "\033[31m[FAIL]\033[0m Cannot find NOVA source tree.\n"
        "  Set NOVA_SRC=/path/to/nova or run from the source directory.",
        file=sys.stderr,
    )
    sys.exit(1)


def _prepare_data_directory() -> Path:
    """Create and return the NOVA data directory.

    The data directory holds:
    - ``nova.db``       — SOS SQLite database (WAL mode)
    - ``models/``       — downloaded GGUF model files
    - ``adapters/``     — LoRA fine-tuning adapters
    - ``logs/``         — audit trail and access logs
    - ``certs/``        — TLS certificates for SSH/HTTPS

    Returns
    -------
    Path
        Absolute path to the data directory (guaranteed to exist).
    """
    data_dir = Path(
        os.environ.get("NOVA_DATA", Path.home() / ".nova")
    ).resolve()

    # Create subdirectories silently on first run.
    for sub in ("models", "adapters", "logs", "certs"):
        (data_dir / sub).mkdir(parents=True, exist_ok=True)

    os.environ["NOVA_DATA"] = str(data_dir)
    return data_dir


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments.

    Returns
    -------
    argparse.Namespace
        Parsed arguments with the following attributes:

        ``daemon``
            Run as a background service (no interactive shell).
        ``cmd``
            Execute a single shell command then exit.
        ``efi``
            UEFI boot mode — skip TTY / POSIX setup.
        ``no_ai``
            Disable all AI features (overrides ``NOVA_NO_AI``).
        ``version``
            Print version and exit.
    """
    parser = argparse.ArgumentParser(
        prog        = "nova",
        description = "PyOS NOVA — Python operating system",
        formatter_class = argparse.RawDescriptionHelpFormatter,
        epilog = """
Examples:
  python3 main.py                   # interactive shell
  python3 main.py --daemon          # headless daemon
  python3 main.py --cmd "ls /"      # single command
  python3 main.py --no-ai           # disable AI (fast startup)
  python3 main.py --version         # print version
        """,
    )
    parser.add_argument(
        "--daemon", action="store_true",
        help="Run in daemon mode (REST API + SSH; no interactive shell)",
    )
    parser.add_argument(
        "--cmd", metavar="COMMAND",
        help="Execute one shell command and exit",
    )
    parser.add_argument(
        "--efi", action="store_true",
        help="UEFI boot mode (called by boot/pyinit_efi.py)",
    )
    parser.add_argument(
        "--no-ai", dest="no_ai", action="store_true",
        help="Disable AI engine (faster startup, reduced RAM)",
    )
    parser.add_argument(
        "--version", "-V", action="version",
        version=f"PyOS NOVA {__version__}",
    )
    return parser.parse_args()


def main() -> int:
    """Boot PyOS NOVA and start the requested mode.

    This is the top-level entry point.  It:

    1. Validates the Python version.
    2. Locates the NOVA source tree.
    3. Prepares the data directory.
    4. Applies any environment-variable overrides.
    5. Imports and starts ``NovaKernel``.

    Returns
    -------
    int
        Exit code.  0 = success, non-zero = error.
    """
    # ── Pre-flight checks ──────────────────────────────────────────────────────
    _ensure_python_version()
    nova_src = _ensure_nova_src()
    data_dir = _prepare_data_directory()
    args     = _parse_args()

    # Apply flag overrides to environment so subsystems can read them.
    if args.no_ai:
        os.environ["NOVA_NO_AI"] = "1"

    log.info("PyOS NOVA %s  src=%s  data=%s", __version__, nova_src, data_dir)

    # ── Import kernel (delayed so sys.path is set first) ───────────────────────
    try:
        from kernel.nova import NovaKernel
    except ImportError as exc:
        print(
            f"\033[31m[FAIL]\033[0m Cannot import kernel: {exc}\n"
            "  Reinstall NOVA:  pip install -e .",
            file=sys.stderr,
        )
        return 1

    # ── Create and boot the kernel ─────────────────────────────────────────────
    kernel = NovaKernel()

    try:
        if not args.cmd:
            kernel.boot()
    except Exception as exc:           # pragma: no cover
        log.exception("Kernel boot failed: %s", exc)
        return 1

    # ── Run the requested mode ─────────────────────────────────────────────────
    try:
        if args.cmd:
            # Non-interactive: run one command and exit.
            from shell.nova_shell import NovaShell
            shell = NovaShell(kernel)
            result = shell.execute(args.cmd)
            if not result.ok:
                log.error("command failed: %s", result.error or args.cmd)
            return 0 if result.ok else 1

        elif args.daemon:
            log.info("Daemon mode — REST API on 127.0.0.1 (SSH only if requested)")
            host = os.environ.get("NOVA_API_HOST", "127.0.0.1")
            port = int(os.environ.get("NOVA_API_PORT", "0"))
            if port == 0:
                import socket as _sock
                s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
                s.bind((host, 0))
                port = s.getsockname()[1]
                s.close()
            kernel.api_server.host = host
            kernel.api_server.port = port
            url = kernel.api_server.start()
            ready = Path(data_dir) / "daemon.ready"
            ready.write_text(url, encoding="utf-8")
            log.info("daemon ready %s", url)
            import signal
            stop_event = __import__("threading").Event()
            signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
            signal.signal(signal.SIGINT,  lambda *_: stop_event.set())
            stop_event.wait()
            kernel.api_server.stop()
            return 0

        else:
            # Interactive shell — this blocks until the user exits.
            from shell.nova_shell import NovaShell
            shell = NovaShell(kernel)
            shell.run()
            return 0

    except KeyboardInterrupt:
        # Ctrl-C during interactive session — clean exit.
        print("\n  Shutting down NOVA...  ", flush=True)
        return 0
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 0
    finally:
        # Always attempt a clean shutdown so the SOS WAL is flushed.
        try:
            kernel.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
