"""
PyOS NOVA — Kernel Orchestrator
================================
The central kernel class that owns every NOVA subsystem.

On instantiation, ``NovaKernel.__init__`` creates all subsystem objects.
On ``boot()``, it starts background services (search indexer, advisor,
agents, mDNS discovery) and builds the interactive shell.

All subsystems are accessible as attributes so that shell commands,
API handlers, and agents can reach any capability through a single
``kernel`` reference:

    kernel.sos        — Semantic Object Store
    kernel.ai         — tiered LLM engine
    kernel.search     — HNSW + BM25 vector search
    kernel.advisor    — proactive health advisor
    kernel.agents     — multi-agent manager
    kernel.mem        — AI persistent memory
    kernel.reviewer   — static + AI code reviewer
    kernel.doctor     — AI system repair (fix + rollback)
    kernel.crypto     — per-object AES-256-GCM encryption
    kernel.branches   — git-like SOS branching
    kernel.discovery  — LAN peer discovery
    kernel.plugins    — app store
    kernel.scripting  — shell scripting engine
    kernel.shell      — interactive shell (set in boot())

Design note:
    The kernel intentionally violates the single-responsibility principle —
    it is the *composition root* of the entire OS.  Individual subsystems
    are responsible for their own logic; the kernel only wires them together
    and exposes a stable syscall surface.
===
All subsystems wired together.
"""

import os
import hashlib
import os, sys, time, threading, logging

log = logging.getLogger("nova.kernel")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

from store.sos      import SemanticObjectStore
from store.branches import BranchManager
from store.crypto   import CryptoEngine
from ai.engine      import AIEngine
from ai.advisor     import Advisor, Agent, ScriptWriter
from ai.memory      import MemoryManager, patch_engine_with_memory
from ai.reviewer    import CodeReviewer
from ai.agents      import AgentManager
from search.neural  import NeuralSearch
from net.server     import APIServer, FileServer, SOSSync, NOVADiscovery
from security.audit import SecurityAuditor
from plugins.manager import PluginManager
from system.doctor    import SystemDoctor
from system.sandbox    import SandboxManager
from security.capabilities import CapabilityStore
from security.zkauth   import ZKAuthManager
from security.ledger   import AuditTrail, patch_sos_with_audit
from store.lineage     import LineageTracker, patch_sos_with_lineage
from store.classifier  import ClassificationManager
from store.timelock    import TimeLockManager
from store.privacy     import DifferentialPrivacy
from store.nlquery     import NLQueryEngine
from store.prefetch    import MarkovPrefetcher
from store.immutable   import ImmutablePartition
from store.dataplane   import DataPlane
from net.crdt          import CRDTStore, ProcessReputation
from kernel.hotreload  import HotReloader
from store.waq         import patch_sos_with_waq
from store.bloom       import SOSBloomIndex
from store.schema      import SchemaRegistry
from store.stream      import StreamManager
from store.eventsource import EventSourceLog, patch_sos_with_event_sourcing
from store.advanced    import (MaterialisedViewCache, MVCCManager,
                                ObjectStreamer, ColumnarStore)
from net.resilience    import (CircuitBreakerRegistry, RateLimiter,
                                Tracer, retry)
from system.devtools   import (NovaDebugger, BuildSystem,
                                FlameGraphProfiler, SessionRecorder)
from store.events      import patch_sos_with_events, EventBus
from store.compression import patch_sos_with_compression
from search.fast       import FastNgramEmbedder, IVFIndex
from ai.speculative    import SpeculativeExecutor, LLMKVCache, patch_ai_with_kvcache
from ai.federated      import FederatedLearner
from runtime.workers   import ProcessPoolManager
from runtime.wasm      import WASMRuntime
from runtime.replay    import ReplayRecorder, ReplayPlayer, patch_sos_with_recorder
from runtime.debugger  import DebugManager
from runtime.build     import BuildSystem as RuntimeBuildSystem
from runtime.profiler  import FlameProfiler
from runtime.completeness import (
    GitBridge, ResilienceKit, ChaosEngine, ViewManager, MVCCTransaction,
)
from store.pipeline    import StreamProcessor, EventSourceStore, SchemaValidator
from net.ssh_server    import NovaSSHServer
from i18n.engine       import I18n, set_global_i18n
from kernel.watchdog   import KernelWatchdog, check_and_repair_sos
from plugins.registry  import PluginRegistry
from system.pkgmgr     import PackageManager
from ai.context          import ContextManager
from net.http_client     import HTTPClient, HealthServer, DistributedReplicator, VPNNode
from runtime.ci          import CIPipeline
from system.enterprise import (TenantManager, RBAC, RaftNode,
                                ComplianceManager, Observability, CloudManifests)
from system.devtools2  import HTTPClient, Benchmarker, DevLinter, Tutorial, Notifier
from apps.repl         import RichREPL
from shell.scripting2  import NovaInterpreter
from net.platform      import VPNTunnel, ServiceMesh, EmailServer, StreamingAI, LoadTester
from net.discovery     import ServiceDiscovery, APIGateway, DNSServer
from ai.models         import ModelManager, Finetuner
from shell.scripting import ScriptingEngine
from kernel.memory  import MemoryManager as SysMemoryManager
from kernel.process import ProcessManager


class NovaKernel:
    """Central kernel class.  Owns and wires all PyOS NOVA subsystems.

    Usage::

        kernel = NovaKernel()
        kernel.boot()
        kernel.shell.run()

    After ``boot()`` the following background threads are running:

    * Search indexer (``nova-indexer``)
    * Proactive advisor (``nova-advisor``)
    * Four AI agents (``nova-agents``)
    * mDNS peer discovery (``nova-mdns``)
    """

    VERSION = "2.0.0-nova"
    """Kernel version string shown in ``uname`` and the boot log."""

    def __init__(self):
        """Instantiate all subsystems.

        Does *not* start any background threads — call :meth:`boot` for
        that.  Separating construction from startup makes unit testing
        possible without spawning threads.
        """
        _dbg = os.environ.get("NOVA_BOOT_DEBUG")
        def _p(msg: str) -> None:
            if _dbg:
                print(f"[boot] {msg}", flush=True)

        _p("sos")
        # Core storage
        self.sos      = SemanticObjectStore()
        self.branches = BranchManager(self.sos)
        self.crypto   = CryptoEngine(self.sos)

        _p("memory/procs")
        # System
        self.memory   = SysMemoryManager(total_mb=1024)
        self.procs    = ProcessManager()

        _p("ai")
        # AI
        self.ai       = AIEngine()
        self.mem      = MemoryManager(self.sos)       # AI memory
        self.reviewer = CodeReviewer(self.ai)
        self.agents   = AgentManager(self)
        self.advisor  = Advisor(self)
        self.agent    = Agent(self)
        self.writer   = ScriptWriter(self)

        _p("search")
        # Search
        self.search   = NeuralSearch(self.sos)

        _p("network1")
        # Network
        self.api_server  = APIServer(self.sos, port=8080)
        self.file_server = FileServer(self.sos, port=8081)
        self.sync        = SOSSync(self.sos)
        self.discovery   = NOVADiscovery(api_port=8080)

        _p("security")
        # Security
        self.auditor  = SecurityAuditor(self)

        # Plugins
        self.plugins  = PluginManager(self)

        # System Doctor
        self.doctor = SystemDoctor(self)

        _p("caps/data")
        # Security subsystems
        self.caps       = CapabilityStore(self.sos)
        self.zk         = ZKAuthManager(self.sos)
        self.audit      = AuditTrail(self.sos)
        self.reputation = ProcessReputation(self.sos)
        # Primary I/O: flat handles + tags + capabilities (no folder tree)
        self.data       = DataPlane(
            self.sos, self.caps, enforce=False, audit=self.audit
        )
        self.data.load_policy()
        self.api_server.dataplane = self.data
        self.file_server.host = "127.0.0.1"
        self.file_server.dataplane = self.data
        if hasattr(self.ai, "bind_store"):
            self.ai.bind_store(sos=self.sos, dataplane=self.data)

        _p("data mgmt")
        # Data management
        self.lineage    = LineageTracker(self.sos)
        self.classifier = ClassificationManager(self.sos, self.ai)
        self.timelock   = TimeLockManager(self.sos)
        self.privacy    = DifferentialPrivacy(self.sos)
        self.nlquery    = NLQueryEngine(self.sos, self.ai)
        self.prefetch   = MarkovPrefetcher(self.sos)
        self.immutable  = ImmutablePartition(self.sos)

        _p("hotreload")
        # Distribution & runtime
        self.crdt       = CRDTStore(self.sos)
        self.sandbox    = SandboxManager(self)
        # ── Core kernel subsystem ─────────────────────────────────────────────
        # HotReloader watches Python modules for changes and reloads them live.
        # This is how NOVA updates itself without rebooting.
        self.hotreload  = HotReloader(self)

        _p("devtools")
        # ── Phase 1: Stabilisation ─────────────────────────────────────────────
        # Watchdog monitors subsystem health and auto-restarts failures.

        # New feature subsystems
        self.debugger   = DebugManager(self)
        self.builder    = BuildSystem(self)
        self.profiler   = FlameProfiler(self)
        self.cast       = SessionRecorder(self.sos)
        self.git        = GitBridge(self)
        self.resilience = ResilienceKit()
        self.tracer     = Tracer(self.sos)
        self.chaos      = ChaosEngine(self)
        self.ssh        = NovaSSHServer(self)
        self.i18n       = I18n(sos=self.sos, ai=self.ai)
        self.watchdog   = KernelWatchdog(self)
        self.plugins    = PluginRegistry(self)
        self.pkg        = PackageManager(self.sos)
        _p("discovery")
        self.discovery  = ServiceDiscovery(self)
        self.gateway    = APIGateway(self.sos)
        self.dns        = DNSServer(self.sos)
        self.models     = ModelManager(self)
        self.finetuner  = Finetuner(self)
        _p("enterprise")
        self.tenants    = TenantManager(self)
        self.rbac       = RBAC(self.sos)
        self.compliance = ComplianceManager(self.sos)
        self.observe    = Observability(self)
        self.cloud      = CloudManifests()
        _node_id        = hashlib.sha256(
            __import__('socket').gethostname().encode()).hexdigest()[:12]
        self.raft       = RaftNode(_node_id, [], self.sos)
        self.http       = HTTPClient(self.sos)
        self.bench      = Benchmarker()
        self.linter     = DevLinter()
        self.tutorial   = Tutorial(self)
        self.notifier   = Notifier()
        self.repl       = RichREPL(self)
        self.script     = NovaInterpreter(self)
        self.vpn        = VPNTunnel(self.sos)
        self.mesh       = ServiceMesh(self.sos)
        self.mail       = EmailServer(self.sos)
        self.stream_ai  = StreamingAI(self)
        self.loadtest   = LoadTester()
        self.context    = ContextManager(self.sos, self.ai)
        self.http       = HTTPClient(self.sos)
        self.health     = HealthServer(self)
        self.vpn        = VPNNode(self)
        self.replicator = DistributedReplicator(self)
        self.ci         = CIPipeline(self)
        set_global_i18n(self.i18n)
        self.stream     = None  # init after event_bus
        self.eventsrc   = EventSourceStore(self.sos)
        self.schema     = SchemaValidator(self.sos)
        self.bloom      = SOSBloomIndex(self.sos)
        self.views      = ViewManager(self.sos)

        _p("workers")
        # Performance subsystems
        data_dir          = os.environ.get("NOVA_DATA",
                            os.path.expanduser("~/.nova"))
        db_path           = os.path.join(data_dir, "sos.db")
        self.workers      = ProcessPoolManager(db_path, data_dir)
        self.event_bus    = EventBus()
        self.recorder     = ReplayRecorder(self.sos)
        self.replayer     = ReplayPlayer(self)
        self.wasm         = WASMRuntime(self)
        self.federated    = FederatedLearner(self)
        self.speculative  = SpeculativeExecutor(self)
        self.kvcache      = LLMKVCache(self.sos)

        _p("storage innov")
        # Storage innovations
        self.bloom        = SOSBloomIndex(self.sos)
        self.schema_reg   = SchemaRegistry(self.sos)
        self.stream_mgr   = StreamManager(self.sos, self.event_bus)
        self.es_log       = EventSourceLog(self.sos)
        self.views        = MaterialisedViewCache(self.sos)
        self.mvcc         = MVCCManager(self.sos)
        self.obj_streamer = ObjectStreamer(self.sos)
        self.columnar     = ColumnarStore(self.sos)

        _p("net2")
        # Network & resilience
        self.circuits     = CircuitBreakerRegistry()
        self.rate_limiter = RateLimiter()
        self.tracer       = Tracer(self.sos)
        self.ssh_server   = self.ssh
        self.git_bridge   = self.git
        self.dns          = DNSServer(self.sos)

        _p("devtools2")
        # Developer tooling
        self.debugger     = NovaDebugger(self)
        self.build        = BuildSystem(self)
        self.profiler     = FlameGraphProfiler(self.sos)
        self.cast         = SessionRecorder(self.sos)

        # Shell scripting engine
        self.scripting = ScriptingEngine(self.sos)

        self.shell    = None   # set in boot()
        _p("init done")
        # Seed flat handles before SOS monkey-patches (avoids patch deadlocks).
        try:
            self.data.seed_core()
            if hasattr(self.bloom, "rebuild"):
                self.bloom.rebuild()
            _p("seed-early")
        except Exception as exc:
            _p(f"seed-early FAILED: {exc}")

    def boot(self) -> None:
        """Start services and build the interactive shell.

        With ``NOVA_NO_AI=1`` or ``NOVA_LEAN=1``, skip discovery, metrics,
        agents, advisor, and most SOS monkey-patches — product path is
        SOS + DataPlane + shell.
        """
        from shell.nova_shell import NovaShell

        lean = os.environ.get("NOVA_NO_AI") == "1" or os.environ.get(
            "NOVA_LEAN"
        ) == "1"

        def _step(name: str, fn) -> None:
            if os.environ.get("NOVA_BOOT_DEBUG"):
                print(f"[boot] {name}", flush=True)
            try:
                fn()
            except Exception as exc:
                if os.environ.get("NOVA_BOOT_DEBUG"):
                    print(f"[boot] {name} FAILED: {exc}", flush=True)

        _step("ai-memory", lambda: patch_engine_with_memory(self.ai, self.mem))
        if not lean:
            _step("prefetch", lambda: self.prefetch.patch_sos())
        _step("watchdog-reg", lambda: (
            self.watchdog.register(
                "sos",
                health_check=lambda: self.sos._pool.alive > 0,
                restart_fn=lambda: (
                    self.sos._pool.reset()
                    if hasattr(self.sos._pool, "reset") else None
                ),
            ),
            self.watchdog.register(
                "event_bus",
                health_check=lambda: self.event_bus._running,
            ),
            self.watchdog.start(),
        ))
        if not lean:
            _step("discovery", lambda: self.discovery.start())
            _step("metrics", lambda: self.observe.start_metrics_server(9090))
            _step("health", lambda: self.health.start())
            _step("waq", lambda: patch_sos_with_waq(self.sos))
            _step("events", lambda: patch_sos_with_events(self.sos, self.event_bus))
            _step("compress", lambda: patch_sos_with_compression(self.sos))
            _step("stream", lambda: setattr(
                self, "stream", StreamProcessor(self.sos, self.event_bus)))
            _step("view_mgr", lambda: setattr(
                self, "view_mgr", ViewManager(self.sos, self.event_bus)))
            _step("bloom-patch", lambda: self.bloom.patch_sos())
            _step("schema", lambda: self.schema.patch_sos())
            _step("kvcache", lambda: patch_ai_with_kvcache(self.ai, self.sos))
            _step("schema_reg", lambda: self.schema_reg.patch_sos())
            _step("views-patch", lambda: (
                self.views.patch_sos() if hasattr(self.views, "patch_sos") else None
            ))
            _step("eventsrc", lambda: patch_sos_with_event_sourcing(self.sos, self.es_log))
            _step("timelock", lambda: self.timelock.patch_sos())
            _step("classifier", lambda: self.classifier.patch_sos())
            _step("audit", lambda: patch_sos_with_audit(self.sos, self.audit))
            _step("lineage", lambda: patch_sos_with_lineage(self.sos, self.lineage))
            _step("search", lambda: self.search.start())
            _step("advisor", lambda: self.advisor.start())
            _step("agents", lambda: self.agents.start_all())

        for pid, name in [(1, "nova-kernel"), (5, "nova-shell")]:
            self.procs.spawn(name, pid=pid, user="root", cmd=f"[{name}]")
        if not lean:
            for pid, name in [
                (2, "nova-indexer"), (3, "nova-advisor"),
                (4, "nova-agents"), (6, "nova-mdns"),
            ]:
                self.procs.spawn(name, pid=pid, user="root", cmd=f"[{name}]")

        _step("shell", lambda: setattr(self, "shell", NovaShell(self)))
        if self.shell:
            self.agent.shell = self.shell
            self.reviewer.advisor = self.advisor
            if not lean:
                _step("rc", lambda: self.scripting.load_rc(self.shell))
                _step("cron", lambda: self.scripting.start_cron(self.shell))

    def shutdown(self) -> None:
        """Flush SOS WAL and stop background services cleanly."""
        try:
            if getattr(self, "audit", None) and hasattr(self.audit, "flush"):
                self.audit.flush()
        except Exception as exc:
            log.error("audit flush failed during shutdown: %s", exc)
            raise
        try:
            if getattr(self, "watchdog", None):
                self.watchdog.stop()
        except Exception:
            pass
        try:
            if getattr(self, "event_bus", None) and hasattr(self.event_bus, "stop"):
                self.event_bus.stop()
        except Exception:
            pass
        try:
            conn = self.sos._pool.get()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.commit()
        except Exception:
            pass
        try:
            self.sos._pool.reset()
        except Exception:
            pass

    # ── Syscall surface ───────────────────────────────────────────────────────
    # These methods form the stable interface used by shell commands, agents,
    # and plugins.  Prefer kernel.data (flat handles) over path trees.
    """Sys read.

        Args:
        path: Path.
        """
    def sys_read(self, path):
        """Read via DataPlane. Legacy SOS paths are denied under lockdown."""
        from store.dataplane import DataPlaneError
        if "/" not in str(path).lstrip("@"):
            return self.data.get(path).content
        if self.data.enforce:
            raise DataPlaneError("legacy path access denied under lockdown")
        return self.sos.read(path)

    def sys_write(self, path, c, **kw):
        """Write via DataPlane. Legacy SOS paths are denied under lockdown."""
        from store.dataplane import DataPlaneError
        if "/" not in str(path).lstrip("@"):
            rec = self.data.put(path, c, **kw)
            try:
                self.search.index_now(self.data.alias_of(rec.handle))
            except Exception:
                pass
            return rec.oid
        if self.data.enforce:
            raise DataPlaneError("legacy path access denied under lockdown")
        return self.sos.write(path, c, **kw)

    def sys_exec(self, path, args=None):
        """Execute a DataPlane object. Untrusted SOS fallback is not permitted."""
        from store.dataplane import DataPlaneError
        if "/" in str(path).lstrip("@"):
            if self.data.enforce:
                raise DataPlaneError("legacy path exec denied under lockdown")
            content = self.sos.read(path)
        else:
            content = self.data.get(path).content
        ns = {"__name__":"__main__","kernel":self,"sos":self.sos,"data":self.data}
        exec(compile(content, path, "exec"), ns)
    """Sys fork.

        Args:
        name: Name.
        user: User, defaults to 'root'.
        cmd: Cmd, defaults to ''.
        """
    def sys_fork(self, name, user="root", cmd=""): return self.procs.spawn(name,user=user,cmd=cmd)
    """Sys kill.

        Args:
        pid: Pid.
        """
    def sys_kill(self, pid): return self.procs.kill(pid)
    """Sys alloc.

        Args:
        mb: Mb.
        """
    def sys_alloc(self, mb): return self.memory.allocate(mb)

    def stats(self):
        """Return usage statistics."""
        return {
            "version":   self.VERSION,
            "processes": self.procs.count(),
            "search":    self.search.stats(),
            "ai":        self.ai.status(),
            "agents":    self.agents.status_all(),
            "memory_mb": self.memory.stats(),
            "scripting": self.scripting.status(),
            "data":      len(self.data.find(limit=1000)),
        }