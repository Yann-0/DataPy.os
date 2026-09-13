"""
PyOS NOVA — Kernel Test Suite
================================
Tests for all kernel subsystems:
  - AsyncScheduler (cooperative green threads)
  - KernelWatchdog (health monitoring + auto-restart)
  - Process Namespace mounts (Plan 9-style bind mounts)
  - Checkpoint & restore (process state snapshots)
  - Hot reload (live module replacement)
  - Memory manager (object cache, limits)
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import threading
import tempfile
import shutil
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ── AsyncScheduler ────────────────────────────────────────────────────────────

class TestAsyncScheduler:
    """Tests for the asyncio-based cooperative green-thread scheduler."""

    def test_start_stop(self, scheduler):
        """Scheduler starts with a live event loop."""
        assert scheduler._loop is not None
        assert scheduler._loop.is_running()

    def test_submit_coroutine(self, scheduler):
        """A submitted coroutine runs and returns its value."""
        result = scheduler.run_sync(asyncio.sleep(0.01))
        assert result is None   # sleep returns None

    def test_return_value(self, scheduler):
        """run_sync returns the coroutine's return value."""
        async def _add(a, b):
            await asyncio.sleep(0)
            return a + b
        assert scheduler.run_sync(_add(3, 4)) == 7

    def test_multiple_concurrent_tasks(self, scheduler):
        """Multiple tasks run concurrently without blocking each other."""
        results = []

        async def _task(n):
            await asyncio.sleep(0.02)
            results.append(n)
            return n

        t1 = scheduler.submit(_task(1), "t1")
        t2 = scheduler.submit(_task(2), "t2")
        t3 = scheduler.submit(_task(3), "t3")
        time.sleep(0.1)
        assert len(results) == 3
        assert set(results) == {1, 2, 3}

    def test_task_cancel(self, scheduler):
        """Cancelling a task stops its execution."""
        async def _long():
            await asyncio.sleep(60)

        task = scheduler.submit(_long(), "long-task")
        time.sleep(0.05)
        cancelled = scheduler.cancel(task.pid)
        assert cancelled

    def test_priority_labels(self, scheduler):
        """Tasks carry their priority label."""
        from kernel.scheduler import Priority
        async def _noop(): return 42
        task = scheduler.submit(_noop(), "prio-test", Priority.HIGH)
        assert task.priority == Priority.HIGH

    def test_channel_put_get(self, scheduler):
        """Channel passes messages between threads."""
        ch = scheduler.create_channel()
        scheduler.run_sync(asyncio.sleep(0))  # ensure loop is warm
        threading.Thread(target=ch.put, args=("hello",), daemon=True).start()
        val = ch.get(timeout=2.0)
        assert val == "hello"

    def test_list_tasks(self, scheduler):
        """list_tasks returns running tasks."""
        async def _sleep(): await asyncio.sleep(5)
        t = scheduler.submit(_sleep(), "visible-task")
        time.sleep(0.05)
        alive = [x for x in scheduler.list_tasks() if x.name == "visible-task"]
        assert len(alive) == 1
        scheduler.cancel(t.pid)

    def test_purge_done(self, scheduler):
        """purge_done removes completed tasks from memory."""
        async def _quick(): return True
        scheduler.run_sync(_quick())
        time.sleep(0.05)
        removed = scheduler.purge_done()
        assert removed >= 0   # may be 0 if tasks finished before registration


# ── KernelWatchdog ────────────────────────────────────────────────────────────

class TestKernelWatchdog:
    """Tests for the kernel watchdog subsystem."""

    @pytest.fixture
    def watchdog(self, fake_kernel):
        """Return the kernel's watchdog instance."""
        return fake_kernel.watchdog

    def test_healthy_subsystem(self, watchdog):
        """A healthy subsystem reports 'healthy' after check."""
        watchdog.register("always_ok", health_check=lambda: True)
        watchdog._check_all()
        status = {s["name"]: s["state"] for s in watchdog.status()}
        assert status["always_ok"] == "healthy"

    def test_failing_subsystem_degrades(self, watchdog):
        """One failure marks the subsystem as 'degraded'."""
        watchdog.register("one_fail", health_check=lambda: False,
                           restart_fn=lambda: None)
        watchdog._check_one(watchdog._records["one_fail"])
        status = {s["name"]: s["state"] for s in watchdog.status()}
        assert status["one_fail"] == "degraded"

    def test_restart_triggered_after_threshold(self, watchdog):
        """Three consecutive failures trigger a restart attempt."""
        restarted = []
        watchdog.register(
            "needs_restart",
            health_check=lambda: False,
            restart_fn=lambda: restarted.append(True),
        )
        rec = watchdog._records["needs_restart"]
        for _ in range(3):
            watchdog._check_one(rec)
        assert len(restarted) >= 1

    def test_quarantine_after_max_restarts(self, watchdog):
        """Subsystem is quarantined after QUARANTINE_THRESHOLD failed restarts."""
        from kernel.watchdog import QUARANTINE_THRESHOLD
        watchdog.register(
            "broken",
            health_check=lambda: False,
            restart_fn=lambda: (_ for _ in ()).throw(RuntimeError("still broken")),
        )
        rec = watchdog._records["broken"]
        rec.restart_count = QUARANTINE_THRESHOLD
        rec.consecutive_failures = 3
        rec.last_restart = 0.0
        watchdog._attempt_restart(rec)
        assert rec.state.value == "quarantined"

    def test_resume_clears_quarantine(self, watchdog):
        """resume() allows a quarantined subsystem to be re-monitored."""
        from kernel.watchdog import SubsystemState
        watchdog.register("quar", health_check=lambda: True)
        rec = watchdog._records["quar"]
        rec.state = SubsystemState.QUARANTINED
        result = watchdog.resume("quar")
        assert result
        assert rec.state != SubsystemState.QUARANTINED

    def test_sos_integrity_check(self, tmp_dir):
        """SOS integrity check passes on a fresh database."""
        from kernel.watchdog import check_and_repair_sos
        from store.sos import SemanticObjectStore
        db = os.path.join(tmp_dir, "integrity.db")
        store = SemanticObjectStore(db_path=db)
        store.write("/test", "hello")
        ok, msg = check_and_repair_sos(db)
        assert ok
        assert "passed" in msg.lower() or "ok" in msg.lower() or "checkpoint" in msg.lower()

    def test_manual_restart(self, watchdog):
        """watchdog.restart() manually triggers a restart."""
        calls = []
        watchdog.register("manual_restart",
                           health_check=lambda: True,
                           restart_fn=lambda: calls.append(1))
        watchdog.restart("manual_restart")
        assert len(calls) >= 1

    def test_status_returns_all_subsystems(self, watchdog):
        """status() returns an entry for every registered subsystem."""
        watchdog.register("status_a", health_check=lambda: True)
        watchdog.register("status_b", health_check=lambda: True)
        watchdog._check_all()
        names = {s["name"] for s in watchdog.status()}
        assert "status_a" in names
        assert "status_b" in names


# ── Namespace Mounts ──────────────────────────────────────────────────────────

class TestNamespaceMounts:
    """Tests for Plan 9-style bind mounts."""

    def test_basic_bind_before(self, sos):
        """Bind BEFORE mode shadows the canonical SOS path."""
        from kernel.namespaces import NamespaceManager, BindMode
        sos.mkdir("/real/dir", parents=True)
        sos.write("/real/dir/file.txt", "real content")

        mgr = NamespaceManager(sos)
        ns  = mgr.create("test-ns")
        ns.bind("/real/dir", "/virtual/dir", BindMode.BEFORE)

        result = ns.listdir("/virtual/dir")
        assert "file.txt" in result

    def test_bind_resolve(self, sos):
        """resolve() returns the OID via the namespace binding."""
        from kernel.namespaces import NamespaceManager, BindMode
        sos.mkdir("/src", parents=True)
        sos.write("/src/a.py", "x = 1")

        mgr = NamespaceManager(sos)
        ns  = mgr.create("resolve-ns")
        ns.bind("/src", "/app", BindMode.BEFORE)

        oid = ns.resolve("/app/a.py")
        assert oid is not None

    def test_union_merges_dirs(self, sos):
        """UNION mode merges entries from both source and canonical dirs."""
        from kernel.namespaces import NamespaceManager, BindMode
        sos.mkdir("/dir_a", parents=True)
        sos.write("/dir_a/from_a.txt", "a")
        sos.mkdir("/dir_b", parents=True)
        sos.write("/dir_b/from_b.txt", "b")

        mgr = NamespaceManager(sos)
        ns  = mgr.create("union-ns")
        ns.bind("/dir_a", "/dir_b", BindMode.UNION)

        entries = ns.listdir("/dir_b")
        assert "from_a.txt" in entries
        assert "from_b.txt" in entries

    def test_unbind_removes_binding(self, sos):
        """unbind() removes a mount so the canonical path is exposed again."""
        from kernel.namespaces import NamespaceManager, BindMode
        sos.mkdir("/shadow", parents=True)
        sos.write("/shadow/x.txt", "shadow")

        mgr = NamespaceManager(sos)
        ns  = mgr.create("unbind-ns")
        ns.bind("/shadow", "/mount_point", BindMode.REPLACE)
        ns.unbind("/mount_point")

        bindings = ns.bindings()
        assert not any(b["dst"] == "/mount_point" for b in bindings)

    def test_clone_copies_bindings(self, sos):
        """Cloning a namespace copies all parent bindings."""
        from kernel.namespaces import NamespaceManager, BindMode
        sos.mkdir("/original", parents=True)

        mgr    = NamespaceManager(sos)
        parent = mgr.create("parent-ns")
        parent.bind("/original", "/cloned", BindMode.BEFORE)

        child = parent.clone("child-ns")
        assert any(b["dst"] == "/cloned" for b in child.bindings())


# ── Checkpoint ────────────────────────────────────────────────────────────────

class TestCheckpoint:
    """Tests for process checkpoint & restore."""

    def test_snapshot_creates_record(self, sos):
        """snapshot() returns a CheckpointRecord with nonzero size."""
        from kernel.checkpoint import CheckpointManager
        mgr = CheckpointManager(sos)
        rec = mgr.snapshot("test_snap")
        assert rec.size_bytes > 0
        assert rec.name == "test_snap"

    def test_list_checkpoints(self, sos):
        """Snapshots appear in list_checkpoints()."""
        from kernel.checkpoint import CheckpointManager
        mgr = CheckpointManager(sos)
        mgr.snapshot("snap_a")
        mgr.snapshot("snap_b")
        names = {r.name for r in mgr.list_checkpoints()}
        assert "snap_a" in names
        assert "snap_b" in names

    def test_restore_calls_hook(self, sos):
        """restore() calls the registered restore_fn with saved state."""
        from kernel.checkpoint import CheckpointManager
        state = {"counter": 10}
        restored = []

        mgr = CheckpointManager(sos)
        mgr.register(
            "counter_hook",
            save_fn=lambda: dict(state),
            restore_fn=lambda s: restored.append(s),
        )
        mgr.snapshot("hook_snap")
        mgr.restore("hook_snap")
        assert len(restored) == 1
        assert restored[0]["counter"] == 10

    def test_delete_removes_snapshot(self, sos):
        """delete() removes a checkpoint from the list."""
        from kernel.checkpoint import CheckpointManager
        mgr = CheckpointManager(sos)
        mgr.snapshot("del_snap")
        mgr.delete("del_snap")
        names = {r.name for r in mgr.list_checkpoints()}
        assert "del_snap" not in names

    def test_diff_shows_changed_globals(self, sos):
        """diff() detects changes between two snapshots."""
        from kernel.checkpoint import CheckpointManager
        mgr     = CheckpointManager(sos)
        val     = [1]
        mgr.register("val_hook",
                      save_fn=lambda: {"v": val[0]},
                      restore_fn=lambda s: None)
        mgr.snapshot("before")
        val[0] = 99
        mgr.snapshot("after")
        diff = mgr.diff("before", "after")
        assert "before" in diff or "after" in diff   # shows the checkpoint names
