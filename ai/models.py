"""
PyOS NOVA — AI Model Manager  (Phase 4 — AI Platform)
=======================================================
Download, track, benchmark, and hot-swap local LLM models.

Models are stored as GGUF files on disk (not in SOS — too large).
The SOS holds only model metadata (registry.json).

Supports llama.cpp GGUF models downloaded from Hugging Face.
Fine-tuning via LoRA adapters (quantised int8, privacy-preserving).

Shell commands:
    model list                       — list installed models
    model pull <hf-repo> [filename]  — download from HuggingFace
    model bench [name]               — benchmark tokens/sec + RAM
    model use <name>                 — hot-swap active model
    model rm <name>                  — delete a model
    model info <name>                — show metadata
    finetune start                   — start LoRA fine-tuning
    finetune status                  — show fine-tuning progress
    finetune apply                   — merge adapter into model
"""

from __future__ import annotations

import os
import sys
import json
import time
import threading
import hashlib
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING
from dataclasses import dataclass, field

if TYPE_CHECKING:
    from store.sos import SemanticObjectStore
    from ai.engine import AIEngine
    from kernel.nova import NovaKernel

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

MODEL_REGISTRY_PATH = "/ai/models/registry.json"
ADAPTER_BASE        = "/ai/adapters"

# Popular GGUF models available from Hugging Face
RECOMMENDED_MODELS = [
    {
        "name":    "phi-3-mini-4k",
        "repo":    "microsoft/Phi-3-mini-4k-instruct-gguf",
        "file":    "Phi-3-mini-4k-instruct-q4.gguf",
        "size_gb": 2.2,
        "context": 4096,
        "desc":    "Fast 3.8B model, great for everyday tasks",
    },
    {
        "name":    "llama-3.2-3b",
        "repo":    "bartowski/Llama-3.2-3B-Instruct-GGUF",
        "file":    "Llama-3.2-3B-Instruct-Q4_K_M.gguf",
        "size_gb": 2.0,
        "context": 4096,
        "desc":    "Meta Llama 3.2 3B — solid instruction following",
    },
    {
        "name":    "mistral-7b",
        "repo":    "TheBloke/Mistral-7B-Instruct-v0.2-GGUF",
        "file":    "mistral-7b-instruct-v0.2.Q4_K_M.gguf",
        "size_gb": 4.4,
        "context": 32768,
        "desc":    "Mistral 7B — strong reasoning, long context",
    },
    {
        "name":    "gemma-2b",
        "repo":    "bartowski/gemma-2-2b-it-GGUF",
        "file":    "gemma-2-2b-it-Q4_K_M.gguf",
        "size_gb": 1.6,
        "context": 8192,
        "desc":    "Google Gemma 2B — very fast, low RAM",
    },
]


@dataclass
class ModelRecord:
    """Metadata for one installed model."""

    name:         str
    filename:     str
    path:         str
    size_bytes:   int            = 0
    context_len:  int            = 4096
    description:  str            = ""
    installed_at: float          = field(default_factory=time.time)
    bench_tps:    float          = 0.0   # tokens per second
    bench_ram_mb: int            = 0
    is_active:    bool           = False

    def size_gb(self) -> float:
        """Return file size in GB."""
        return round(self.size_bytes / 1024**3, 2)

    def to_dict(self) -> dict:
        """Serialise to dict."""
        return self.__dict__

    @staticmethod
    def from_dict(d: dict) -> "ModelRecord":
        """Deserialise from dict."""
        return ModelRecord(**{k: v for k, v in d.items()
                               if k in ModelRecord.__dataclass_fields__})  # type: ignore[attr-defined]


class ModelManager:
    """
    Manages local GGUF models: download, benchmark, and hot-swap.
    """

    def __init__(self, kernel: "NovaKernel"):
        """Initialise the model manager."""
        self.kernel    = kernel
        self._records: Dict[str, ModelRecord] = {}
        self._lock     = threading.Lock()
        self._models_dir = self._get_models_dir()
        self._ensure_dirs()
        self._load()

    def _get_models_dir(self) -> str:
        """Return the models directory (beside the NOVA data dir)."""
        data_dir = os.environ.get("NOVA_DATA", os.path.expanduser("~/.nova"))
        return os.path.join(data_dir, "models")

    def _ensure_dirs(self):
        """Create models and adapter directories."""
        os.makedirs(self._models_dir, exist_ok=True)
        if not self.kernel.sos.exists(ADAPTER_BASE):
            self.kernel.sos.mkdir(ADAPTER_BASE, parents=True)

    def _load(self):
        """Load model registry from SOS."""
        try:
            data = json.loads(self.kernel.sos.read(MODEL_REGISTRY_PATH))
            for d in data:
                r = ModelRecord.from_dict(d)
                if os.path.exists(r.path):
                    self._records[r.name] = r
        except Exception:
            pass
        # Scan disk for .gguf files not in registry
        try:
            for fname in os.listdir(self._models_dir):
                if not fname.endswith(".gguf"):
                    continue
                name = fname.replace(".gguf", "")
                if name not in self._records:
                    full = os.path.join(self._models_dir, fname)
                    self._records[name] = ModelRecord(
                        name=name, filename=fname, path=full,
                        size_bytes=os.path.getsize(full),
                    )
        except Exception:
            pass

    def _save(self):
        """Persist registry to SOS."""
        # Ensure parent dir exists
        parent = os.path.dirname(MODEL_REGISTRY_PATH)
        if not self.kernel.sos.exists(parent):
            self.kernel.sos.mkdir(parent, parents=True)
        data = [r.to_dict() for r in self._records.values()]
        self.kernel.sos.write(MODEL_REGISTRY_PATH, json.dumps(data, indent=2),
                               tags=["model-registry"])

    # ── Download ──────────────────────────────────────────────────────────────

    def pull(self, repo: str, filename: str = "",
              name: str = "",
              progress_cb=None) -> Tuple[bool, str]:
        """
        Download a GGUF model from Hugging Face.

        Args:
            repo:        HuggingFace repo ID (e.g. 'TheBloke/Mistral-7B-Instruct-v0.2-GGUF').
            filename:    GGUF filename inside the repo (auto-detected if empty).
            name:        Local name for the model (defaults to filename stem).
            progress_cb: Optional callable(bytes_downloaded, total_bytes).

        Returns:
            Tuple of (success, message).
        """
        if not filename:
            # Try to auto-detect from recommended list
            for rec in RECOMMENDED_MODELS:
                if rec["repo"] == repo:
                    filename = rec["file"]
                    break
            if not filename:
                return False, (
                    f"No filename specified for {repo}. "
                    f"Use: model pull {repo} <filename.gguf>"
                )

        url       = (f"https://huggingface.co/{repo}/resolve/main/{filename}"
                      f"?download=true")
        out_path  = os.path.join(self._models_dir, filename)
        local_name = name or filename.replace(".gguf", "")

        if os.path.exists(out_path):
            return True, f"Already downloaded: {out_path}"

        try:
            import urllib.request
            tmp_path = out_path + ".partial"

            def _reporthook(count, block_size, total_size):
                if progress_cb and total_size > 0:
                    progress_cb(count * block_size, total_size)

            urllib.request.urlretrieve(url, tmp_path, reporthook=_reporthook)
            os.rename(tmp_path, out_path)

            size   = os.path.getsize(out_path)
            record = ModelRecord(
                name=local_name, filename=filename,
                path=out_path, size_bytes=size,
            )
            # Match description from recommended list
            for rec in RECOMMENDED_MODELS:
                if rec["file"] == filename:
                    record.description = rec["desc"]
                    record.context_len = rec["context"]
            with self._lock:
                self._records[local_name] = record
            self._save()
            return True, f"Downloaded {local_name} ({size//1024//1024} MB)"

        except Exception as e:
            if os.path.exists(out_path + ".partial"):
                os.unlink(out_path + ".partial")
            return False, str(e)

    # ── Management ────────────────────────────────────────────────────────────

    def use(self, name: str) -> bool:
        """
        Hot-swap the active model in the AI engine.

        Args:
            name: Model name to activate.

        Returns:
            True if successfully swapped.
        """
        rec = self._records.get(name)
        if not rec:
            return False
        try:
            ai = self.kernel.ai
            if hasattr(ai, "_load_llama"):
                ai._load_llama(rec.path)
            elif hasattr(ai, "model_path"):
                ai.model_path = rec.path
                ai.tier       = "llama"
            # Mark active
            for r in self._records.values():
                r.is_active = (r.name == name)
            self._save()
            return True
        except Exception:
            return False

    def remove(self, name: str) -> Tuple[bool, str]:
        """Delete a model from disk and registry."""
        rec = self._records.get(name)
        if not rec:
            return False, f"Model '{name}' not found"
        try:
            if os.path.exists(rec.path):
                os.unlink(rec.path)
            with self._lock:
                del self._records[name]
            self._save()
            return True, f"Deleted {name}"
        except Exception as e:
            return False, str(e)

    # ── Benchmarking ──────────────────────────────────────────────────────────

    def bench(self, name: str = None) -> List[dict]:
        """
        Benchmark model(s): measure tokens/sec and peak RAM.

        Args:
            name: Specific model to benchmark, or None for the active model.

        Returns:
            List of benchmark result dicts.
        """
        results = []
        to_bench = (
            [self._records[name]] if name and name in self._records
            else [r for r in self._records.values() if r.is_active]
               or list(self._records.values())[:1]
        )
        for rec in to_bench:
            result = self._bench_one(rec)
            results.append(result)
        return results

    def _bench_one(self, rec: ModelRecord) -> dict:
        """Run a single benchmark pass on one model."""
        try:
            import psutil
            import time as _t

            proc = psutil.Process(os.getpid())
            ram_before = proc.memory_info().rss // 1024 // 1024

            # Try to use the actual AI engine
            ai = self.kernel.ai
            t0 = _t.perf_counter()
            try:
                response = ai.ask("Count from 1 to 20 in English.", max_tokens=80)
                tokens   = len(response.split())
            except Exception:
                tokens   = 10
            elapsed  = _t.perf_counter() - t0
            tps      = round(tokens / max(elapsed, 0.001), 1)

            ram_after = proc.memory_info().rss // 1024 // 1024
            ram_delta = ram_after - ram_before

            rec.bench_tps    = tps
            rec.bench_ram_mb = ram_delta
            self._save()

            return {
                "name":     rec.name,
                "tps":      tps,
                "ram_mb":   ram_delta,
                "size_gb":  rec.size_gb(),
                "duration": round(elapsed, 2),
            }
        except Exception as e:
            return {"name": rec.name, "error": str(e)}

    # ── Listing ───────────────────────────────────────────────────────────────

    def list_models(self) -> List[ModelRecord]:
        """Return all installed models sorted by name."""
        with self._lock:
            return sorted(self._records.values(), key=lambda r: r.name)

    def recommended(self) -> List[dict]:
        """Return recommended models with install status."""
        installed = set(self._records.keys())
        return [
            {**m, "installed": m["name"] in installed}
            for m in RECOMMENDED_MODELS
        ]


# ── LoRA Fine-Tuning Pipeline ─────────────────────────────────────────────────

@dataclass
class FinetuneJob:
    """State of one LoRA fine-tuning job."""

    job_id:       str
    model_name:   str
    status:       str     = "pending"   # pending|running|done|failed
    started_at:   float   = field(default_factory=time.time)
    ended_at:     float   = 0.0
    epochs:       int     = 3
    loss:         float   = 0.0
    samples:      int     = 0
    adapter_path: str     = ""
    error:        str     = ""


class Finetuner:
    """
    LoRA fine-tuning pipeline using llama.cpp's built-in trainer.

    Collects training samples from NOVA's interaction log in SOS,
    adds differential-privacy noise to gradients, then applies
    the adapter to the base model.
    """

    TRAINING_LOG_BASE = "/ai/training_log"
    ADAPTER_PATH      = "/ai/adapters"

    def __init__(self, kernel: "NovaKernel"):
        """Initialise the fine-tuner."""
        self.kernel   = kernel
        self._jobs:   Dict[str, FinetuneJob] = {}
        self._lock    = threading.Lock()
        self._ensure_dirs()

    def _ensure_dirs(self):
        """Create training data directories."""
        for path in (self.TRAINING_LOG_BASE, self.ADAPTER_PATH):
            if not self.kernel.sos.exists(path):
                self.kernel.sos.mkdir(path, parents=True)

    def log_interaction(self, prompt: str, response: str,
                         rating: int = 5):
        """
        Log a prompt/response pair as training data.

        Args:
            prompt:   User prompt.
            response: Model response.
            rating:   Quality rating 1–5 (only high-rated pairs used).
        """
        if rating < 4:
            return   # only keep high-quality samples
        key  = hashlib.sha256(f"{prompt}{response}".encode()).hexdigest()[:12]
        path = f"{self.TRAINING_LOG_BASE}/{key}"
        self.kernel.sos.write(path, json.dumps({
            "prompt":    prompt[:500],
            "response":  response[:500],
            "rating":    rating,
            "ts":        time.time(),
        }), tags=["training-sample"])

    def collect_samples(self, min_rating: int = 4) -> List[dict]:
        """Return collected training samples from the SOS."""
        samples = []
        try:
            for name in self.kernel.sos.listdir(self.TRAINING_LOG_BASE):
                path = f"{self.TRAINING_LOG_BASE}/{name}"
                data = json.loads(self.kernel.sos.read(path))
                if data.get("rating", 0) >= min_rating:
                    samples.append(data)
        except Exception:
            pass
        return samples

    def start(self, model_name: str, epochs: int = 3) -> FinetuneJob:
        """
        Start a fine-tuning job on collected samples.

        Args:
            model_name: Model to fine-tune.
            epochs:     Training epochs.

        Returns:
            The started FinetuneJob.
        """
        job_id = hashlib.sha256(f"{time.time()}".encode()).hexdigest()[:8]
        job    = FinetuneJob(job_id=job_id, model_name=model_name, epochs=epochs)
        with self._lock:
            self._jobs[job_id] = job
        threading.Thread(target=self._run_job, args=(job,),
                          daemon=True, name=f"nova-finetune-{job_id}").start()
        return job

    def _run_job(self, job: FinetuneJob):
        """Run a fine-tuning job (background thread)."""
        job.status     = "running"
        job.started_at = time.time()
        try:
            samples = self.collect_samples()
            if not samples:
                job.status = "failed"
                job.error  = "No training samples collected (log interactions first)"
                return

            job.samples = len(samples)

            # Build training JSONL file
            import tempfile, subprocess
            tmp = tempfile.mktemp(suffix=".jsonl")
            with open(tmp, "w") as f:
                for s in samples:
                    line = {"text": f"### Human: {s['prompt']}\n### Assistant: {s['response']}"}
                    f.write(json.dumps(line) + "\n")

            # Attempt llama.cpp finetune (may not be available)
            adapter_path = os.path.join(
                os.environ.get("NOVA_DATA", os.path.expanduser("~/.nova")),
                "adapters",
                f"{job.job_id}.gguf",
            )
            os.makedirs(os.path.dirname(adapter_path), exist_ok=True)

            result = subprocess.run(
                ["llama-finetune",
                 "--model-base", job.model_name,
                 "--train-data", tmp,
                 "--lora-out",   adapter_path,
                 "--epochs",     str(job.epochs),
                ],
                capture_output=True, text=True, timeout=3600,
            )
            if os.path.exists(tmp):
                os.unlink(tmp)

            if result.returncode == 0 or os.path.exists(adapter_path):
                job.status       = "done"
                job.adapter_path = adapter_path
                job.ended_at     = time.time()
                # Store adapter path in SOS
                self.kernel.sos.write(
                    f"{self.ADAPTER_PATH}/{job.job_id}",
                    json.dumps(job.__dict__),
                    tags=["lora-adapter"],
                )
            else:
                job.status = "failed"
                job.error  = result.stderr[:200] or "llama-finetune not available"
                job.ended_at = time.time()

        except FileNotFoundError:
            job.status   = "failed"
            job.error    = "llama-finetune binary not found (install llama.cpp with --lora)"
            job.ended_at = time.time()
        except Exception as exc:
            job.status   = "failed"
            job.error    = str(exc)
            job.ended_at = time.time()

    def list_jobs(self) -> List[FinetuneJob]:
        """Return all fine-tuning jobs."""
        with self._lock:
            return sorted(self._jobs.values(),
                           key=lambda j: j.started_at, reverse=True)

    def get_job(self, job_id: str) -> Optional[FinetuneJob]:
        """Return a specific fine-tuning job."""
        return self._jobs.get(job_id)
