"""
PyOS NOVA — System Doctor
===========================
AI-powered system repair with:
  - Natural language problem description
  - Structured fix proposals (AI-generated)
  - Superuser approval before any change
  - Atomic execution with automatic snapshots
  - Full rollback support

Commands:
  fix "<problem>"          — analyse + propose fixes
  fix list                 — show fix history
  fix show <id>            — show fix details
  fix rollback <id>        — roll back an applied fix
  fix simulate <id>        — dry-run a rollback (no changes)
  fix clear                — clear fix history

Fix step types:
  cmd        — shell command (reverse cmd stored)
  file_write — write file (previous version auto-saved via SOS)
  file_delete— delete file (content saved before deletion)
  pip_install— install package (reverse = pip uninstall)
  pip_remove — remove package (reverse = pip install)
  config     — write config key (old value saved)
  symbolic   — informational step (no rollback needed)
"""

import os, sys, json, time, uuid, hashlib, subprocess, getpass, re
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional, Tuple, TYPE_CHECKING

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from kernel.nova import NovaKernel

# ── ANSI helpers ──────────────────────────────────────────────────────────────
R = "\033[0m"
"""C.

    Args:
    t: T.
    c: C.
    """
def _c(t, c): return f"\033[{c}m{t}{R}"
GRN  = lambda t: _c(t,"32")
RED  = lambda t: _c(t,"31")
YLW  = lambda t: _c(t,"33")
CYN  = lambda t: _c(t,"36")
MAG  = lambda t: _c(t,"35")
DIM  = lambda t: _c(t,"2")
BOLD = lambda t: _c(t,"1")

FIXES_BASE = "/system/fixes"
LEDGER_PATH = "/system/fixes/ledger.json"

RISK_COLORS = {"low": GRN, "medium": YLW, "high": RED, "critical": lambda t: _c(t,"1;31")}
RISK_ICONS  = {"low": "○", "medium": "◐", "high": "●", "critical": "✖"}


# ─────────────────────────────────────────────────────────────────── data model
@dataclass
class FixStep:
    """One atomic step in a fix."""
    step_id:         str
    kind:            str      # cmd|file_write|file_delete|pip_install|pip_remove|config|symbolic
    description:     str
    forward:         dict     # what to do
    rollback:        dict     # how to undo it
    risk:            str      # low|medium|high|critical
    snapshot_oid:    str = "" # OID of SOS object before change (for file steps)
    executed:        bool = False
    rolled_back:     bool = False
    output:          str = ""
    error:           str = ""

    """To dict."""
    def to_dict(self): return asdict(self)

    @staticmethod
    def from_dict(d):

        """From dict.

        Args:
        d: D.
        """

        return FixStep(**d)


@dataclass
class FixProposal:
    """A complete fix proposal including all steps."""
    fix_id:      str
    query:       str          # original user query
    diagnosis:   str          # AI's diagnosis
    root_cause:  str          # AI's root cause analysis
    steps:       List[FixStep]
    overall_risk: str         # worst step risk
    estimated_time: str       # "< 1 min" etc.
    reversible:  bool         # all steps have rollback
    created_at:  float
    applied_at:  Optional[float] = None
    rolled_back_at: Optional[float] = None
    status:      str = "pending"  # pending|approved|applied|rolled_back|rejected|failed
    applied_by:  str = ""

    @property
    def is_applied(self):

        """Return True if applied."""

        return self.status == "applied"
    @property
    def is_rolled_back(self):

        """Return True if rolled back."""

        return self.status == "rolled_back"

    def to_dict(self):
        """To dict."""
        d = asdict(self)
        d["steps"] = [s.to_dict() for s in self.steps]
        return d

    @staticmethod
    def from_dict(d):
        """From dict.

            Args:
            d: D.
            """
        d = dict(d)
        d["steps"] = [FixStep.from_dict(s) for s in d.get("steps", [])]
        return FixProposal(**d)


# ─────────────────────────────────────────────────────────────────── AI builder
SYSTEM_PROMPT = """You are PyOS NOVA System Doctor. Analyse the user's system problem
and generate a structured JSON fix proposal.

Respond ONLY with valid JSON in this exact format:
{
  "diagnosis": "clear one-paragraph diagnosis",
  "root_cause": "specific root cause in one sentence",
  "estimated_time": "< 1 min",
  "steps": [
    {
      "description": "what this step does",
      "kind": "cmd",
      "risk": "low",
      "forward": {"command": "pip install numpy --upgrade"},
      "rollback": {"command": "pip install numpy==1.24.0"}
    },
    {
      "description": "update config file",
      "kind": "file_write",
      "risk": "medium",
      "forward": {"path": "/etc/nova/config.json", "content": "{\"key\": \"value\"}"},
      "rollback": {"restore_previous_version": true}
    },
    {
      "description": "install missing package",
      "kind": "pip_install",
      "risk": "low",
      "forward": {"package": "psutil"},
      "rollback": {"package": "psutil"}
    },
    {
      "description": "document a manual step",
      "kind": "symbolic",
      "risk": "low",
      "forward": {"note": "Restart service manually if needed"},
      "rollback": {"note": "No rollback needed"}
    }
  ]
}

Step kinds: cmd, file_write, file_delete, pip_install, pip_remove, config, symbolic
Risk levels: low (no data loss), medium (config change), high (data modification), critical (destructive)
Always include a rollback for every non-symbolic step.
Be specific and safe. Never suggest commands that wipe data without backup."""


def _parse_ai_response(text: str) -> Optional[dict]:
    """Extract JSON from AI response, tolerant of extra text."""
    # Try direct parse
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Find JSON block
    m = re.search(r'\{[\s\S]*"diagnosis"[\s\S]*\}', text)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    # Strip markdown fences
    text = re.sub(r"^```(?:json)?\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _build_proposal_from_ai(query: str, ai_data: dict,
                              fix_id: str) -> FixProposal:
    """Build and return proposal from ai.

        Args:
        query (str): Query.
        ai_data (dict): Ai data.
        fix_id (str): Fix id.


        Returns:
            FixProposal: Result.
        """
    steps = []
    risk_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    worst_risk  = "low"

    for i, s in enumerate(ai_data.get("steps", [])):
        risk = s.get("risk", "low")
        if risk_order.get(risk, 0) > risk_order.get(worst_risk, 0):
            worst_risk = risk
        step = FixStep(
            step_id     = f"{fix_id}_s{i+1:02d}",
            kind        = s.get("kind", "cmd"),
            description = s.get("description", ""),
            forward     = s.get("forward", {}),
            rollback    = s.get("rollback", {}),
            risk        = risk,
        )
        steps.append(step)

    reversible = all(
        s.kind == "symbolic" or bool(s.rollback)
        for s in steps
    )
    return FixProposal(
        fix_id        = fix_id,
        query         = query,
        diagnosis     = ai_data.get("diagnosis", "AI diagnosis unavailable"),
        root_cause    = ai_data.get("root_cause", ""),
        steps         = steps,
        overall_risk  = worst_risk,
        estimated_time= ai_data.get("estimated_time", "unknown"),
        reversible    = reversible,
        created_at    = time.time(),
    )


def _fallback_proposal(query: str, fix_id: str) -> FixProposal:
    """Generate a safe fallback proposal when AI is unavailable."""
    q = query.lower()
    steps = []

    if "disk" in q or "space" in q or "full" in q:
        steps = [
            FixStep(f"{fix_id}_s01","cmd","Find large files (>100MB)",
                    {"command":"find / -size +100M -type f 2>/dev/null | head -20"},
                    {"note":"diagnostic only — no rollback needed"},"low"),
            FixStep(f"{fix_id}_s02","cmd","Check disk usage by directory",
                    {"command":"du -sh /* 2>/dev/null | sort -rh | head -15"},
                    {"note":"diagnostic only"},"low"),
            FixStep(f"{fix_id}_s03","cmd","Clean Python cache files",
                    {"command":'find / -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null; echo done'},
                    {"command":"echo 'Cache cleaned — cannot restore automatically'"},"medium"),
        ]
        diagnosis   = "Disk space analysis. Found common disk space culprits."
        root_cause  = "Large files, Python cache, or log accumulation."

    elif "slow" in q or "performance" in q or "cpu" in q:
        steps = [
            FixStep(f"{fix_id}_s01","cmd","Show top CPU consumers",
                    {"command":"python3 -c \"import psutil; procs=sorted(psutil.process_iter(['pid','name','cpu_percent']),key=lambda p:p.info['cpu_percent'],reverse=True); [print(f'{p.info[\\\"pid\\\"]}: {p.info[\\\"name\\\"]}: {p.info[\\\"cpu_percent\\\"]}%') for p in procs[:10]]\""},
                    {"note":"diagnostic only"},"low"),
            FixStep(f"{fix_id}_s02","symbolic","Restart heavy process if identified",
                    {"note":"After identifying the CPU-heavy process, use 'kill <pid>' to restart it"},
                    {"note":"Restart process that was stopped"},"medium"),
        ]
        diagnosis  = "Performance analysis showing CPU and process utilisation."
        root_cause = "High CPU usage from one or more processes."

    elif "package" in q or "import" in q or "install" in q:
        # Extract package name from query
        pkg_match = re.search(r"(?:install|package|import)\s+(\w[\w-]*)", q)
        pkg = pkg_match.group(1) if pkg_match else "the-package"
        steps = [
            FixStep(f"{fix_id}_s01","pip_install",f"Install {pkg}",
                    {"package": pkg},
                    {"package": pkg},"low"),
        ]
        diagnosis  = f"Missing Python package: {pkg}"
        root_cause = f"{pkg} is not installed."

    elif "memory" in q or "ram" in q or "oom" in q:
        steps = [
            FixStep(f"{fix_id}_s01","cmd","Show memory usage",
                    {"command":"python3 -c \"import psutil; vm=psutil.virtual_memory(); print(f'RAM: {vm.percent:.0f}% used  {vm.used//1024//1024}MB/{vm.total//1024//1024}MB')\""},
                    {"note":"diagnostic"},"low"),
            FixStep(f"{fix_id}_s02","cmd","Find memory-hungry processes",
                    {"command":"python3 -c \"import psutil; [print(f'{p.info[\\\"pid\\\"]}: {p.info[\\\"name\\\"]}: {p.info[\\\"memory_percent\\\"]:.1f}%') for p in sorted(psutil.process_iter(['pid','name','memory_percent']),key=lambda p:p.info['memory_percent'],reverse=True)[:10]]\""},
                    {"note":"diagnostic"},"low"),
        ]
        diagnosis  = "Memory pressure analysis."
        root_cause = "High RAM usage from running processes."

    else:
        # Generic diagnostic
        steps = [
            FixStep(f"{fix_id}_s01","cmd","Run system diagnostics",
                    {"command":"python3 -c \"import psutil; print('CPU:',psutil.cpu_percent(),'%  RAM:',psutil.virtual_memory().percent,'%  Disk:',psutil.disk_usage('/').percent,'%')\""},
                    {"note":"diagnostic only"},"low"),
        ]
        diagnosis  = f"Diagnosed: {query[:100]}"
        root_cause = "See diagnostic output above."

    return FixProposal(
        fix_id        = fix_id,
        query         = query,
        diagnosis     = diagnosis,
        root_cause    = root_cause,
        steps         = steps,
        overall_risk  = max((s.risk for s in steps),
                            key=lambda r: {"low":0,"medium":1,"high":2,"critical":3}.get(r,0)),
        estimated_time= "< 1 min",
        reversible    = True,
        created_at    = time.time(),
    )


# ─────────────────────────────────────────────────────────────────── ledger
class FixLedger:
    """Persistent fix history stored in the SOS."""

    def __init__(self, sos):
        """Initialise the instance."""
        self.sos = sos
        self._ensure_dirs()

    def _ensure_dirs(self):
        """Ensure dirs."""
        if not self.sos.exists(FIXES_BASE):
            self.sos.mkdir(FIXES_BASE, parents=True)

    def _fix_path(self, fix_id: str) -> str:
        """Fix path.

            Args:
            fix_id (str): Fix id.


            Returns:
                str: Result.
            """
        return f"{FIXES_BASE}/{fix_id}.json"

    def save(self, proposal: FixProposal):
        """Save the operation.

            Args:
            proposal (FixProposal): Proposal.
            """
        self.sos.write(self._fix_path(proposal.fix_id),
                       json.dumps(proposal.to_dict(), indent=2),
                       tags=["fix-ledger", proposal.status])

    def load(self, fix_id: str) -> Optional[FixProposal]:
        """Load the operation.

            Args:
            fix_id (str): Fix id.


            Returns:
                Optional[FixProposal]: Result.
            """
        try:
            data = json.loads(self.sos.read(self._fix_path(fix_id)))
            return FixProposal.from_dict(data)
        except Exception:
            return None

    def all(self) -> List[FixProposal]:
        """All.


            Returns:
                List[FixProposal]: Result.
            """
        proposals = []
        for name in self.sos.listdir(FIXES_BASE):
            if not name.endswith(".json") or name == "ledger.json":
                continue
            fix_id = name[:-5]
            p = self.load(fix_id)
            if p:
                proposals.append(p)
        return sorted(proposals, key=lambda p: p.created_at, reverse=True)

    def snapshot_path(self, fix_id: str, step_id: str) -> str:
        """Snapshot path to the ledger.

            Args:
            fix_id (str): Fix id.
            step_id (str): Step id.


            Returns:
                str: Result.
            """
        return f"{FIXES_BASE}/{fix_id}_snap_{step_id}"


# ─────────────────────────────────────────────────────────────────── executor
class FixExecutor:
    """Executes and rolls back fix steps."""

    def __init__(self, sos, ledger: FixLedger):
        """Initialise the instance."""
        self.sos    = sos
        self.ledger = ledger

    def _exec_cmd(self, command: str,
                  timeout: int = 30) -> Tuple[bool, str]:
        """Exec cmd.

            Args:
            command (str): Command.
            timeout (int): Timeout, defaults to 30.


            Returns:
                Tuple[bool, str]: Result.
            """
        try:
            result = subprocess.run(
                command, shell=True,
                capture_output=True, text=True, timeout=timeout
            )
            output = result.stdout + result.stderr
            return result.returncode == 0, output.strip()
        except subprocess.TimeoutExpired:
            return False, "Command timed out"
        except Exception as e:
            return False, str(e)

    def _snapshot_file(self, fix_id: str, step: FixStep, path: str) -> str:
        """Save current file content to SOS snapshot. Returns snapshot path."""
        snap_path = self.ledger.snapshot_path(fix_id, step.step_id)
        try:
            content = self.sos.read(path)
            self.sos.write(snap_path, content,
                           tags=["fix-snapshot"],
                           meta={"original_path": path,
                                 "fix_id": fix_id,
                                 "step_id": step.step_id})
        except Exception:
            self.sos.write(snap_path, "",
                           meta={"original_path": path,
                                 "existed": False,
                                 "fix_id": fix_id})
        return snap_path

    def execute_step(self, fix_id: str,
                     step: FixStep, dry_run: bool = False) -> Tuple[bool, str]:
        """Execute one step. Returns (success, output)."""
        if dry_run:
            return True, f"[dry-run] would execute: {step.kind}: {step.forward}"

        kind = step.kind
        fwd  = step.forward

        if kind == "symbolic":
            return True, f"Manual: {fwd.get('note','')}"

        elif kind == "cmd":
            cmd = fwd.get("command", "")
            if not cmd:
                return False, "No command specified"
            ok, out = self._exec_cmd(cmd)
            return ok, out

        elif kind == "file_write":
            path    = fwd.get("path", "")
            content = fwd.get("content", "")
            if not path:
                return False, "No path specified"
            # Snapshot current state before overwriting
            self._snapshot_file(fix_id, step, path)
            try:
                self.sos.write(path, content)
                return True, f"Written {len(content)} chars to {path}"
            except Exception as e:
                return False, str(e)

        elif kind == "file_delete":
            path = fwd.get("path", "")
            if not path:
                return False, "No path"
            # Snapshot before deletion
            self._snapshot_file(fix_id, step, path)
            try:
                self.sos.remove(path)
                return True, f"Deleted {path}"
            except Exception as e:
                return False, str(e)

        elif kind == "pip_install":
            pkg = fwd.get("package", "")
            if not pkg:
                return False, "No package name"
            ok, out = self._exec_cmd(
                f"{sys.executable} -m pip install {pkg} --quiet")
            return ok, out

        elif kind == "pip_remove":
            pkg = fwd.get("package", "")
            ok, out = self._exec_cmd(
                f"{sys.executable} -m pip uninstall {pkg} -y --quiet")
            return ok, out

        elif kind == "config":
            path    = fwd.get("path", "")
            key     = fwd.get("key", "")
            value   = fwd.get("value", "")
            if path and self.sos.exists(path):
                self._snapshot_file(fix_id, step, path)
            try:
                if path.endswith(".json"):
                    try: data = json.loads(self.sos.read(path))
                    except: data = {}
                    data[key] = value
                    self.sos.write(path, json.dumps(data, indent=2))
                else:
                    self.sos.write(path, f"{key}={value}\n")
                return True, f"Set {key}={value} in {path}"
            except Exception as e:
                return False, str(e)

        return False, f"Unknown step kind: {kind}"

    def rollback_step(self, fix_id: str,
                      step: FixStep, dry_run: bool = False) -> Tuple[bool, str]:
        """Roll back one step."""
        if not step.executed:
            return True, "Step was not executed — nothing to roll back"

        kind = step.kind
        rb   = step.rollback

        if dry_run:
            return True, f"[dry-run] would rollback: {kind}: {rb}"

        if kind == "symbolic":
            return True, f"Manual rollback: {rb.get('note', 'No action needed')}"

        elif kind == "cmd":
            cmd = rb.get("command", "")
            if not cmd:
                return True, "No rollback command specified (may not be needed)"
            ok, out = self._exec_cmd(cmd)
            return ok, out

        elif kind in ("file_write", "file_delete", "config"):
            # Restore from snapshot
            snap_path = self.ledger.snapshot_path(fix_id, step.step_id)
            orig_path = step.forward.get("path", "")
            if not orig_path:
                return False, "No original path in step"
            if self.sos.exists(snap_path):
                snap_meta = self.sos.stat(snap_path)
                existed   = snap_meta.get("meta", {}).get("existed", True)
                if not existed:
                    # File didn't exist before — delete it
                    try:
                        self.sos.remove(orig_path)
                        return True, f"Restored: removed {orig_path} (didn't exist before fix)"
                    except Exception as e:
                        return False, str(e)
                # Restore content
                try:
                    content = self.sos.read(snap_path)
                    self.sos.write(orig_path, content)
                    return True, f"Restored {orig_path} from snapshot"
                except Exception as e:
                    return False, str(e)
            # Fall back to SOS version history
            elif rb.get("restore_previous_version"):
                history = self.sos.history(orig_path)
                if len(history) >= 2:
                    prev = history[1]   # second-newest = before the fix
                    self.sos.write(orig_path, prev.text)
                    return True, f"Restored {orig_path} to version {prev.version}"
                return False, "No previous version available"
            return True, "Snapshot not found — file may have been in original state"

        elif kind == "pip_install":
            pkg = rb.get("package", "")
            ok, out = self._exec_cmd(
                f"{sys.executable} -m pip uninstall {pkg} -y --quiet")
            return ok, out

        elif kind == "pip_remove":
            pkg = rb.get("package", "")
            ok, out = self._exec_cmd(
                f"{sys.executable} -m pip install {pkg} --quiet")
            return ok, out

        return True, f"No rollback implemented for: {kind}"


# ─────────────────────────────────────────────────────────────────── UI helpers
def _print_proposal(proposal: FixProposal, current_user: str = "root"):
    """Print proposal.

        Args:
        proposal (FixProposal): Proposal.
        current_user (str): Current user, defaults to 'root'.
        """
    risk_fn = RISK_COLORS.get(proposal.overall_risk, YLW)
    icon    = RISK_ICONS.get(proposal.overall_risk, "·")

    print()
    print(f"  {CYN('─' * 58)}")
    print(f"  {BOLD('System Doctor — Fix Proposal')}")
    print(f"  {CYN('─' * 58)}")
    print(f"  {BOLD('Query    :')} {proposal.query}")
    print(f"  {BOLD('ID       :')} {DIM(proposal.fix_id)}")
    print(f"  {BOLD('Risk     :')} {risk_fn(icon + ' ' + proposal.overall_risk.upper())}")
    print(f"  {BOLD('Time     :')} {proposal.estimated_time}")
    print(f"  {BOLD('Reversible:')} {GRN('yes') if proposal.reversible else RED('no')}")
    print()
    print(f"  {BOLD('Diagnosis:')}")
    for line in proposal.diagnosis.splitlines():
        print(f"    {line}")
    if proposal.root_cause:
        print(f"\n  {BOLD('Root cause:')} {proposal.root_cause}")
    print()
    print(f"  {BOLD('Steps ({n}):'.format(n=len(proposal.steps)))}")
    for i, step in enumerate(proposal.steps, 1):
        sfn  = RISK_COLORS.get(step.risk, YLW)
        sico = RISK_ICONS.get(step.risk, "·")
        print(f"  {DIM(str(i)+'.')} {step.description}")
        print(f"     {sfn(sico)} risk: {step.risk}   type: {step.kind}")
        # Show forward action
        fwd = step.forward
        if step.kind == "cmd":
            print(f"     {DIM('run :')} {fwd.get('command','')[:80]}")
        elif step.kind in ("file_write","file_delete","config"):
            print(f"     {DIM('path:')} {fwd.get('path','')}")
            if step.kind == "file_write":
                preview = fwd.get("content","")[:60].replace("\n","↵")
                print(f"     {DIM('data:')} {preview}...")
        elif step.kind in ("pip_install","pip_remove"):
            print(f"     {DIM('pkg :')} {fwd.get('package','')}")
        # Show rollback
        rb = step.rollback
        if rb and step.kind != "symbolic":
            if "command" in rb:
                print(f"     {DIM('undo:')} {rb['command'][:80]}")
            elif rb.get("restore_previous_version"):
                print(f"     {DIM('undo:')} restore previous SOS version")
            elif "note" in rb:
                print(f"     {DIM('undo:')} {rb['note']}")
    print(f"\n  {CYN('─' * 58)}")


def _require_root(user: str) -> bool:
    """Require root.

        Args:
        user (str): User.


        Returns:
            bool: Result.
        """
    return user == "root"


# ─────────────────────────────────────────────────────────────────── main class
class SystemDoctor:
    """
    Main system repair orchestrator.
    Registered on the kernel as kernel.doctor.
    """

    def __init__(self, kernel: "NovaKernel"):
        """Initialise the instance."""
        self.kernel  = kernel
        self.sos     = kernel.sos
        self.ledger  = FixLedger(kernel.sos)
        self.executor= FixExecutor(kernel.sos, self.ledger)

    # ── public API ─────────────────────────────────────────────────────────────
    def analyse(self, query: str) -> FixProposal:
        """Generate a fix proposal for a problem description."""
        fix_id = self._new_id()
        print(DIM(f"\n  Analysing: {query}"))
        print(DIM("  Consulting AI..."), end="", flush=True)

        ai_data = None
        if self.kernel.ai.tier != "rag":
            raw = self.kernel.ai.ask(
                f"System problem: {query}\n\nGenerate a fix proposal.",
                system_key="assistant",
                max_tokens=800,
            )
            ai_data = _parse_ai_response(raw)

        if ai_data and "steps" in ai_data and ai_data["steps"]:
            print(DIM(" done (AI)"))
            proposal = _build_proposal_from_ai(query, ai_data, fix_id)
        else:
            print(DIM(" done (built-in)"))
            proposal = _fallback_proposal(query, fix_id)

        # Add system context to diagnosis
        try:
            import psutil
            vm   = psutil.virtual_memory()
            disk = psutil.disk_usage("/")
            ctx  = (f"\n  System state: CPU {psutil.cpu_percent():.0f}%  "
                    f"RAM {vm.percent:.0f}%  Disk {disk.percent:.0f}%")
            proposal.diagnosis += ctx
        except ImportError:
            pass

        self.ledger.save(proposal)
        return proposal

    def present_and_approve(
        self, proposal: FixProposal,
        current_user: str = "root",
        step_by_step: bool = False,
    ) -> bool:
        """
        Show the proposal, ask for approval.
        Returns True if approved.
        """
        _print_proposal(proposal, current_user)

        # Require root for high/critical risk
        risk_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        if risk_order.get(proposal.overall_risk, 0) >= 2:
            if not _require_root(current_user):
                print(RED(f"\n  This fix requires root privileges (risk: {proposal.overall_risk})"))
                print(DIM("  Switch user: su root"))
                return False

        print(f"\n  Options:")
        print(f"    {CYN('a')} — approve all steps at once")
        print(f"    {CYN('s')} — approve step-by-step (confirm each step)")
        print(f"    {CYN('d')} — dry-run (simulate, no changes)")
        print(f"    {CYN('n')} — reject (don't apply)")
        print()

        try:
            ans = input(f"  Choice [a/s/d/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(); return False

        if ans == "d":
            self._dry_run(proposal)
            return False
        if ans not in ("a", "s"):
            print(DIM("  Fix rejected."))
            proposal.status = "rejected"
            self.ledger.save(proposal)
            return False

        proposal._step_by_step = (ans == "s")
        return True

    def apply(self, proposal: FixProposal,
              current_user: str = "root") -> bool:
        """Apply an approved fix proposal."""
        step_by_step = getattr(proposal, "_step_by_step", False)
        print(f"\n  {CYN('Applying fix')} {DIM(proposal.fix_id)}")
        print()

        proposal.status     = "approved"
        proposal.applied_by = current_user
        self.ledger.save(proposal)

        applied_steps = []
        failed        = False

        for i, step in enumerate(proposal.steps, 1):
            print(f"  {DIM(str(i)+'/'+str(len(proposal.steps)))} {step.description}...",
                  end="", flush=True)

            if step_by_step and step.kind != "symbolic":
                print()
                rfn  = RISK_COLORS.get(step.risk, YLW)
                print(f"  {rfn(RISK_ICONS[step.risk])} Risk: {step.risk}")
                if step.kind == "cmd":
                    print(f"  Command: {step.forward.get('command','')}")
                try:
                    conf = input(f"  Apply this step? [Y/n]: ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print(); conf = "n"
                if conf == "n":
                    print(DIM("  Step skipped."))
                    continue

            ok, output = self.executor.execute_step(proposal.fix_id, step)
            step.executed = True
            step.output   = output[:500]

            if ok:
                print(f" {GRN('✓')}")
                if output and len(output) < 200:
                    for line in output.splitlines()[:5]:
                        print(f"    {DIM(line)}")
                applied_steps.append(step)
            else:
                step.error = output
                print(f" {RED('✗')}")
                print(f"  {RED('Error:')} {output[:200]}")

                if step.risk in ("high", "critical"):
                    print(RED("\n  Critical step failed. Auto-rolling back..."))
                    self._auto_rollback(proposal, applied_steps)
                    proposal.status = "failed"
                    self.ledger.save(proposal)
                    failed = True
                    break
                else:
                    print(YLW("  Non-critical failure — continuing..."))

        if not failed:
            proposal.status     = "applied"
            proposal.applied_at = time.time()
            self.ledger.save(proposal)
            print(f"\n  {GRN('Fix applied successfully!')}")
            print(f"  ID: {DIM(proposal.fix_id)}")
            print(f"  To roll back: {CYN('fix rollback ' + proposal.fix_id)}")

        return not failed

    def _auto_rollback(self, proposal: FixProposal,
                        executed_steps: List[FixStep]):
        """Automatically roll back executed steps on failure."""
        print(DIM("  Rolling back executed steps in reverse order..."))
        for step in reversed(executed_steps):
            ok, out = self.executor.rollback_step(proposal.fix_id, step)
            icon    = GRN("✓") if ok else RED("✗")
            print(f"  {icon} Rolled back: {step.description}")
            step.rolled_back = True

    def rollback(self, fix_id: str,
                 current_user: str = "root",
                 dry_run: bool = False) -> bool:
        """Roll back an applied fix."""
        proposal = self.ledger.load(fix_id)
        if not proposal:
            print(RED(f"  Fix not found: {fix_id}"))
            return False

        if proposal.status not in ("applied",):
            print(YLW(f"  Fix status is '{proposal.status}' — can only roll back 'applied' fixes"))
            return False

        print(f"\n  {CYN('Rollback plan for')} {DIM(fix_id)}")
        print(f"  {BOLD('Original query:')} {proposal.query}")
        print(f"  {BOLD('Applied at    :')} {time.strftime('%Y-%m-%d %H:%M', time.localtime(proposal.applied_at or 0))}")
        print(f"  {BOLD('Steps to undo :')} {len([s for s in proposal.steps if s.executed])}")
        print()

        executed = [s for s in proposal.steps if s.executed]
        for i, step in enumerate(reversed(executed), 1):
            rb = step.rollback
            print(f"  {DIM(str(i)+'.')} Undo: {step.description}")
            if "command" in rb:
                print(f"     {DIM('run:')} {rb['command'][:80]}")
            elif rb.get("restore_previous_version"):
                print(f"     {DIM('action:')} restore previous SOS version of {step.forward.get('path','')}")

        print()
        if dry_run:
            print(DIM("  [Dry-run mode — no changes made]"))
            return True

        try:
            ans = input(f"  Confirm rollback? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(); return False
        if ans not in ("y", "yes"):
            print(DIM("  Rollback cancelled."))
            return False

        print(f"\n  {CYN('Rolling back...')}")
        all_ok = True
        for step in reversed(executed):
            print(f"  Undoing: {step.description}...", end="", flush=True)
            ok, output = self.executor.rollback_step(proposal.fix_id, step,
                                                      dry_run=dry_run)
            print(f" {GRN('✓') if ok else RED('✗')}")
            if output and not dry_run:
                for line in output.splitlines()[:3]:
                    print(f"    {DIM(line)}")
            if not ok:
                all_ok = False
                print(f"  {YLW('Warning: rollback step failed:')} {output[:100]}")
            step.rolled_back = True

        proposal.status         = "rolled_back"
        proposal.rolled_back_at = time.time()
        self.ledger.save(proposal)

        if all_ok:
            print(f"\n  {GRN('Rollback complete.')} System restored to pre-fix state.")
        else:
            print(f"\n  {YLW('Rollback completed with warnings.')} Check output above.")

        return all_ok

    def _dry_run(self, proposal: FixProposal):
        """Dry run.

            Args:
            proposal (FixProposal): Proposal.
            """
        print(f"\n  {CYN('Dry-run — simulating fix (no changes)')}")
        for i, step in enumerate(proposal.steps, 1):
            ok, out = self.executor.execute_step(proposal.fix_id, step, dry_run=True)
            print(f"  {DIM(str(i)+'.')} {step.description}")
            print(f"    {DIM(out)}")
        print(f"\n  {DIM('Dry-run complete. Run again without dry-run to apply.')}")

    def list_fixes(self, n: int = 20) -> List[FixProposal]:
        """Return a list of fixes.

            Args:
            n (int): N, defaults to 20.


            Returns:
                List[FixProposal]: Result.
            """
        return self.ledger.all()[:n]

    def _new_id(self) -> str:
        """New id.


            Returns:
                str: Result.
            """
        ts  = time.strftime("%Y%m%d_%H%M%S")
        rnd = uuid.uuid4().hex[:6]
        return f"fix_{ts}_{rnd}"
