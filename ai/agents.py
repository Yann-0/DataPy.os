"""
PyOS NOVA — Multi-Agent System
================================
Multiple AI agents running as background threads, each with a role.
They communicate by writing to/reading from the SOS.

Built-in agents:
  sysadmin  — monitors system health, suggests fixes
  coder     — watches for .py files, offers improvements
  researcher— answers deep questions, caches results
  taskmaster— manages a todo list, reminds about tasks

Agents coordinate via a shared SOS inbox: /ai/agents/<agent>/inbox/
"""

import os, sys, time, json, threading, queue, uuid
from typing import Dict, List, Optional, TYPE_CHECKING

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from kernel.nova import NovaKernel


class Message:
    """Message."""
    def __init__(self, sender: str, recipient: str,
                 subject: str, body: str, reply_to: str = None):
        """Initialise the instance."""
        self.id         = str(uuid.uuid4())[:8]
        self.sender     = sender
        self.recipient  = recipient
        self.subject    = subject
        self.body       = body
        self.reply_to   = reply_to
        self.ts         = time.time()
        self.read       = False

    """To dict."""
    def to_dict(self): return self.__dict__

    @staticmethod
    def from_dict(d): 
        """From dict.

            Args:
            d: D.
            """
        m = Message(d["sender"],d["recipient"],d["subject"],d["body"],d.get("reply_to"))
        m.id = d.get("id", m.id)
        m.ts = d.get("ts", m.ts)
        m.read = d.get("read", False)
        return m


class AgentBus:
    """
    Message bus backed by SOS. Agents post/read via /ai/agents/<name>/inbox/.
    """
    BASE = "/ai/agents"

    def __init__(self, sos):
        """Initialise the instance."""
        self.sos = sos
        if not sos.exists(self.BASE):
            sos.mkdir(self.BASE, parents=True)

    def _inbox(self, agent: str) -> str:
        """Inbox.

            Args:
            agent (str): Agent.


            Returns:
                str: Result.
            """
        path = f"{self.BASE}/{agent}/inbox"
        if not self.sos.exists(path):
            self.sos.mkdir(path, parents=True)
        return path

    def send(self, msg: Message):
        """Send the operation.

            Args:
            msg (Message): Msg.
            """
        path = f"{self._inbox(msg.recipient)}/{msg.id}.json"
        self.sos.write(path, json.dumps(msg.to_dict()))

    def receive(self, agent: str, max_n: int = 10) -> List[Message]:
        """Receive and return the operation.

            Args:
            agent (str): Agent.
            max_n (int): Max n, defaults to 10.


            Returns:
                List[Message]: Result.
            """
        inbox = self._inbox(agent)
        msgs  = []
        for name in self.sos.listdir(inbox)[:max_n]:
            path = f"{inbox}/{name}"
            try:
                data = json.loads(self.sos.read(path))
                m    = Message.from_dict(data)
                if not m.read:
                    m.read = True
                    self.sos.write(path, json.dumps(m.to_dict()))
                    msgs.append(m)
            except Exception:
                pass
        return msgs

    def broadcast(self, msg: Message, recipients: List[str]):
        """Broadcast the operation to all listeners.

            Args:
            msg (Message): Msg.
            recipients (List[str]): Recipients.
            """
        for r in recipients:
            m = Message(msg.sender, r, msg.subject, msg.body, msg.reply_to)
            self.send(m)


class BaseAgent:
    """Base class for all agents."""
    NAME     = "agent"
    ROLE     = "general purpose"
    INTERVAL = 60   # seconds between cycles

    def __init__(self, kernel: "NovaKernel"):
        """Initialise the instance."""
        self.kernel   = kernel
        self.bus      = AgentBus(kernel.sos)
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._log: List[str] = []

    def start(self):
        """Start the operation."""
        if self._running: return
        self._running = True
        self._thread  = threading.Thread(target=self._loop,
                                          daemon=True, name=f"agent-{self.NAME}")
        self._thread.start()

    """Stop the operation."""
    def stop(self): self._running = False

    def _loop(self):
        """Main event loop — runs until stopped."""
        time.sleep(3)   # stagger startup
        while self._running:
            try:
                # Process inbox
                for msg in self.bus.receive(self.NAME):
                    self._handle_message(msg)
                # Do periodic work
                self.tick()
            except Exception as e:
                self._log.append(f"ERROR: {e}")
            time.sleep(self.INTERVAL)

    def tick(self):
        """Override in subclasses for periodic work."""
        pass

    def _handle_message(self, msg: Message):
        """Override in subclasses."""
        pass

    def log(self, text: str):
        """Log the operation to the activity log.

            Args:
            text (str): Text.
            """
        ts = time.strftime("%H:%M")
        self._log.append(f"[{ts}] {self.NAME}: {text}")
        if len(self._log) > 100:
            self._log.pop(0)

    def say(self, text: str, recipient: str = "user"):
        """Send a message to user's advisor queue."""
        try:
            from ai.advisor import Advice
            self.kernel.advisor.push(Advice(
                f"[{self.NAME}] {text[:60]}",
                text,
                severity="info", source=f"agent:{self.NAME}",
            ))
        except Exception: pass

    def ask_llm(self, prompt: str, max_tokens: int = 256) -> str:
        """Ask llm.

            Args:
            prompt (str): Prompt.
            max_tokens (int): Max tokens, defaults to 256.


            Returns:
                str: Result.
            """
        try:
            return self.kernel.ai.ask(prompt, system_key="assistant",
                                      max_tokens=max_tokens)
        except Exception:
            return ""

    @property
    def status(self) -> dict:
        """Return the current status as a dict.


            Returns:
                dict: Result.
            """
        return {"name": self.NAME, "role": self.ROLE,
                "running": self._running, "log_lines": len(self._log)}


class SysadminAgent(BaseAgent):
    """Sysadmin agent."""
    NAME     = "sysadmin"
    ROLE     = "system health monitoring"
    INTERVAL = 120

    def tick(self):
        """Perform one periodic work cycle."""
        try:
            import psutil
            disk = psutil.disk_usage("/")
            if disk.percent > 85:
                self.say(f"Disk at {disk.percent:.0f}% — consider cleanup")
            ram = psutil.virtual_memory()
            if ram.percent > 88:
                self.say(f"RAM at {ram.percent:.0f}% — check top processes")
            # Check for many processes
            if len(psutil.pids()) > 200:
                self.say("High process count. Run `ps` to investigate.")
        except ImportError:
            pass

    def _handle_message(self, msg: Message):
        """Handle message.

            Args:
            msg (Message): Msg.
            """
        if "status" in msg.subject.lower():
            try:
                import psutil
                vm   = psutil.virtual_memory()
                disk = psutil.disk_usage("/")
                reply = (f"System: CPU {psutil.cpu_percent()}%, "
                         f"RAM {vm.percent:.0f}%, Disk {disk.percent:.0f}%")
            except ImportError:
                reply = "psutil not installed"
            self.bus.send(Message(self.NAME, msg.sender,
                                  f"Re: {msg.subject}", reply, msg.id))


class CoderAgent(BaseAgent):
    """Coder agent."""
    NAME     = "coder"
    ROLE     = "Python code quality"
    INTERVAL = 180

    def tick(self):
        # Watch for recently-modified .py files
        """Perform one periodic work cycle."""
        for name in self.kernel.sos.listdir("/home/root/projects"):
            if not name.endswith(".py"): continue
            path = f"/home/root/projects/{name}"
            try:
                oid  = self.kernel.sos.resolve(path)
                obj  = self.kernel.sos.get(oid)
                if obj and (time.time() - obj.created_at) < self.INTERVAL * 2:
                    code = obj.text
                    if len(code) > 30:
                        from ai.reviewer import StaticReviewer
                        issues = StaticReviewer().review(code, path)
                        errors = [i for i in issues if i.severity == "error"]
                        if errors:
                            self.say(f"{name}: {len(errors)} issue(s) found\n" +
                                     "\n".join(str(i) for i in errors[:3]))
            except Exception: pass

    def _handle_message(self, msg: Message):
        """Handle message.

            Args:
            msg (Message): Msg.
            """
        if "review" in msg.subject.lower():
            path = msg.body.strip()
            try:
                code = self.kernel.sos.read(path)
                from ai.reviewer import StaticReviewer
                issues = StaticReviewer().review(code, path)
                reply  = "\n".join(str(i) for i in issues[:10]) or "No issues found."
            except Exception as e:
                reply = str(e)
            self.bus.send(Message(self.NAME, msg.sender,
                                  f"Review: {path}", reply, msg.id))


class ResearcherAgent(BaseAgent):
    """Researcher agent."""
    NAME     = "researcher"
    ROLE     = "deep question answering with cache"
    INTERVAL = 300

    CACHE_BASE = "/ai/agents/researcher/cache"

    def __init__(self, kernel):
        """Initialise the instance."""
        super().__init__(kernel)
        if not kernel.sos.exists(self.CACHE_BASE):
            kernel.sos.mkdir(self.CACHE_BASE, parents=True)

    def _cache_key(self, q: str) -> str:
        """Cache key.

            Args:
            q (str): Q.


            Returns:
                str: Result.
            """
        import hashlib
        return hashlib.sha256(q.lower().strip().encode()).hexdigest()[:16]

    def _cached(self, q: str) -> Optional[str]:
        """Cached.

            Args:
            q (str): Q.


            Returns:
                Optional[str]: Result.
            """
        key  = self._cache_key(q)
        path = f"{self.CACHE_BASE}/{key}"
        if self.kernel.sos.exists(path):
            try: return self.kernel.sos.read(path)
            except: pass
        return None

    def _store(self, q: str, answer: str):
        """Store.

            Args:
            q (str): Q.
            answer (str): Answer.
            """
        key  = self._cache_key(q)
        path = f"{self.CACHE_BASE}/{key}"
        self.kernel.sos.write(path, answer, tags=["ai-research"])

    def _handle_message(self, msg: Message):
        """Handle message.

            Args:
            msg (Message): Msg.
            """
        q = msg.body.strip()
        if not q: return
        cached = self._cached(q)
        if cached:
            self.bus.send(Message(self.NAME, msg.sender,
                                  f"Re: {msg.subject} [cached]", cached, msg.id))
            return
        answer = self.ask_llm(q, max_tokens=400)
        if answer:
            self._store(q, answer)
            self.bus.send(Message(self.NAME, msg.sender,
                                  f"Re: {msg.subject}", answer, msg.id))


class TaskmasterAgent(BaseAgent):
    """Taskmaster agent."""
    NAME     = "taskmaster"
    ROLE     = "todo list and reminders"
    INTERVAL = 60

    TASKS_PATH = "/ai/agents/taskmaster/tasks.json"

    def _load_tasks(self) -> List[dict]:
        """Load tasks.


            Returns:
                List[dict]: Result.
            """
        try:
            return json.loads(self.kernel.sos.read(self.TASKS_PATH))
        except Exception:
            return []

    def _save_tasks(self, tasks: List[dict]):
        """Save tasks.

            Args:
            tasks (List[dict]): Tasks.
            """
        self.kernel.sos.write(self.TASKS_PATH, json.dumps(tasks, indent=2))

    def add_task(self, text: str, due_hours: float = None):
        """Add task.

            Args:
            text (str): Text.
            due_hours (float): Due hours, defaults to None.
            """
        tasks = self._load_tasks()
        task  = {"id": str(uuid.uuid4())[:8], "text": text,
                 "done": False, "created": time.time(),
                 "due": time.time() + due_hours*3600 if due_hours else None}
        tasks.append(task)
        self._save_tasks(tasks)
        return task

    def complete_task(self, task_id: str):
        """Complete task.

            Args:
            task_id (str): Task id.
            """
        tasks = self._load_tasks()
        for t in tasks:
            if t["id"] == task_id:
                t["done"] = True
        self._save_tasks(tasks)

    def pending_tasks(self) -> List[dict]:
        """Pending tasks.


            Returns:
                List[dict]: Result.
            """
        return [t for t in self._load_tasks() if not t["done"]]

    def tick(self):
        """Perform one periodic work cycle."""
        now   = time.time()
        tasks = self.pending_tasks()
        for t in tasks:
            if t.get("due") and t["due"] < now:
                self.say(f"Task due: {t['text']}")

    def _handle_message(self, msg: Message):
        """Handle message.

            Args:
            msg (Message): Msg.
            """
        body = msg.body.strip()
        if msg.subject.lower().startswith("add"):
            task = self.add_task(body)
            self.bus.send(Message(self.NAME, msg.sender,
                                  "Task added", f"Added: {task['text']} [{task['id']}]",
                                  msg.id))
        elif msg.subject.lower() == "list":
            tasks = self.pending_tasks()
            reply = "\n".join(f"[{t['id']}] {t['text']}" for t in tasks) or "No pending tasks."
            self.bus.send(Message(self.NAME, msg.sender, "Tasks", reply, msg.id))


class AgentManager:
    """Manages all agents. Provides the `agents` shell interface."""

    ALL_AGENTS = [SysadminAgent, CoderAgent, ResearcherAgent, TaskmasterAgent]

    def __init__(self, kernel: "NovaKernel"):
        """Initialise the instance."""
        self.kernel   = kernel
        self.bus      = AgentBus(kernel.sos)
        self._agents: Dict[str, BaseAgent] = {}

    def start_all(self):
        """Start all."""
        for cls in self.ALL_AGENTS:
            agent = cls(self.kernel)
            self._agents[cls.NAME] = agent
            agent.start()

    def stop_all(self):
        """Stop all."""
        for a in self._agents.values():
            a.stop()

    def get(self, name: str) -> Optional[BaseAgent]:
        """Return the the operation.

            Args:
            name (str): Name.


            Returns:
                Optional[BaseAgent]: Result.
            """
        return self._agents.get(name)

    def status_all(self) -> List[dict]:
        """Return the current status as a dict.


            Returns:
                List[dict]: Result.
            """
        return [a.status for a in self._agents.values()]

    def send(self, to: str, subject: str, body: str, sender: str = "user") -> str:
        """Send the operation.

            Args:
            to (str): To.
            subject (str): Subject.
            body (str): Body.
            sender (str): Sender, defaults to 'user'.


            Returns:
                str: Result.
            """
        if to not in self._agents:
            return f"Unknown agent: {to}. Available: {', '.join(self._agents)}"
        msg = Message(sender, to, subject, body)
        self.bus.send(msg)
        return f"Message sent to {to} [{msg.id}]"

    def messages_for(self, agent: str) -> List[Message]:
        """Messages for.

            Args:
            agent (str): Agent.


            Returns:
                List[Message]: Result.
            """
        return self.bus.receive(agent)
