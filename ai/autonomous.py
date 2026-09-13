"""
PyOS NOVA — Autonomous ReAct Agent
=====================================
Full autonomous multi-step agent loop:
  THINK → PLAN → ACT → OBSERVE → REFLECT → REPEAT

The agent breaks a natural language task into numbered steps,
executes each one via shell commands or Python code in the sandbox,
observes the output, and corrects on failure — without human intervention.

This is the ReAct (Reasoning + Acting) pattern applied to NOVA.

Example:
  agent run "Build a web scraper for Hacker News top stories"
  → Plan: 1.Install requests 2.Write scraper 3.Test it 4.Register as app
  → Execute step 1: pip install requests → OK
  → Execute step 2: write /home/root/hn_scraper.py → OK
  → Execute step 3: sandbox run /home/root/hn_scraper.py → OK
  → Execute step 4: app install hn-scraper → OK
  → Done in 4 steps, 23 seconds

Shell commands:
  agent run "<task>"          — run an autonomous task
  agent run --dry-run "<t>"   — show plan without executing
  agent status                — show running autonomous tasks
  agent stop <id>             — cancel a running task
  agent history               — show completed tasks
"""

from __future__ import annotations
import os, sys, time, json, re, hashlib, threading
from typing import List, Dict, Optional, Any, TYPE_CHECKING
from dataclasses import dataclass, field

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from kernel.nova import NovaKernel

HISTORY_BASE = "/ai/agent_history"

# Maximum steps before giving up
MAX_STEPS    = 12
MAX_RETRIES  = 2   # retries per step on failure


PLANNER_PROMPT = """You are an autonomous agent for PyOS NOVA — a Python operating system.
Your task: {task}

Break this into numbered steps. Each step must be one of:
  shell: <command>        — run a shell command
  python: <code>          — run Python code in sandbox
  write: <path> | <content>  — write a file to SOS
  read: <path>            — read a file from SOS
  think: <reasoning>      — internal reasoning (no action)

Respond with JSON only:
{{
  "plan": "one sentence description",
  "steps": [
    {{"step": 1, "type": "shell", "description": "Install requests library", "action": "pip install requests"}},
    {{"step": 2, "type": "python", "description": "Write scraper", "action": "print('hello')"}},
    {{"step": 3, "type": "think", "description": "Check if done", "action": "verify output looks correct"}}
  ]
}}

Keep steps concrete and small. Max {max_steps} steps."""

REFLECTOR_PROMPT = """You are an agent that just executed a step.

Task: {task}
Step: {step_desc}
Action: {action}
Output: {output}
Success: {success}

If the step failed, provide a corrected action. If it succeeded, say "OK".
Respond with JSON:
{{"status": "ok" | "retry", "corrected_action": "<new action if retry>"}}"""


@dataclass
class AgentStep:
    """One step in an autonomous agent plan."""
    step:        int
    type:        str     # shell|python|write|read|think
    description: str
    action:      str
    output:      str = ""
    success:     bool = False
    retries:     int = 0
    duration_ms: float = 0.0
    corrected:   bool = False


@dataclass
class AgentRun:
    """One autonomous agent execution run."""
    run_id:      str
    task:        str
    plan:        str
    steps:       List[AgentStep]
    status:      str = "running"   # running|done|failed|cancelled
    created_at:  float = field(default_factory=time.time)
    ended_at:    float = 0.0
    error:       str = ""

    @property
    def elapsed_s(self) -> float:
        """Return elapsed time in seconds."""
        end = self.ended_at if self.ended_at else time.time()
        return round(end - self.created_at, 1)

    def to_dict(self) -> dict:
        """Serialize to dict."""
        return {
            "run_id":     self.run_id,
            "task":       self.task,
            "plan":       self.plan,
            "steps":      [s.__dict__ for s in self.steps],
            "status":     self.status,
            "created_at": self.created_at,
            "ended_at":   self.ended_at,
            "elapsed_s":  self.elapsed_s,
        }


class AutonomousAgent:
    """
    Runs autonomous multi-step tasks using the ReAct pattern.

    Think → Plan → Act → Observe → Reflect → Repeat
    until the task is complete or MAX_STEPS is reached.
    """

    def __init__(self, kernel: "NovaKernel"):
        """Initialise the autonomous agent."""
        self.kernel  = kernel
        self._runs:  Dict[str, AgentRun] = {}
        self._lock   = threading.Lock()
        self._ensure_dirs()

    def _ensure_dirs(self):
        """Create agent history directory."""
        if not self.kernel.sos.exists(HISTORY_BASE):
            self.kernel.sos.mkdir(HISTORY_BASE, parents=True)

    def _new_id(self) -> str:
        """Generate a unique run ID."""
        return hashlib.sha256(
            f"{time.time()}".encode()
        ).hexdigest()[:8]

    def _ask_ai(self, prompt: str, max_tokens: int = 600) -> str:
        """Query the AI engine."""
        try:
            return self.kernel.ai.ask(prompt, max_tokens=max_tokens)
        except Exception:
            return ""

    def _parse_plan(self, raw: str) -> Optional[dict]:
        """Extract JSON plan from AI response."""
        raw = raw.strip()
        raw = re.sub(r"^```(?:json)?\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r'\{[\s\S]*"steps"[\s\S]*\}', raw)
            if m:
                try:
                    return json.loads(m.group())
                except Exception:
                    pass
        return None

    def _execute_step(self, step: AgentStep,
                       run: AgentRun) -> tuple[bool, str]:
        """
        Execute one step action.

        Returns (success, output).
        """
        action = step.action
        t_start = time.perf_counter()

        try:
            if step.type == "think":
                return True, f"[reasoning] {action}"

            elif step.type == "shell":
                # Run via shell
                import subprocess
                result = subprocess.run(
                    action, shell=True,
                    capture_output=True, text=True, timeout=30
                )
                out = (result.stdout + result.stderr).strip()
                return result.returncode == 0, out[:2000]

            elif step.type == "python":
                # Run in sandbox
                sb_result = self.kernel.sandbox.run(action)
                out = (sb_result.stdout + sb_result.stderr).strip()
                return sb_result.success, out[:2000]

            elif step.type == "write":
                # Parse: <path> | <content>
                if "|" in action:
                    parts   = action.split("|", 1)
                    path    = parts[0].strip()
                    content = parts[1].strip()
                else:
                    path    = action.strip()
                    content = ""
                self.kernel.sos.write(path, content)
                return True, f"Written: {path} ({len(content)} chars)"

            elif step.type == "read":
                path    = action.strip()
                content = self.kernel.sos.read(path)
                return True, content[:500]

            else:
                return False, f"Unknown step type: {step.type}"

        except Exception as e:
            return False, str(e)
        finally:
            step.duration_ms = (time.perf_counter() - t_start) * 1000

    def _reflect(self, task: str, step: AgentStep) -> Optional[str]:
        """Ask AI to reflect on a failed step and suggest a correction."""
        prompt = REFLECTOR_PROMPT.format(
            task=task, step_desc=step.description,
            action=step.action, output=step.output[:300],
            success=step.success,
        )
        raw = self._ask_ai(prompt, max_tokens=200)
        try:
            raw = re.sub(r"^```(?:json)?\n?", "", raw.strip())
            raw = re.sub(r"\n?```$", "", raw)
            data = json.loads(raw)
            if data.get("status") == "retry":
                return data.get("corrected_action")
        except Exception:
            pass
        return None

    def run(self, task: str, dry_run: bool = False,
             callback=None) -> AgentRun:
        """
        Run an autonomous task.

        Args:
            task (str): Natural language task description.
            dry_run (bool): If True, show plan without executing.
            callback: Optional callable(AgentRun) called on each step.

        Returns:
            AgentRun: The completed (or running) task record.
        """
        run_id = self._new_id()

        # Ask AI for a plan
        prompt    = PLANNER_PROMPT.format(task=task, max_steps=MAX_STEPS)
        raw_plan  = self._ask_ai(prompt, max_tokens=800)
        plan_data = self._parse_plan(raw_plan)

        if not plan_data:
            # Fallback plan
            plan_data = {
                "plan": f"Execute: {task}",
                "steps": [
                    {"step": 1, "type": "shell",
                     "description": task[:80],
                     "action": task},
                ],
            }

        steps = [
            AgentStep(
                step=s.get("step", i+1),
                type=s.get("type", "shell"),
                description=s.get("description", ""),
                action=s.get("action", ""),
            )
            for i, s in enumerate(plan_data.get("steps", []))
        ]

        agent_run = AgentRun(
            run_id=run_id,
            task=task,
            plan=plan_data.get("plan", ""),
            steps=steps,
        )
        with self._lock:
            self._runs[run_id] = agent_run

        if dry_run:
            agent_run.status = "dry_run"
            return agent_run

        # Execute in background thread
        def _execute():
            for step in agent_run.steps:
                if agent_run.status == "cancelled":
                    break
                success, output = self._execute_step(step, agent_run)
                step.output  = output
                step.success = success

                if callback:
                    try:
                        callback(agent_run, step)
                    except Exception:
                        pass

                if not success:
                    # Reflect and retry
                    for retry in range(MAX_RETRIES):
                        corrected = self._reflect(task, step)
                        if not corrected:
                            break
                        step.action   = corrected
                        step.corrected = True
                        step.retries  += 1
                        ok, out = self._execute_step(step, agent_run)
                        step.output  = out
                        step.success = ok
                        if ok:
                            break

            all_ok = all(s.success or s.type == "think"
                         for s in agent_run.steps)
            agent_run.status   = "done" if all_ok else "failed"
            agent_run.ended_at = time.time()

            # Save to SOS
            try:
                path = f"{HISTORY_BASE}/{run_id}"
                self.kernel.sos.write(
                    path, json.dumps(agent_run.to_dict()),
                    tags=["agent-run", agent_run.status],
                )
            except Exception:
                pass

        threading.Thread(target=_execute, daemon=True,
                          name=f"nova-agent-{run_id}").start()
        return agent_run

    def cancel(self, run_id: str) -> bool:
        """Cancel a running task."""
        run = self._runs.get(run_id)
        if run and run.status == "running":
            run.status = "cancelled"
            return True
        return False

    def list_runs(self, n: int = 20) -> List[AgentRun]:
        """Return recent agent runs."""
        return sorted(self._runs.values(),
                       key=lambda r: r.created_at, reverse=True)[:n]

    def history(self) -> List[dict]:
        """Load agent run history from SOS."""
        runs = []
        for name in self.kernel.sos.listdir(HISTORY_BASE):
            try:
                data = json.loads(self.kernel.sos.read(
                    f"{HISTORY_BASE}/{name}"))
                runs.append(data)
            except Exception:
                pass
        return sorted(runs, key=lambda r: r.get("created_at", 0),
                       reverse=True)[:50]
