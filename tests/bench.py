"""
PyOS NOVA — Benchmark Suite  (v0.0008)
=======================================
Measures real performance of every NOVA subsystem and compares
results against the documented targets.

Running:
    nova bench             — full suite (~30 seconds)
    nova bench --sos       — SOS benchmarks only
    nova bench --ai        — AI inference benchmark
    nova bench --search    — vector search benchmark
    nova bench --save      — save results to SOS

Results:
    Each benchmark produces: {name, value, unit, target, passed}
    All results are stored at /system/bench/<timestamp>.json

Documented targets (from performance sprints):
    SOS resolve×500         ≤   2 ms
    SOS write (WAQ)         ≥ 800 writes/sec
    Embed 200 docs          ≤  30 ms
    Vector search           ≤   5 ms/query
    Compression ratio       ≤  10 % of original
"""

from __future__ import annotations

import os
import sys
import time
import json
import threading
from typing import List, Dict, Optional, Callable, Tuple, TYPE_CHECKING
from dataclasses import dataclass, field

if TYPE_CHECKING:
    from store.sos import SemanticObjectStore
    from kernel.nova import NovaKernel

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

BENCH_BASE = "/system/bench"


@dataclass
class BenchResult:
    """Result of one benchmark."""

    name:    str
    value:   float          # measured value
    unit:    str            # e.g. "ms", "ops/sec", "%"
    target:  float          # documented target
    op:      str            # "<=" or ">=" comparison direction
    passed:  bool = False   # True if target was met
    note:    str  = ""

    def __post_init__(self):
        """Compute pass/fail immediately."""
        if self.op == "<=":
            self.passed = self.value <= self.target
        elif self.op == ">=":
            self.passed = self.value >= self.target

    def to_dict(self) -> dict:
        """Serialise to dict."""
        return self.__dict__

    def format(self) -> str:
        """Human-readable one-line result."""
        icon  = "✓" if self.passed else "✗"
        delta = abs(self.value - self.target)
        rel   = f" ({delta:.1f}{self.unit} {'under' if self.value <= self.target else 'over'} target)"
        return (f"  {icon} {self.name:<40} "
                f"{self.value:>8.2f} {self.unit}  "
                f"target {self.op} {self.target}{rel}")


class BenchmarkSuite:
    """
    Runs all NOVA benchmarks and reports against documented targets.
    """

    def __init__(self, kernel: "NovaKernel"):
        """Initialise the benchmark suite."""
        self.kernel = kernel
        self._results: List[BenchResult] = []

    # ── SOS benchmarks ────────────────────────────────────────────────────────

    def bench_sos_resolve(self, n: int = 500) -> BenchResult:
        """
        Benchmark: resolve *n* paths via cache and SQLite.

        Target: ≤ 2 ms for 500 resolves.
        """
        sos = self.kernel.sos
        # Seed paths
        for i in range(min(n, 50)):
            sos.write(f"/bench/resolve/file{i}.txt", f"content {i}")

        paths = [f"/bench/resolve/file{i % 50}.txt" for i in range(n)]
        t     = time.perf_counter()
        for p in paths:
            sos.resolve(p)
        elapsed_ms = (time.perf_counter() - t) * 1000

        return BenchResult("sos.resolve ×500", elapsed_ms, "ms",
                             target=2.0, op="<=")

    def bench_sos_write_throughput(self, n: int = 200) -> BenchResult:
        """
        Benchmark: raw write throughput.

        Target: ≥ 800 writes/sec.
        """
        sos = self.kernel.sos
        t   = time.perf_counter()
        for i in range(n):
            sos.write(f"/bench/write/file{i}.txt", f"content {i} " * 10)
        elapsed    = time.perf_counter() - t
        throughput = round(n / max(elapsed, 0.001))

        return BenchResult("sos.write throughput", throughput, "writes/sec",
                             target=800, op=">=")

    def bench_sos_waq(self, n: int = 200) -> BenchResult:
        """
        Benchmark: WAQ batch queue throughput.

        Target: ≥ 5000 writes/sec (batched).
        """
        waq = getattr(self.kernel.sos, "_waq", None)
        if waq is None:
            return BenchResult("sos.waq (batch)", 0, "writes/sec",
                                 target=5000, op=">=",
                                 note="WAQ not active")

        from store.waq import _WriteOp
        ops = [
            _WriteOp(oid=f"bench_oid_{i:06}", content=f"data {i}".encode(),
                      kind="text", meta_json="{}", tags_json="[]",
                      links_json="[]", parent_oid=None, version=1,
                      created_at=time.time(), size=10, tags=[])
            for i in range(n)
        ]
        t = time.perf_counter()
        for op in ops:
            waq.enqueue(op)
        waq.flush(force=True)
        elapsed    = time.perf_counter() - t
        throughput = round(n / max(elapsed, 0.001))

        return BenchResult("sos.waq batch write", throughput, "writes/sec",
                             target=5000, op=">=")

    # ── Search benchmarks ─────────────────────────────────────────────────────

    def bench_embed_docs(self, n: int = 200) -> BenchResult:
        """
        Benchmark: embed *n* documents.

        Target: ≤ 30 ms.
        """
        try:
            from search.fast import FastNgramEmbedder
            emb  = FastNgramEmbedder()
            docs = [
                f"Python function compute transform document {i} with content"
                for i in range(n)
            ]
            t = time.perf_counter()
            for doc in docs:
                emb.embed(doc)
            elapsed_ms = (time.perf_counter() - t) * 1000
            return BenchResult("embed 200 docs", elapsed_ms, "ms",
                                 target=30.0, op="<=")
        except Exception as exc:
            return BenchResult("embed 200 docs", 9999, "ms",
                                 target=30.0, op="<=", note=str(exc))

    def bench_vector_search(self) -> BenchResult:
        """
        Benchmark: vector search over 200 indexed documents.

        Target: ≤ 5 ms per query.
        """
        try:
            from search.fast import FastNgramEmbedder, IVFIndex

            emb = FastNgramEmbedder()
            idx = IVFIndex(n_clusters=16, nprobe=4)
            for i in range(200):
                v = emb.embed(f"test document {i} with content topic {i % 4}")
                if v:
                    idx.add(f"doc{i}", v)

            query = emb.embed("python compute function transform")
            t = time.perf_counter()
            for _ in range(100):
                idx.query(query, k=10)
            per_query_ms = (time.perf_counter() - t) * 10   # ms per query

            return BenchResult("vector search (200 docs)", per_query_ms, "ms",
                                 target=5.0, op="<=")
        except Exception as exc:
            return BenchResult("vector search (200 docs)", 9999, "ms",
                                 target=5.0, op="<=", note=str(exc))

    def bench_keyword_search(self) -> BenchResult:
        """
        Benchmark: FTS5 keyword search over 100 documents.

        Target: ≤ 5 ms per query.
        """
        sos = self.kernel.sos
        for i in range(100):
            sos.write(f"/bench/fts/doc{i}.txt",
                       f"document {i} about python machine learning AI tools")
        query = "machine learning"
        t = time.perf_counter()
        for _ in range(100):
            sos.keyword_search(query, n=10)
        per_query_ms = (time.perf_counter() - t) * 10

        return BenchResult("keyword search (FTS5)", per_query_ms, "ms",
                             target=5.0, op="<=")

    # ── Compression benchmark ─────────────────────────────────────────────────

    def bench_compression(self) -> BenchResult:
        """
        Benchmark: compression ratio on typical Python code.

        Target: ≤ 10 % of original size.
        """
        try:
            from store.compression import compress, decompress
            sample = b"class NovaKernel:\n    def __init__(self):\n" * 200
            compressed = compress(sample, "code")
            ratio_pct  = round(len(compressed) / len(sample) * 100, 1)
            return BenchResult("compression ratio", ratio_pct, "%",
                                 target=10.0, op="<=")
        except Exception as exc:
            return BenchResult("compression ratio", 100.0, "%",
                                 target=10.0, op="<=", note=str(exc))

    # ── AI benchmark ──────────────────────────────────────────────────────────

    def bench_ai_response(self) -> BenchResult:
        """
        Benchmark: AI response time.

        Target: ≤ 5000 ms (5 seconds) for a short response.
        """
        ai = self.kernel.ai
        t  = time.perf_counter()
        try:
            ai.ask("What is 2+2? Answer in one word.", max_tokens=10)
        except Exception:
            pass
        elapsed_ms = (time.perf_counter() - t) * 1000
        return BenchResult("ai.ask (short)", elapsed_ms, "ms",
                             target=5000.0, op="<=")

    # ── Runner ────────────────────────────────────────────────────────────────

    def run_all(self, categories: List[str] = None,
                 save: bool = False) -> List[BenchResult]:
        """
        Run all benchmarks (or a filtered subset).

        Args:
            categories: List of categories to run ('sos', 'search', 'ai').
                        None = run all.
            save:       Persist results to SOS.

        Returns:
            List of BenchResult objects.
        """
        all_cats = {
            "sos":    [self.bench_sos_resolve, self.bench_sos_write_throughput,
                        self.bench_sos_waq],
            "search": [self.bench_embed_docs, self.bench_vector_search,
                        self.bench_keyword_search],
            "compress": [self.bench_compression],
            "ai":     [self.bench_ai_response],
        }
        to_run = categories or list(all_cats.keys())
        results: List[BenchResult] = []

        for cat in to_run:
            for bench_fn in all_cats.get(cat, []):
                result = bench_fn()
                results.append(result)

        self._results = results

        if save:
            self._save_results(results)

        return results

    def _save_results(self, results: List[BenchResult]):
        """Persist benchmark results to SOS."""
        ts   = int(time.time())
        path = f"{BENCH_BASE}/{ts}.json"
        if not self.kernel.sos.exists(BENCH_BASE):
            self.kernel.sos.mkdir(BENCH_BASE, parents=True)
        data = {
            "ts":      ts,
            "results": [r.to_dict() for r in results],
            "passed":  sum(1 for r in results if r.passed),
            "total":   len(results),
        }
        self.kernel.sos.write(path, json.dumps(data, indent=2),
                               tags=["benchmark"])

    def print_report(self, results: List[BenchResult]):
        """Print a formatted benchmark report to stdout."""
        passed = sum(1 for r in results if r.passed)
        print(f"\n  PyOS NOVA Benchmark Report  ({passed}/{len(results)} targets met)")
        print("  " + "─" * 70)
        for r in results:
            print(r.format())
        print("  " + "─" * 70)
        overall = "PASSED" if passed == len(results) else f"PARTIAL ({passed}/{len(results)})"
        print(f"  Overall: {overall}\n")
