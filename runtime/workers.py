"""
PyOS NOVA — Multiprocessing Worker Pool
=========================================
Bypass the Python GIL for true parallelism on multi-core hardware.

The GIL causes 4 threads to be 88% slower than sequential for SQLite
writes. Using separate processes eliminates this entirely.

Three worker pools:
  1. StoragePool   — async SOS writes (SQLite, 1 writer per core)
  2. IndexPool     — neural search indexing (CPU-bound embedding)
  3. InferencePool — AI inference (CPU-bound matrix multiplication)

Communication: multiprocessing.Queue with JSON-serialisable messages.
Shared state: none needed — the SOS WAQ handles consistency.

Shell commands:
  workers status   — show all worker pools
  workers scale <n> — resize a pool
  workers bench    — run throughput benchmark
"""

from __future__ import annotations
import os, sys, time, json, queue, multiprocessing as mp
from typing import Optional, Callable, Any, Dict, TYPE_CHECKING
from dataclasses import dataclass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)


@dataclass
class WorkItem:
    """A unit of work sent to a worker process."""
    task_id: str
    fn_name: str
    args:    list
    kwargs:  dict


@dataclass
class WorkResult:
    """Result returned from a worker process."""
    task_id:  str
    result:   Any
    error:    Optional[str]
    duration: float


def _worker_main(db_path: str, work_q: mp.Queue,
                  result_q: mp.Queue, data_dir: str):
    """
    Worker process entry point.

    Receives WorkItems from work_q, executes them,
    puts WorkResults into result_q.

    Args:
        db_path (str): Path to the SQLite database.
        work_q (mp.Queue): Incoming work queue.
        result_q (mp.Queue): Outgoing result queue.
        data_dir (str): NOVA_DATA directory.
    """
    os.environ["NOVA_DATA"] = data_dir
    sys.path.insert(0, ROOT)

    # Each worker gets its own SOS connection
    from store.sos import SemanticObjectStore
    sos = SemanticObjectStore(db_path=db_path)

    HANDLERS: Dict[str, Callable] = {
        "store":  lambda a, kw: sos.store(*a, **kw),
        "write":  lambda a, kw: sos.write(*a, **kw),
        "read":   lambda a, kw: sos.read(*a, **kw),
        "search": lambda a, kw: _handle_search(a, kw, data_dir),
        "embed":  lambda a, kw: _handle_embed(a, kw),
        "infer":  lambda a, kw: _handle_infer(a, kw, data_dir),
    }

    while True:
        try:
            item = work_q.get(timeout=1)
            if item is None:   # shutdown signal
                break
            t = time.perf_counter()
            try:
                fn  = HANDLERS.get(item.fn_name)
                res = fn(item.args, item.kwargs) if fn else None
                err = None
            except Exception as e:
                res = None
                err = str(e)
            result_q.put(WorkResult(
                task_id  = item.task_id,
                result   = res,
                error    = err,
                duration = time.perf_counter() - t,
            ))
        except queue.Empty:
            continue
        except Exception:
            continue


def _handle_search(args, kwargs, data_dir):
    """Execute a search query in a worker process."""
    from store.sos import SemanticObjectStore
    from search.neural import NeuralSearch
    sos = SemanticObjectStore()
    ns  = NeuralSearch(sos)
    return ns.search(*args, **kwargs)


def _handle_embed(args, kwargs):
    """Compute embeddings in a worker process."""
    from search.neural import CharNgramEmbedder
    emb = CharNgramEmbedder()
    return emb.embed(*args, **kwargs)


def _handle_infer(args, kwargs, data_dir):
    """Run AI inference in a worker process."""
    os.environ["NOVA_DATA"] = data_dir
    from ai.engine import AIEngine
    ai = AIEngine()
    return ai.ask(*args, **kwargs)


class WorkerPool:
    """
    A pool of worker processes for a specific task type.

    Provides submit() for fire-and-forget and call() for blocking results.
    """

    def __init__(self, name: str, n_workers: int,
                 db_path: str, data_dir: str):
        """Initialise the worker pool."""
        self.name      = name
        self.n_workers = n_workers
        self.db_path   = db_path
        self.data_dir  = data_dir
        self._work_q   = mp.Queue(maxsize=1000)
        self._result_q = mp.Queue()
        self._procs    = []
        self._pending: dict = {}
        self._started  = False
        self._task_counter = 0

    def start(self):
        """Start all worker processes."""
        if self._started:
            return
        for _ in range(self.n_workers):
            p = mp.Process(
                target = _worker_main,
                args   = (self.db_path, self._work_q,
                           self._result_q, self.data_dir),
                daemon = True,
            )
            p.start()
            self._procs.append(p)
        self._started = True

    def submit(self, fn_name: str, *args, **kwargs) -> str:
        """
        Submit a task asynchronously.

        Args:
            fn_name (str): Handler name (store/write/read/search/embed/infer).
            *args: Positional arguments.
            **kwargs: Keyword arguments.

        Returns:
            str: Task ID for retrieving the result.
        """
        self._task_counter += 1
        task_id = f"{self.name}_{self._task_counter}"
        item    = WorkItem(task_id=task_id, fn_name=fn_name,
                           args=list(args), kwargs=kwargs)
        self._work_q.put(item)
        return task_id

    def call(self, fn_name: str, *args,
             timeout: float = 30.0, **kwargs) -> Any:
        """
        Submit a task and block until the result is available.

        Args:
            fn_name (str): Handler name.
            timeout (float): Maximum wait time in seconds.

        Returns:
            Any: The task result.

        Raises:
            TimeoutError: If the result is not available within timeout.
            RuntimeError: If the worker reported an error.
        """
        task_id = self.submit(fn_name, *args, **kwargs)
        deadline = time.time() + timeout
        # Poll result queue
        while time.time() < deadline:
            try:
                result = self._result_q.get(timeout=0.1)
                if result.task_id == task_id:
                    if result.error:
                        raise RuntimeError(f"Worker error: {result.error}")
                    return result.result
                # Different task — stash it
                self._pending[result.task_id] = result
            except queue.Empty:
                # Check stash
                if task_id in self._pending:
                    r = self._pending.pop(task_id)
                    if r.error:
                        raise RuntimeError(r.error)
                    return r.result
        raise TimeoutError(f"Worker timeout after {timeout}s")

    def stop(self):
        """Send shutdown signals and wait for all workers to exit."""
        for _ in self._procs:
            try:
                self._work_q.put(None, timeout=1)
            except Exception:
                pass
        for p in self._procs:
            p.join(timeout=3)
            if p.is_alive():
                p.terminate()
        self._procs.clear()
        self._started = False

    @property
    def alive(self) -> int:
        """Return number of alive worker processes."""
        return sum(1 for p in self._procs if p.is_alive())

    def status(self) -> dict:
        """Return pool status."""
        return {
            "name":       self.name,
            "workers":    self.n_workers,
            "alive":      self.alive,
            "queue_depth":self._work_q.qsize() if hasattr(self._work_q, "qsize") else -1,
            "started":    self._started,
        }


class ProcessPoolManager:
    """
    Manages all NOVA worker process pools.

    Provides a unified interface for submitting work to the
    appropriate pool based on task type.
    """

    def __init__(self, db_path: str, data_dir: str):
        """Initialise the process pool manager (pools start lazily)."""
        self.db_path  = db_path
        self.data_dir = data_dir
        n_cpu         = max(1, (os.cpu_count() or 2) - 1)
        self._n_storage = min(n_cpu, 4)
        self._n_indexer = min(n_cpu, 2)
        self._n_infer   = min(n_cpu, 2)
        self.storage = self.indexer = self.inference = None

    def start_all(self):
        """Start all worker pools (creates queues on first call)."""
        if self.storage is None:
            self.storage = WorkerPool(
                "storage", self._n_storage, self.db_path, self.data_dir
            )
            self.indexer = WorkerPool(
                "indexer", self._n_indexer, self.db_path, self.data_dir
            )
            self.inference = WorkerPool(
                "inference", self._n_infer, self.db_path, self.data_dir
            )
        for pool in (self.storage, self.indexer, self.inference):
            try:
                pool.start()
            except Exception:
                pass   # graceful — single-process mode if mp unavailable

    def stop_all(self):
        """Stop all worker pools."""
        for pool in (self.storage, self.indexer, self.inference):
            if pool is None:
                continue
            try:
                pool.stop()
            except Exception:
                pass

    def async_write(self, path: str, content: str, **kw) -> str:
        """Submit a write to the storage pool asynchronously."""
        return self.storage.submit("write", path, content, **kw)

    def async_index(self, path: str) -> str:
        """Submit an index job to the indexer pool."""
        return self.indexer.submit("search", path)

    def async_infer(self, prompt: str, **kw) -> str:
        """Submit an inference job to the inference pool."""
        return self.inference.submit("infer", prompt, **kw)

    def status_all(self) -> list:
        """Return status of all pools."""
        return [
            self.storage.status(),
            self.indexer.status(),
            self.inference.status(),
        ]
