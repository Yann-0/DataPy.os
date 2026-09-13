"""
PyOS NOVA — CI/CD Pipeline Runner  (Phase 2 — Developer Experience)
=====================================================================
Runs pipeline definitions stored in SOS.  Compatible with a simplified
subset of GitHub Actions YAML syntax so existing workflows can be reused.

Pipeline format (YAML-like, stored at /.nova/pipeline.yml):

    name: nova-ci
    on: [push, manual]
    jobs:
      lint:
        steps:
          - name: Ruff lint
            run:  ruff check .
      test:
        needs: [lint]
        steps:
          - name: Run tests
            run:  pytest tests/ -v
          - name: Coverage
            run:  pytest --cov=. tests/

Shell commands:
    ci run [job]           — run all jobs (or a specific job)
    ci run --dry-run       — show plan without executing
    ci status              — show last run results
    ci history             — list all pipeline runs
    ci log <run_id>        — show full log for a run
"""

from __future__ import annotations

import os
import sys
import time
import json
import subprocess
import threading
import hashlib
from typing import Dict, List, Optional, Tuple, Any, TYPE_CHECKING
from dataclasses import dataclass, field

if TYPE_CHECKING:
    from store.sos import SemanticObjectStore
    from kernel.nova import NovaKernel

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PIPELINE_CONFIG_PATH = "/.nova/pipeline.yml"
CI_HISTORY_BASE      = "/system/ci_runs"


@dataclass
class Step:
    """One step within a CI job."""

    name:      str
    run:       str              # shell command
    env:       Dict[str, str]  = field(default_factory=dict)
    timeout_s: int             = 300
    output:    str             = ""
    status:    str             = "pending"   # pending|ok|failed|skipped
    duration:  float           = 0.0

    def to_dict(self) -> dict:
        """Serialise to dict."""
        return self.__dict__


@dataclass
class Job:
    """One CI job containing ordered steps."""

    name:      str
    steps:     List[Step]       = field(default_factory=list)
    needs:     List[str]        = field(default_factory=list)
    env:       Dict[str, str]   = field(default_factory=dict)
    status:    str              = "pending"
    duration:  float            = 0.0


@dataclass
class PipelineRun:
    """One complete pipeline execution."""

    run_id:    str
    pipeline:  str              # pipeline name
    trigger:   str              # manual|push|schedule
    jobs:      Dict[str, Job]   = field(default_factory=dict)
    status:    str              = "running"
    started_at: float           = field(default_factory=time.time)
    ended_at:  float            = 0.0

    @property
    def elapsed_s(self) -> float:
        """Return elapsed time in seconds."""
        end = self.ended_at if self.ended_at else time.time()
        return round(end - self.started_at, 1)

    @property
    def passed(self) -> bool:
        """Return True if all jobs passed."""
        return all(j.status == "ok" for j in self.jobs.values())


class CIPipeline:
    """
    CI/CD pipeline runner for PyOS NOVA.

    Parses pipeline definitions, resolves job dependencies, and runs
    jobs in topological order (parallel where possible).
    """

    def __init__(self, kernel: "NovaKernel"):
        """Initialise the CI pipeline runner."""
        self.kernel  = kernel
        self._runs:  Dict[str, PipelineRun] = {}
        self._lock   = threading.Lock()
        self._ensure_dirs()

    def _ensure_dirs(self):
        """Create CI history directory."""
        if not self.kernel.sos.exists(CI_HISTORY_BASE):
            self.kernel.sos.mkdir(CI_HISTORY_BASE, parents=True)

    # ── Configuration ─────────────────────────────────────────────────────────

    def load_config(self, path: str = None) -> Optional[dict]:
        """
        Load pipeline configuration from SOS.

        Args:
            path: SOS path to the pipeline config. Defaults to pipeline.yml.

        Returns:
            Parsed configuration dict, or None if not found.
        """
        config_path = path or PIPELINE_CONFIG_PATH
        try:
            content = self.kernel.sos.read(config_path)
            # Parse simplified YAML (key: value pairs and lists)
            return self._parse_simple_yaml(content)
        except Exception:
            return None

    @staticmethod
    def _parse_simple_yaml(text: str) -> dict:
        """
        Parse a simplified YAML subset.

        Only handles the pipeline format — not a general YAML parser.
        Falls back to JSON if the content is already JSON.
        """
        try:
            return json.loads(text)
        except Exception:
            pass

        # Very simplified YAML → dict parser
        result: dict = {}
        current_job: Optional[str] = None
        current_step: Optional[dict] = None

        for raw_line in text.splitlines():
            line   = raw_line.rstrip()
            indent = len(line) - len(line.lstrip())
            line   = line.strip()

            if not line or line.startswith("#"):
                continue

            if ":" in line:
                key, _, value = line.partition(":")
                key   = key.strip()
                value = value.strip()

                if indent == 0:
                    result[key] = value if value else {}
                elif indent == 2 and isinstance(result.get("jobs"), dict):
                    current_job = key
                    result["jobs"][key] = {"steps": []}
                elif indent == 4 and current_job:
                    job = result["jobs"][current_job]
                    if key == "needs":
                        job["needs"] = [v.strip(" []") for v in value.split(",")]
                    elif key == "run" and current_step is not None:
                        current_step["run"] = value
                    elif key == "name":
                        current_step = {"name": value, "run": ""}
                        job["steps"].append(current_step)

        if "jobs" not in result:
            result["jobs"] = {}
        return result

    def create_default_config(self) -> str:
        """
        Create a default pipeline config in SOS and return the SOS path.

        Returns:
            SOS path of the created config.
        """
        config = json.dumps({
            "name": "nova-ci",
            "on":   ["push", "manual"],
            "jobs": {
                "lint": {
                    "steps": [
                        {"name": "Syntax check",
                         "run": f"{sys.executable} -m py_compile main.py"},
                        {"name": "Ruff (if available)",
                         "run": "ruff check . || echo 'ruff not installed'"},
                    ]
                },
                "test": {
                    "needs": ["lint"],
                    "steps": [
                        {"name": "Run tests",
                         "run": f"{sys.executable} -m pytest tests/ -v --tb=short 2>&1 | head -100"},
                    ]
                },
            }
        }, indent=2)

        parent = os.path.dirname(PIPELINE_CONFIG_PATH)
        if not self.kernel.sos.exists(parent):
            self.kernel.sos.mkdir(parent, parents=True)
        self.kernel.sos.write(PIPELINE_CONFIG_PATH, config,
                               tags=["ci-config"])
        return PIPELINE_CONFIG_PATH

    # ── Execution ─────────────────────────────────────────────────────────────

    def run(self, job_filter: str = None,
             dry_run: bool = False,
             trigger: str = "manual") -> PipelineRun:
        """
        Execute the pipeline.

        Args:
            job_filter: Only run this job (and its deps).
            dry_run:    Show execution plan without running.
            trigger:    Event that triggered the run.

        Returns:
            PipelineRun object (running in background thread).
        """
        config = self.load_config()
        if not config:
            config = {"name": "default", "jobs": {
                "check": {"steps": [
                    {"name": "Syntax check",
                     "run": f"{sys.executable} -m py_compile main.py"}
                ]}
            }}

        run_id = hashlib.sha256(f"{time.time()}".encode()).hexdigest()[:8]
        jobs   = self._build_jobs(config, job_filter)
        run    = PipelineRun(run_id=run_id,
                              pipeline=config.get("name", "pipeline"),
                              trigger=trigger, jobs=jobs)
        with self._lock:
            self._runs[run_id] = run

        if dry_run:
            run.status = "dry_run"
            return run

        threading.Thread(target=self._execute_run, args=(run,),
                          daemon=True, name=f"nova-ci-{run_id}").start()
        return run

    def _build_jobs(self, config: dict,
                     job_filter: str = None) -> Dict[str, Job]:
        """Build Job objects from config, respecting filter."""
        jobs: Dict[str, Job] = {}
        for job_name, job_cfg in config.get("jobs", {}).items():
            if job_filter and job_name != job_filter:
                continue
            steps = [
                Step(name=s.get("name", "step"),
                      run=s.get("run", "true"),
                      env=s.get("env", {}),
                      timeout_s=s.get("timeout", 300))
                for s in job_cfg.get("steps", [])
            ]
            jobs[job_name] = Job(
                name=job_name, steps=steps,
                needs=job_cfg.get("needs", []),
                env=job_cfg.get("env", {}),
            )
        return jobs

    def _execute_run(self, run: PipelineRun):
        """Execute all jobs in dependency order."""
        completed: set = set()
        max_iterations  = len(run.jobs) * 2

        for _ in range(max_iterations):
            if not run.jobs or completed >= set(run.jobs):
                break
            for job_name, job in run.jobs.items():
                if job_name in completed:
                    continue
                # Check all deps are satisfied
                if not all(dep in completed for dep in job.needs):
                    continue
                if any(run.jobs.get(dep, Job("x")).status == "failed"
                       for dep in job.needs):
                    job.status = "skipped"
                    completed.add(job_name)
                    continue
                self._execute_job(job)
                completed.add(job_name)

        run.status   = "passed" if run.passed else "failed"
        run.ended_at = time.time()
        self._save_run(run)

    def _execute_job(self, job: Job):
        """Execute one job's steps sequentially."""
        job.status = "running"
        t_start    = time.perf_counter()

        env = {**os.environ, **job.env}

        for step in job.steps:
            step.status = "running"
            t_step      = time.perf_counter()
            try:
                result = subprocess.run(
                    step.run, shell=True, capture_output=True,
                    text=True, timeout=step.timeout_s,
                    env={**env, **step.env},
                    cwd=ROOT,
                )
                step.output   = (result.stdout + result.stderr)[:2000]
                step.status   = "ok" if result.returncode == 0 else "failed"
                step.duration = time.perf_counter() - t_step
                if step.status == "failed":
                    job.status = "failed"
                    break
            except subprocess.TimeoutExpired:
                step.status   = "failed"
                step.output   = f"Timeout after {step.timeout_s}s"
                step.duration = step.timeout_s
                job.status    = "failed"
                break
            except Exception as exc:
                step.status   = "failed"
                step.output   = str(exc)
                step.duration = time.perf_counter() - t_step
                job.status    = "failed"
                break

        if job.status != "failed":
            job.status = "ok"
        job.duration = time.perf_counter() - t_start

    def _save_run(self, run: PipelineRun):
        """Persist run summary to SOS."""
        data = {
            "run_id":    run.run_id,
            "pipeline":  run.pipeline,
            "trigger":   run.trigger,
            "status":    run.status,
            "elapsed_s": run.elapsed_s,
            "jobs": {
                name: {
                    "status":   job.status,
                    "duration": round(job.duration, 2),
                    "steps": [
                        {"name":   s.name,
                         "status": s.status,
                         "ms":     round(s.duration * 1000)}
                        for s in job.steps
                    ],
                }
                for name, job in run.jobs.items()
            },
        }
        self.kernel.sos.write(
            f"{CI_HISTORY_BASE}/{run.run_id}",
            json.dumps(data, indent=2),
            tags=["ci-run", run.status],
        )

    # ── Reporting ─────────────────────────────────────────────────────────────

    def history(self, n: int = 20) -> List[dict]:
        """Return recent pipeline runs."""
        runs = []
        try:
            for name in self.kernel.sos.listdir(CI_HISTORY_BASE):
                path = f"{CI_HISTORY_BASE}/{name}"
                try:
                    data = json.loads(self.kernel.sos.read(path))
                    runs.append(data)
                except Exception:
                    pass
        except Exception:
            pass
        return sorted(runs, key=lambda r: r.get("run_id", ""),
                       reverse=True)[:n]

    def last_run(self) -> Optional[PipelineRun]:
        """Return the most recently started pipeline run."""
        with self._lock:
            if not self._runs:
                return None
            return sorted(self._runs.values(),
                           key=lambda r: r.started_at, reverse=True)[0]
