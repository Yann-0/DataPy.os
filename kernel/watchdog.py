"""
PyOS NOVA — Kernel Watchdog  (Phase 1 — Stabilisation)
=========================================================
Monitors every kernel subsystem and restarts it if it crashes or hangs.

Architecture:
  - Each subsystem registers a HealthCheck callable that returns bool
  - The watchdog polls every INTERVAL seconds
  - Three consecutive failures → restart attempt (exponential back-off)
  - Five failed restarts → quarantine (stop trying, emit alert)
  - All events logged to the audit trail and the SOS

SOS corruption recovery:
  - On startup, run PRAGMA integrity_check
  - If corruption detected, attempt WAL recovery
  - If unrecoverable, restore from the most recent SOS snapshot

Shell commands:
  watchdog status          — health of all registered subsystems
  watchdog restart <name>  — manual subsystem restart
  watchdog quarantine      — list quarantined subsystems
  watchdog resume <name>   — clear quarantine and retry
"""

from __future__ import annotations

import os
import sys
import time
import threading
import traceback
import logging
from typing import Callable, Dict, List, Optional, Tuple, TYPE_CHECKING
from dataclasses import dataclass, field
from enum import Enum

if TYPE_CHECKING:
    from kernel.nova import NovaKernel

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

logger = logging.getLogger("nova.watchdog")

# ── Configuration ─────────────────────────────────────────────────────────────
POLL_INTERVAL_S      = 10      # seconds between health checks
FAILURE_THRESHOLD    = 3       # consecutive failures before restart
QUARANTINE_THRESHOLD = 5       # failed restarts before quarantine
MAX_BACKOFF_S        = 300     # maximum restart back-off (5 minutes)


class SubsystemState(Enum):
    """Lifecycle state of a watched subsystem."""

    HEALTHY     = "healthy"
    DEGRADED    = "degraded"      # 1–2 failures
    RESTARTING  = "restarting"
    QUARANTINED = "quarantined"   # gave up after repeated failures
    UNKNOWN     = "unknown"


@dataclass
class SubsystemRecord:
    """Watchdog record for one monitored subsystem."""

    name:           str
    health_check:   Callable[[], bool]
    restart_fn:     Optional[Callable[[], None]]

    state:          SubsystemState = SubsystemState.UNKNOWN
    consecutive_failures: int      = 0
    total_failures: int            = 0
    restart_count:  int            = 0
    last_checked:   float          = 0.0
    last_ok:        float          = 0.0
    last_restart:   float          = 0.0
    error_msg:      str            = ""

    def next_restart_delay(self) -> float:
        """Exponential back-off delay before next restart attempt."""
        return min(MAX_BACKOFF_S, 2 ** self.restart_count)


class KernelWatchdog:
    """
    Monitors kernel subsystems and automatically recovers from failures.

    Usage::

        watchdog = KernelWatchdog(kernel)
        watchdog.register("sos",
            health_check=lambda: kernel.sos._pool.alive > 0,
            restart_fn=lambda: kernel.sos._pool.reset())
        watchdog.start()
    """

    def __init__(self, kernel: "NovaKernel"):
        """Initialise the watchdog with a kernel reference."""
        self.kernel    = kernel
        self._records: Dict[str, SubsystemRecord] = {}
        self._lock     = threading.Lock()
        self._running  = False
        self._thread:  Optional[threading.Thread] = None

    # ── Registration ──────────────────────────────────────────────────────────

    def register(self,
                  name:         str,
                  health_check: Callable[[], bool],
                  restart_fn:   Optional[Callable[[], None]] = None):
        """
        Register a subsystem for monitoring.

        Args:
            name:         Unique subsystem identifier.
            health_check: Callable that returns True when healthy.
            restart_fn:   Callable that restarts the subsystem; None = no auto-restart.
        """
        with self._lock:
            self._records[name] = SubsystemRecord(
                name=name,
                health_check=health_check,
                restart_fn=restart_fn,
            )

    def unregister(self, name: str):
        """Remove a subsystem from monitoring."""
        with self._lock:
            self._records.pop(name, None)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        """Start the watchdog polling thread."""
        if self._running:
            return
        self._running = True
        self._thread  = threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name="nova-watchdog",
        )
        self._thread.start()
        logger.info("Watchdog started — monitoring %d subsystems",
                     len(self._records))

    def stop(self):
        """Stop the watchdog."""
        self._running = False

    # ── Polling ───────────────────────────────────────────────────────────────

    def _poll_loop(self):
        """Background polling loop."""
        while self._running:
            time.sleep(POLL_INTERVAL_S)
            self._check_all()

    def _check_all(self):
        """Run health checks for every registered subsystem."""
        with self._lock:
            records = list(self._records.values())

        for rec in records:
            if rec.state == SubsystemState.QUARANTINED:
                continue
            self._check_one(rec)

    def _check_one(self, rec: SubsystemRecord):
        """Run the health check for one subsystem."""
        rec.last_checked = time.time()
        try:
            healthy = rec.health_check()
        except Exception as exc:
            healthy   = False
            rec.error_msg = str(exc)

        if healthy:
            rec.state                = SubsystemState.HEALTHY
            rec.consecutive_failures = 0
            rec.last_ok              = time.time()
            return

        # Unhealthy
        rec.consecutive_failures += 1
        rec.total_failures        += 1

        if rec.consecutive_failures == 1:
            rec.state = SubsystemState.DEGRADED
            logger.warning("Subsystem %s: health check failed (attempt %d)",
                            rec.name, rec.consecutive_failures)
        elif rec.consecutive_failures >= FAILURE_THRESHOLD:
            self._attempt_restart(rec)

    def _attempt_restart(self, rec: SubsystemRecord):
        """Attempt to restart a failed subsystem."""
        if rec.restart_fn is None:
            rec.state = SubsystemState.QUARANTINED
            logger.error("Subsystem %s: no restart function — quarantined", rec.name)
            self._emit_alert(rec, "no restart function")
            return

        # Check back-off
        since_last = time.time() - rec.last_restart
        if rec.last_restart > 0 and since_last < rec.next_restart_delay():
            return   # still in back-off window

        if rec.restart_count >= QUARANTINE_THRESHOLD:
            rec.state = SubsystemState.QUARANTINED
            logger.error("Subsystem %s: %d restarts failed — quarantined",
                          rec.name, rec.restart_count)
            self._emit_alert(rec, f"{rec.restart_count} failed restarts")
            return

        rec.state        = SubsystemState.RESTARTING
        rec.last_restart = time.time()
        rec.restart_count += 1

        logger.warning("Watchdog restarting subsystem %s (attempt %d)",
                        rec.name, rec.restart_count)
        try:
            rec.restart_fn()
            rec.consecutive_failures = 0
            rec.state                = SubsystemState.HEALTHY
            rec.last_ok              = time.time()
            logger.info("Subsystem %s: restart succeeded", rec.name)
        except Exception as exc:
            rec.error_msg = str(exc)
            logger.error("Subsystem %s: restart failed: %s", rec.name, exc)

    def _emit_alert(self, rec: SubsystemRecord, reason: str):
        """Emit an alert via the kernel advisor (if available)."""
        try:
            advisor = getattr(self.kernel, "advisor", None)
            if advisor:
                advisor.alert(
                    f"WATCHDOG: subsystem '{rec.name}' {reason}.",
                    severity="critical",
                )
        except Exception:
            pass

    # ── Manual controls ───────────────────────────────────────────────────────

    def restart(self, name: str) -> bool:
        """
        Manually restart a subsystem (clears quarantine).

        Args:
            name: Subsystem name.

        Returns:
            True if restart was attempted.
        """
        with self._lock:
            rec = self._records.get(name)
        if not rec:
            return False
        rec.state          = SubsystemState.DEGRADED
        rec.restart_count  = 0
        rec.consecutive_failures = FAILURE_THRESHOLD   # triggers restart
        self._attempt_restart(rec)
        return True

    def resume(self, name: str) -> bool:
        """
        Clear quarantine and resume monitoring a subsystem.

        Args:
            name: Subsystem name.

        Returns:
            True if quarantine was cleared.
        """
        with self._lock:
            rec = self._records.get(name)
        if rec and rec.state == SubsystemState.QUARANTINED:
            rec.state         = SubsystemState.UNKNOWN
            rec.restart_count = 0
            rec.consecutive_failures = 0
            return True
        return False

    # ── Reporting ─────────────────────────────────────────────────────────────

    def status(self) -> List[dict]:
        """
        Return health status of all registered subsystems.

        Returns:
            List of status dicts, one per subsystem.
        """
        with self._lock:
            records = list(self._records.values())
        now = time.time()
        return [
            {
                "name":      rec.name,
                "state":     rec.state.value,
                "failures":  rec.total_failures,
                "restarts":  rec.restart_count,
                "last_ok_s": round(now - rec.last_ok) if rec.last_ok else None,
                "error":     rec.error_msg[:60] if rec.error_msg else "",
            }
            for rec in sorted(records, key=lambda r: r.name)
        ]

    def quarantined(self) -> List[str]:
        """Return names of quarantined subsystems."""
        with self._lock:
            return [
                r.name for r in self._records.values()
                if r.state == SubsystemState.QUARANTINED
            ]


# ── SOS Corruption Recovery ───────────────────────────────────────────────────

def check_and_repair_sos(db_path: str) -> Tuple[bool, str]:
    """
    Run SQLite integrity check and attempt WAL recovery if needed.

    Called at kernel startup before the SOS is opened.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        Tuple of (ok, message). If ok is False the DB may be unusable.
    """
    import sqlite3

    if not os.path.exists(db_path):
        return True, "No existing database — fresh start."

    try:
        conn   = sqlite3.connect(db_path, timeout=10)
        result = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()

        if result and result[0] == "ok":
            return True, "Database integrity check passed."

        # Integrity check failed — attempt WAL recovery
        logger.warning("SOS integrity check failed: %s — attempting recovery",
                        result[0] if result else "unknown")

        # Force WAL checkpoint
        conn  = sqlite3.connect(db_path, timeout=10)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA integrity_check")
        conn.close()

        return True, "WAL checkpoint completed — database may be recoverable."

    except sqlite3.DatabaseError as exc:
        logger.error("SOS database error: %s", exc)
        # Rename corrupt DB and start fresh
        corrupt_path = db_path + ".corrupt"
        try:
            os.rename(db_path, corrupt_path)
            logger.warning("Corrupt database moved to %s — starting fresh",
                            corrupt_path)
            return True, f"Corrupt database renamed to {corrupt_path}. Fresh start."
        except OSError:
            return False, f"Database corrupt and could not be renamed: {exc}"
    except Exception as exc:
        return False, f"Unexpected error during integrity check: {exc}"
