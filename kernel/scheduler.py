"""
PyOS NOVA — Async Green-Thread Scheduler
==========================================
Asyncio-based cooperative scheduler replacing Python threading.Thread
throughout the kernel. Zero GIL contention, hundreds of concurrent
'processes', predictable scheduling at await points.

Architecture:
  - NovaTask wraps a coroutine as a first-class NOVA process
  - AsyncScheduler owns the event loop, manages task lifecycle
  - Existing daemon threads migrate to async tasks via run_in_executor
  - Priority levels: REALTIME > HIGH > NORMAL > LOW > IDLE

Process model:
  - Each task has a PID, name, priority, and status
  - Tasks communicate via asyncio.Queue (zero-copy IPC)
  - Cooperative yield via await asyncio.sleep(0)
  - Preemptive timeout via asyncio.wait_for

Shell commands:
  tasks             — list all running async tasks
  tasks kill <pid>  — cancel a task
  tasks info <pid>  — show task details
  yield             — yield the scheduler (let other tasks run)
"""

from __future__ import annotations
import asyncio, time, sys, os, threading, functools
from typing import Callable, Coroutine, Optional, Dict, Any, List
from dataclasses import dataclass, field
from enum import IntEnum

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)


class Priority(IntEnum):
    """Task priority levels."""
    IDLE      = 0
    LOW       = 1
    NORMAL    = 2
    HIGH      = 3
    REALTIME  = 4


@dataclass
class NovaTask:
    """A first-class NOVA process backed by an asyncio Task."""

    pid:       int
    name:      str
    priority:  Priority
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    ended_at:   float = 0.0
    status:    str    = "pending"   # pending|running|done|cancelled|failed
    result:    Any    = None
    error:     Optional[str] = None
    _task:     Optional[asyncio.Task] = field(default=None, repr=False)

    @property
    def runtime_ms(self) -> float:
        """Return task runtime in milliseconds."""
        if self.started_at == 0:
            return 0.0
        end = self.ended_at if self.ended_at else time.time()
        return (end - self.started_at) * 1000

    @property
    def is_alive(self) -> bool:
        """Return True if the task is still running."""
        return self.status in ("pending", "running")

    def cancel(self) -> bool:
        """Cancel this task."""
        if self._task and not self._task.done():
            self._task.cancel()
            self.status = "cancelled"
            return True
        future = getattr(self, "_future", None)
        if future is not None and not future.done():
            future.cancel()
            self.status = "cancelled"
            return True
        return False

    def to_dict(self) -> dict:
        """Serialize to dict."""
        return {
            "pid":        self.pid,
            "name":       self.name,
            "priority":   self.priority.name,
            "status":     self.status,
            "runtime_ms": round(self.runtime_ms, 1),
            "error":      self.error,
        }


class AsyncScheduler:
    """
    Cooperative async task scheduler for the NOVA kernel.

    Manages an asyncio event loop in a dedicated thread.
    Other threads submit coroutines via submit() and get
    futures back for coordination.
    """

    def __init__(self):
        """Initialise the async scheduler."""
        self._loop:    Optional[asyncio.AbstractEventLoop] = None
        self._thread:  Optional[threading.Thread]          = None
        self._tasks:   Dict[int, NovaTask]                 = {}
        self._pid_ctr: int                                 = 100
        self._running  = False
        self._lock     = threading.Lock()

    def start(self):
        """Start the scheduler's event loop in a background thread."""
        if self._running:
            return
        self._running = True
        self._thread  = threading.Thread(
            target=self._run_loop, daemon=True, name="nova-scheduler"
        )
        self._thread.start()
        # Wait until loop is ready
        for _ in range(100):
            if self._loop and self._loop.is_running():
                break
            time.sleep(0.01)

    def _run_loop(self):
        """Event loop runner (runs in background thread)."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def stop(self):
        """Stop the scheduler and cancel all tasks."""
        self._running = False
        if self._loop:
            # Cancel all running tasks
            for task in list(self._tasks.values()):
                task.cancel()
            self._loop.call_soon_threadsafe(self._loop.stop)

    def submit(self, coro: Coroutine,
                name: str = "task",
                priority: Priority = Priority.NORMAL) -> NovaTask:
        """
        Submit a coroutine as a new NOVA task.

        Thread-safe: can be called from any thread.

        Args:
            coro (Coroutine): The coroutine to execute.
            name (str): Human-readable task name.
            priority (Priority): Scheduling priority.

        Returns:
            NovaTask: The created task object.
        """
        if not self._loop:
            self.start()

        with self._lock:
            self._pid_ctr += 1
            pid = self._pid_ctr

        nova_task = NovaTask(pid=pid, name=name, priority=priority)
        self._tasks[pid] = nova_task

        async def _wrapper():
            nova_task.status     = "running"
            nova_task.started_at = time.time()
            try:
                nova_task.result = await coro
                nova_task.status = "done"
            except asyncio.CancelledError:
                nova_task.status = "cancelled"
                raise
            except Exception as e:
                nova_task.status = "failed"
                nova_task.error  = str(e)
            finally:
                nova_task.ended_at = time.time()

        future = asyncio.run_coroutine_threadsafe(_wrapper(), self._loop)
        nova_task._task = None  # asyncio.Task created inside loop
        # Store future reference
        nova_task._future = future
        return nova_task

    def run_sync(self, coro: Coroutine, timeout: float = 30.0) -> Any:
        """
        Submit a coroutine and block until it completes.

        Args:
            coro (Coroutine): The coroutine to run.
            timeout (float): Maximum wait time.

        Returns:
            Any: The coroutine's return value.

        Raises:
            TimeoutError: If the coroutine does not complete within timeout.
        """
        if not self._loop:
            self.start()
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def run_in_executor(self, fn: Callable, *args,
                         name: str = "executor",
                         priority: Priority = Priority.LOW) -> NovaTask:
        """
        Run a blocking function in a thread pool without blocking the event loop.

        Args:
            fn (Callable): The blocking function to run.
            *args: Arguments to pass to fn.

        Returns:
            NovaTask: The wrapper task.
        """
        async def _exec():
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, fn, *args)
        return self.submit(_exec(), name=name, priority=priority)

    def create_channel(self, maxsize: int = 0) -> "Channel":
        """
        Create an async message channel between tasks.

        Args:
            maxsize (int): Maximum queue depth (0 = unlimited).

        Returns:
            Channel: A bidirectional async message channel.
        """
        return Channel(maxsize, self._loop)

    # ── task management ────────────────────────────────────────────────────

    def cancel(self, pid: int) -> bool:
        """Cancel a task by PID."""
        task = self._tasks.get(pid)
        return task.cancel() if task else False

    def list_tasks(self, include_done: bool = False) -> List[NovaTask]:
        """Return all tasks, optionally including completed ones."""
        tasks = list(self._tasks.values())
        if not include_done:
            tasks = [t for t in tasks if t.is_alive]
        return sorted(tasks, key=lambda t: t.pid)

    def get(self, pid: int) -> Optional[NovaTask]:
        """Return a task by PID."""
        return self._tasks.get(pid)

    def purge_done(self) -> int:
        """Remove completed tasks from memory. Returns count removed."""
        done_pids = [pid for pid, t in self._tasks.items()
                     if not t.is_alive]
        for pid in done_pids:
            del self._tasks[pid]
        return len(done_pids)

    def stats(self) -> dict:
        """Return scheduler statistics."""
        tasks = list(self._tasks.values())
        return {
            "running":   sum(1 for t in tasks if t.status == "running"),
            "pending":   sum(1 for t in tasks if t.status == "pending"),
            "done":      sum(1 for t in tasks if t.status == "done"),
            "failed":    sum(1 for t in tasks if t.status == "failed"),
            "cancelled": sum(1 for t in tasks if t.status == "cancelled"),
            "loop_running": self._loop is not None and self._loop.is_running(),
        }


class Channel:
    """
    Thread-safe async message channel for inter-task communication.
    Wraps asyncio.Queue with thread-safe put/get methods.
    """

    def __init__(self, maxsize: int = 0,
                  loop: Optional[asyncio.AbstractEventLoop] = None):
        """Initialise the channel."""
        self._loop    = loop
        self._maxsize = maxsize
        self._queue:  Optional[asyncio.Queue] = None

    def _ensure_queue(self):
        """Create the queue lazily in the event loop's context."""
        if self._queue is None and self._loop:
            async def _make():
                self._queue = asyncio.Queue(maxsize=self._maxsize)
            asyncio.run_coroutine_threadsafe(_make(), self._loop).result(1.0)

    def put(self, item: Any, timeout: float = 5.0):
        """Put an item into the channel (thread-safe)."""
        self._ensure_queue()
        if self._loop and self._queue:
            asyncio.run_coroutine_threadsafe(
                self._queue.put(item), self._loop
            ).result(timeout)

    def get(self, timeout: float = 5.0) -> Any:
        """Get an item from the channel (thread-safe)."""
        self._ensure_queue()
        if self._loop and self._queue:
            return asyncio.run_coroutine_threadsafe(
                self._queue.get(), self._loop
            ).result(timeout)
        raise TimeoutError("Channel not ready")

    async def aput(self, item: Any):
        """Async put (use inside coroutines)."""
        self._ensure_queue()
        await self._queue.put(item)

    async def aget(self) -> Any:
        """Async get (use inside coroutines)."""
        self._ensure_queue()
        return await self._queue.get()

    @property
    def depth(self) -> int:
        """Return current queue depth."""
        return self._queue.qsize() if self._queue else 0


# ── async helper decorators ──────────────────────────────────────────────────

def nova_task(name: str = None, priority: Priority = Priority.NORMAL):
    """
    Decorator that submits an async function to the global scheduler.

    Usage:
        @nova_task("my-worker", Priority.LOW)
        async def my_worker():
            while True:
                await asyncio.sleep(1)
                ...
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            from kernel.nova import _scheduler
            coro = fn(*args, **kwargs)
            return _scheduler.submit(coro,
                                      name=name or fn.__name__,
                                      priority=priority)
        return wrapper
    return decorator


async def sleep(seconds: float):
    """Cooperative sleep — yields to other tasks."""
    await asyncio.sleep(seconds)


async def yield_cpu():
    """Yield the CPU to other runnable tasks (one scheduler tick)."""
    await asyncio.sleep(0)
