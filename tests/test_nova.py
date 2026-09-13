"""
PyOS NOVA — Test Suite
========================
Integration and unit tests for all NOVA subsystems.

Run with::

    pytest tests/ -v
    pytest tests/ -v --cov=. --cov-report=term-missing
    pytest tests/test_nova.py::TestSOS -v   # single class

All tests are self-contained.  Each test that needs a SOS creates a
temporary directory in ``setUp`` and tears it down in ``tearDown``,
so tests can run in any order and in parallel.
"""

import os
import sys
import json
import time
import tempfile
import shutil
import unittest

# Add repo root to path so imports work without installation.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# ─────────────────────────────────────────────────── helpers
def _make_sos(td: str):
    """Return a fresh SemanticObjectStore backed by a temp DB."""
    from store.sos import SemanticObjectStore
    os.environ["NOVA_DATA"] = td
    return SemanticObjectStore(db_path=os.path.join(td, "test.db"))


class _TempDir:
    """Context manager / base for tests that need a temp directory."""

    def setUp(self):
        """Set the up."""
        self.td = tempfile.mkdtemp(prefix="nova_test_")
        os.environ["NOVA_DATA"] = self.td

    def tearDown(self):
        """Tear down."""
        shutil.rmtree(self.td, ignore_errors=True)


# ─────────────────────────────────────────────────── SOS core
class TestSOS(_TempDir, unittest.TestCase):
    """Tests for the Semantic Object Store (store/sos.py)."""

    def setUp(self):
        """Set the up."""
        super().setUp()
        self.sos = _make_sos(self.td)

    # ── write / read ──────────────────────────────────────────────────────────

    def test_write_and_read_text(self):
        """Written text is read back unchanged."""
        self.sos.write("/test/hello.txt", "hello world")
        self.assertEqual(self.sos.read("/test/hello.txt"), "hello world")

    def test_write_bytes(self):
        """Binary content is stored and retrieved correctly."""
        data = bytes(range(256))
        self.sos.write("/test/binary", data, kind="data")
        self.assertEqual(self.sos.read_bytes("/test/binary"), data)

    def test_write_returns_oid(self):
        """write() returns a 32-character hex OID."""
        oid = self.sos.write("/test/f.txt", "content")
        self.assertIsInstance(oid, str)
        self.assertEqual(len(oid), 32)

    def test_read_missing_raises(self):
        """Reading a non-existent path raises FileNotFoundError."""
        with self.assertRaises(FileNotFoundError):
            self.sos.read("/does/not/exist.txt")

    # ── content addressing ────────────────────────────────────────────────────

    def test_identical_content_same_oid(self):
        """Identical content always produces the same OID (deduplication)."""
        oid1 = self.sos.store(b"duplicate content")
        oid2 = self.sos.store(b"duplicate content")
        self.assertEqual(oid1, oid2)

    def test_different_content_different_oid(self):
        """Different content produces different OIDs."""
        oid1 = self.sos.store(b"content A")
        oid2 = self.sos.store(b"content B")
        self.assertNotEqual(oid1, oid2)

    # ── versioning ────────────────────────────────────────────────────────────

    def test_versioning(self):
        """Multiple writes to the same path create a version chain."""
        self.sos.write("/test/versioned.txt", "v1")
        self.sos.write("/test/versioned.txt", "v2")
        self.sos.write("/test/versioned.txt", "v3")
        history = self.sos.history("/test/versioned.txt")
        self.assertEqual(len(history), 3)
        self.assertEqual(history[0].text, "v3")  # newest first
        self.assertEqual(history[2].text, "v1")  # oldest last

    def test_version_numbers(self):
        """Version numbers increment correctly."""
        self.sos.write("/test/v.txt", "first")
        self.sos.write("/test/v.txt", "second")
        history = self.sos.history("/test/v.txt")
        versions = sorted(obj.version for obj in history)
        self.assertEqual(versions, [1, 2])

    def test_checkout_version(self):
        """checkout() restores the correct historical version."""
        self.sos.write("/test/doc.txt", "original")
        self.sos.write("/test/doc.txt", "modified")
        v1 = self.sos.checkout("/test/doc.txt", version=1)
        self.assertIsNotNone(v1)
        self.assertEqual(v1.text, "original")

    # ── exists / stat / listdir ───────────────────────────────────────────────

    def test_exists_true(self):
        """exists() returns True for written paths."""
        self.sos.write("/test/exists.txt", "yes")
        self.assertTrue(self.sos.exists("/test/exists.txt"))

    def test_exists_false(self):
        """exists() returns False for unknown paths."""
        self.assertFalse(self.sos.exists("/no/such/path"))

    def test_listdir(self):
        """listdir() returns direct children only."""
        self.sos.write("/dir/a.txt", "a")
        self.sos.write("/dir/b.txt", "b")
        self.sos.write("/dir/sub/c.txt", "c")
        children = self.sos.listdir("/dir")
        self.assertIn("a.txt", children)
        self.assertIn("b.txt", children)
        self.assertNotIn("sub/c.txt", children)  # not a direct child

    def test_mkdir_and_is_dir(self):
        """mkdir() creates a directory object; is_dir() detects it."""
        self.sos.mkdir("/newdir", parents=True)
        self.assertTrue(self.sos.is_dir("/newdir"))
        self.assertFalse(self.sos.is_file("/newdir"))

    # ── tags ──────────────────────────────────────────────────────────────────

    def test_tag_and_find(self):
        """Tagged objects are found by find_by_tag()."""
        self.sos.write("/test/tagged.txt", "content", tags=["important"])
        paths = self.sos.find_by_tag("important")
        self.assertIn("/test/tagged.txt", paths)

    def test_tag_untag(self):
        """tag() adds; untag() removes; get_tags() reflects changes."""
        self.sos.write("/test/t.txt", "data")
        self.sos.tag("/test/t.txt", "alpha", "beta")
        tags = self.sos.get_tags("/test/t.txt")
        self.assertIn("alpha", tags)
        self.assertIn("beta", tags)
        self.sos.untag("/test/t.txt", "alpha")
        tags = self.sos.get_tags("/test/t.txt")
        self.assertNotIn("alpha", tags)
        self.assertIn("beta", tags)

    # ── search ────────────────────────────────────────────────────────────────

    def test_keyword_search_returns_results(self):
        """keyword_search() returns matching objects."""
        self.sos.write("/test/py.txt", "python programming language")
        self.sos.write("/test/js.txt", "javascript node npm")
        results = self.sos.keyword_search("python programming")
        paths = [r["path"] for r in results]
        self.assertIn("/test/py.txt", paths)

    def test_keyword_search_no_results(self):
        """keyword_search() returns empty list for unknown terms."""
        results = self.sos.keyword_search("xyzzy_no_match_12345")
        self.assertEqual(results, [])

    # ── remove / move / copy ──────────────────────────────────────────────────

    def test_remove(self):
        """remove() deletes the alias; path no longer exists."""
        self.sos.write("/test/del.txt", "bye")
        self.assertTrue(self.sos.exists("/test/del.txt"))
        self.sos.remove("/test/del.txt")
        self.assertFalse(self.sos.exists("/test/del.txt"))

    def test_copy(self):
        """copy() creates a new alias pointing to the same object."""
        self.sos.write("/test/orig.txt", "data")
        self.sos.copy("/test/orig.txt", "/test/copy.txt")
        self.assertEqual(self.sos.read("/test/copy.txt"), "data")

    def test_move(self):
        """move() updates alias; old path gone, new path present."""
        self.sos.write("/test/old.txt", "moving")
        self.sos.move("/test/old.txt", "/test/new.txt")
        self.assertFalse(self.sos.exists("/test/old.txt"))
        self.assertEqual(self.sos.read("/test/new.txt"), "moving")

    # ── cache ─────────────────────────────────────────────────────────────────

    def test_path_cache_hit(self):
        """Repeated resolve() calls hit the LRU cache (no DB round-trip)."""
        self.sos.write("/cache/test.txt", "cached")
        # first call populates cache
        oid1 = self.sos.resolve("/cache/test.txt")
        # second call must return same result
        oid2 = self.sos.resolve("/cache/test.txt")
        self.assertEqual(oid1, oid2)
        self.assertIn("/cache/test.txt", self.sos._path_cache)

    def test_cache_invalidated_on_remove(self):
        """Cache entry is invalidated after remove()."""
        self.sos.write("/cache/gone.txt", "here")
        self.sos.resolve("/cache/gone.txt")          # populate cache
        self.sos.remove("/cache/gone.txt")
        self.assertNotIn("/cache/gone.txt", self.sos._path_cache)

    # ── df ────────────────────────────────────────────────────────────────────

    def test_df_returns_string(self):
        """df() returns a non-empty statistics string."""
        self.sos.write("/test/df.txt", "x" * 100)
        result = self.sos.df()
        self.assertIsInstance(result, str)
        self.assertIn("Objects", result)

    # ── graph links ───────────────────────────────────────────────────────────

    def test_relate_and_related(self):
        """relate() creates a link; related() retrieves it."""
        self.sos.write("/test/a.txt", "a")
        self.sos.write("/test/b.txt", "b")
        self.sos.relate("/test/a.txt", "/test/b.txt", "depends_on")
        links = self.sos.related("/test/a.txt")
        found = any(path == "/test/b.txt" and rel == "depends_on"
                    for path, rel in links)
        self.assertTrue(found)


# ─────────────────────────────────────────────────── SOS branches
class TestBranches(_TempDir, unittest.TestCase):
    """Tests for git-like SOS branching (store/branches.py)."""

    def setUp(self):
        """Set the up."""
        super().setUp()
        self.sos     = _make_sos(self.td)
        from store.branches import BranchManager
        self.bm = BranchManager(self.sos)

    def test_create_branch(self):
        """create_branch() produces a Branch with a snapshot of current paths."""
        self.sos.write("/test/file.txt", "content")
        branch = self.bm.create_branch("dev")
        self.assertEqual(branch.name, "dev")
        self.assertIsInstance(branch.tips, dict)

    def test_list_branches(self):
        """list_branches() includes the branch just created."""
        self.bm.create_branch("feature")
        names = [b.name for b in self.bm.list_branches()]
        self.assertIn("feature", names)

    def test_duplicate_branch_raises(self):
        """Creating a branch with an existing name raises ValueError."""
        self.bm.create_branch("dupe")
        with self.assertRaises(ValueError):
            self.bm.create_branch("dupe")

    def test_diff_produces_output(self):
        """diff() returns a non-empty string when versions differ."""
        self.sos.write("/test/v.txt", "line one\n")
        self.sos.write("/test/v.txt", "line one\nline two\n")
        diff = self.bm.diff("/test/v.txt")
        self.assertIsInstance(diff, str)
        self.assertGreater(len(diff), 0)

    def test_diff_no_change(self):
        """diff() indicates no differences when only one version exists."""
        self.sos.write("/test/single.txt", "only version")
        diff = self.bm.diff("/test/single.txt")
        self.assertIn("one version", diff.lower())


# ─────────────────────────────────────────────────── Crypto
class TestCrypto(_TempDir, unittest.TestCase):
    """Tests for per-object encryption (store/crypto.py)."""

    def setUp(self):
        """Set the up."""
        super().setUp()
        self.sos = _make_sos(self.td)
        from store.crypto import CryptoEngine
        self.crypto = CryptoEngine(self.sos)

    def test_init_and_unlock(self):
        """init() creates a key; unlock() with correct password returns True."""
        self.crypto.init("test_password_123")
        self.assertFalse(self.crypto.locked)
        self.crypto.lock()
        self.assertTrue(self.crypto.locked)
        ok = self.crypto.unlock("test_password_123")
        self.assertTrue(ok)
        self.assertFalse(self.crypto.locked)

    def test_wrong_password(self):
        """unlock() with wrong password returns False."""
        self.crypto.init("correct")
        self.crypto.lock()
        ok = self.crypto.unlock("wrong")
        self.assertFalse(ok)
        self.assertTrue(self.crypto.locked)

    def test_encrypt_decrypt_roundtrip(self):
        """encrypt() → decrypt() produces the original bytes."""
        self.crypto.init("pw")
        data = b"secret message 1234567890"
        blob = self.crypto.encrypt(data)
        self.assertIsNotNone(blob)
        self.assertNotEqual(blob, data)
        recovered = self.crypto.decrypt(blob)
        self.assertEqual(recovered, data)

    def test_encrypted_different_each_time(self):
        """Two encryptions of the same plaintext produce different blobs (random nonce)."""
        self.crypto.init("pw")
        blob1 = self.crypto.encrypt(b"same")
        blob2 = self.crypto.encrypt(b"same")
        self.assertNotEqual(blob1, blob2)

    def test_is_encrypted_detection(self):
        """is_encrypted() correctly identifies encrypted blobs."""
        self.crypto.init("pw")
        blob = self.crypto.encrypt(b"data")
        self.assertTrue(self.crypto.is_encrypted(blob))
        self.assertFalse(self.crypto.is_encrypted(b"plain text"))

    def test_decrypt_when_locked_returns_none(self):
        """decrypt() returns None when the engine is locked."""
        self.crypto.init("pw")
        blob = self.crypto.encrypt(b"data")
        self.crypto.lock()
        result = self.crypto.decrypt(blob)
        self.assertIsNone(result)


# ─────────────────────────────────────────────────── AI — memory
class TestAIMemory(_TempDir, unittest.TestCase):
    """Tests for the AI memory layer (ai/memory.py)."""

    def setUp(self):
        """Set the up."""
        super().setUp()
        self.sos = _make_sos(self.td)
        from ai.memory import MemoryManager
        self.mm = MemoryManager(self.sos)

    def test_add_and_retrieve(self):
        """add() stores a memory; all() returns it."""
        m = self.mm.add("User prefers Python 3.11", kind="preference")
        mems = self.mm.all()
        self.assertTrue(any(x.mid == m.mid for x in mems))

    def test_forget(self):
        """forget() removes the memory from all()."""
        m = self.mm.add("temporary fact")
        self.mm.forget(m.mid)
        mems = self.mm.all()
        self.assertFalse(any(x.mid == m.mid for x in mems))

    def test_search_relevance(self):
        """search() returns the most relevant memories first."""
        self.mm.add("User works with Python", kind="fact", importance=0.9)
        self.mm.add("User likes coffee", kind="preference", importance=0.5)
        results = self.mm.search("Python programming")
        self.assertGreater(len(results), 0)
        # Python memory should appear
        texts = [m.text for m in results]
        self.assertTrue(any("Python" in t for t in texts))

    def test_build_context_empty(self):
        """build_context() returns empty string when no memories exist."""
        ctx = self.mm.build_context("anything")
        self.assertEqual(ctx, "")

    def test_build_context_nonempty(self):
        """build_context() returns a non-empty string when relevant memories exist."""
        self.mm.add("User name is Alice", kind="fact", importance=0.9)
        ctx = self.mm.build_context("what is my name")
        self.assertGreater(len(ctx), 0)

    def test_stats(self):
        """stats() returns a dict with total count."""
        self.mm.add("fact one"); self.mm.add("fact two")
        s = self.mm.stats()
        self.assertGreaterEqual(s["total"], 2)
        self.assertIn("by_kind", s)

    def test_clear(self):
        """clear() removes all memories."""
        self.mm.add("m1"); self.mm.add("m2")
        self.mm.clear()
        self.assertEqual(len(self.mm.all()), 0)


# ─────────────────────────────────────────────────── AI — reviewer
class TestReviewer(unittest.TestCase):
    """Tests for static code review (ai/reviewer.py)."""

    def setUp(self):
        """Set the up."""
        from ai.reviewer import StaticReviewer
        self.r = StaticReviewer()

    def test_no_issues_clean_code(self):
        """Clean, minimal code produces no findings."""
        code = 'def add(a: int, b: int) -> int:\n    """Add two numbers."""\n    return a + b\n'
        issues = self.r.review(code)
        self.assertEqual(issues, [])

    def test_detects_eval(self):
        """eval() is flagged as a dangerous call."""
        issues = self.r.review("eval(input())\n")
        self.assertTrue(any("eval" in i.message for i in issues))

    def test_detects_bare_except(self):
        """Bare except: clause is flagged."""
        code = "try:\n    pass\nexcept:\n    pass\n"
        issues = self.r.review(code)
        self.assertTrue(any("bare" in i.message.lower() for i in issues))

    def test_detects_mutable_default(self):
        """Mutable default argument is flagged."""
        code = "def f(items=[]):\n    pass\n"
        issues = self.r.review(code)
        self.assertTrue(any("mutable" in i.message.lower() for i in issues))

    def test_detects_syntax_error(self):
        """Syntax errors produce an error-severity finding."""
        issues = self.r.review("def broken(\n")
        self.assertTrue(any(i.severity == "error" for i in issues))

    def test_detects_hardcoded_secret(self):
        """Hardcoded password pattern is flagged."""
        code = 'password = "SuperSecret123"\n'
        issues = self.r.review(code)
        self.assertTrue(any("secret" in i.message.lower() or
                             "credential" in i.message.lower()
                             for i in issues))

    def test_severity_ordering(self):
        """Results are sorted with highest severity first."""
        code = 'password = "pw"\neval("x")\n'
        issues = self.r.review(code)
        if len(issues) > 1:
            from ai.reviewer import Issue
            severities = [Issue.SEVERITY[i.severity] for i in issues]
            self.assertEqual(severities, sorted(severities, reverse=True))


# ─────────────────────────────────────────────────── System Doctor
class TestSystemDoctor(_TempDir, unittest.TestCase):
    """Tests for the System Doctor (system/doctor.py)."""

    def setUp(self):
        """Set the up."""
        super().setUp()
        self.sos = _make_sos(self.td)
        # Minimal kernel stub — enough for the doctor to function.
        class _FakeAI:
            """Fake a i."""
            tier = "rag"
            """Ask."""
            def ask(self, *a, **kw): return ""
        class _FakeKernel:
            """Fake kernel."""
            def __init__(self, sos):
                """Initialise the instance."""
                self.sos = sos
                self.ai  = _FakeAI()
        from system.doctor import SystemDoctor
        self.kernel = _FakeKernel(self.sos)
        self.doctor = SystemDoctor(self.kernel)

    def test_analyse_returns_proposal(self):
        """analyse() returns a FixProposal for any query."""
        from system.doctor import FixProposal
        p = self.doctor.analyse("disk space is running low")
        self.assertIsInstance(p, FixProposal)
        self.assertGreater(len(p.steps), 0)
        self.assertIsNotNone(p.diagnosis)

    def test_proposal_has_risk(self):
        """Every proposal has a valid overall_risk level."""
        p = self.doctor.analyse("slow performance")
        self.assertIn(p.overall_risk, ("low", "medium", "high", "critical"))

    def test_ledger_save_load(self):
        """Proposals survive a save/load round-trip."""
        p = self.doctor.analyse("test query")
        self.doctor.ledger.save(p)
        loaded = self.doctor.ledger.load(p.fix_id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.fix_id, p.fix_id)
        self.assertEqual(loaded.query, p.query)

    def test_list_fixes(self):
        """list_fixes() includes saved proposals."""
        p = self.doctor.analyse("test")
        self.doctor.ledger.save(p)
        fixes = self.doctor.list_fixes()
        self.assertTrue(any(f.fix_id == p.fix_id for f in fixes))

    def test_dry_run_step(self):
        """execute_step() with dry_run=True returns True and makes no changes."""
        from system.doctor import FixStep
        step = FixStep(
            step_id="dryrun_s01", kind="cmd",
            description="list files",
            forward={"command": "ls /"},
            rollback={"note": "no rollback"},
            risk="low",
        )
        ok, out = self.doctor.executor.execute_step("dry_fix", step, dry_run=True)
        self.assertTrue(ok)
        self.assertIn("dry-run", out)

    def test_file_write_and_rollback(self):
        """A file_write step can be rolled back via SOS snapshot."""
        from system.doctor import FixStep, FixLedger, FixExecutor
        # Write original file
        self.sos.write("/test/config.json", '{"setting": "original"}')
        step = FixStep(
            step_id="fw_s01", kind="file_write",
            description="update config",
            forward={"path": "/test/config.json",
                     "content": '{"setting": "modified"}'},
            rollback={"restore_previous_version": True},
            risk="medium",
        )
        ledger   = FixLedger(self.sos)
        executor = FixExecutor(self.sos, ledger)
        fix_id   = "test_fix_001"

        # Apply
        ok, _ = executor.execute_step(fix_id, step)
        self.assertTrue(ok)
        self.assertIn("modified", self.sos.read("/test/config.json"))

        # Rollback
        step.executed = True
        ok, _ = executor.rollback_step(fix_id, step)
        self.assertTrue(ok)
        self.assertIn("original", self.sos.read("/test/config.json"))


# ─────────────────────────────────────────────────── Shell scripting
class TestShellScripting(_TempDir, unittest.TestCase):
    """Tests for the shell scripting engine (shell/scripting.py)."""

    def setUp(self):
        """Set the up."""
        super().setUp()
        self.sos = _make_sos(self.td)
        from shell.scripting import ScriptingEngine, _parse_interval
        self.se = ScriptingEngine(self.sos)
        self._pi = _parse_interval

    def test_alias_parsing(self):
        """Aliases are parsed correctly from RC content."""
        self.se.parse("alias ll = ls -la\nalias py = python3\n")
        self.assertEqual(self.se.aliases.get("ll"), "ls -la")
        self.assertEqual(self.se.aliases.get("py"), "python3")

    def test_function_parsing(self):
        """Function definitions are parsed from RC content."""
        self.se.parse("def greet(name):\n    echo Hello $name\n")
        fn = self.se.get_function("greet")
        self.assertIsNotNone(fn)
        self.assertEqual(fn.params, ["name"])
        self.assertEqual(fn.body, ["    echo Hello $name"])

    def test_interval_parsing(self):
        """_parse_interval() converts shorthand correctly."""
        self.assertEqual(self._pi("30s"), 30.0)
        self.assertEqual(self._pi("5m"),  300.0)
        self.assertEqual(self._pi("2h"),  7200.0)
        self.assertEqual(self._pi("1d"),  86400.0)

    def test_expand_alias(self):
        """expand_alias() returns the aliased command."""
        self.se.parse("alias myls = ls -la /home\n")
        self.assertEqual(self.se.expand_alias("myls"), "ls -la /home")

    def test_expand_alias_missing(self):
        """expand_alias() returns None for unknown aliases."""
        self.assertIsNone(self.se.expand_alias("unknown_cmd"))

    def test_has_callable(self):
        """has_callable() is True for defined functions and aliases."""
        self.se.parse("alias x = echo hi\ndef myfunc():\n    pass\n")
        self.assertTrue(self.se.has_callable("x"))
        self.assertTrue(self.se.has_callable("myfunc"))
        self.assertFalse(self.se.has_callable("not_defined"))


# ─────────────────────────────────────────────────── Search
class TestSearch(_TempDir, unittest.TestCase):
    """Tests for neural search (search/neural.py)."""

    def setUp(self):
        """Set the up."""
        super().setUp()
        try:
            import numpy  # noqa: F401
            self._has_numpy = True
        except ImportError:
            self._has_numpy = False

    def test_bm25_basic(self):
        """BM25 returns documents containing query terms."""
        from search.neural import BM25
        bm = BM25()
        bm.add("doc1", "python operating system kernel")
        bm.add("doc2", "javascript node npm frontend")
        results = bm.query("python kernel", k=2)
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0][0], "doc1")

    def test_bm25_empty_query(self):
        """BM25 returns empty list for an empty query."""
        from search.neural import BM25
        bm = BM25()
        bm.add("doc1", "some content")
        results = bm.query("", k=5)
        self.assertEqual(results, [])

    @unittest.skipUnless(
        __import__("importlib").util.find_spec("numpy"),
        "numpy not installed",
    )
    def test_embedder_produces_vector(self):
        """CharNgramEmbedder produces a 512-dimensional vector."""
        from search.neural import CharNgramEmbedder
        emb = CharNgramEmbedder()
        vec = emb.embed("hello world python")
        self.assertIsNotNone(vec)
        self.assertEqual(len(vec), 512)

    @unittest.skipUnless(
        __import__("importlib").util.find_spec("numpy"),
        "numpy not installed",
    )
    def test_hnsw_add_and_query(self):
        """HNSW index returns nearest neighbours after insertion."""
        from search.neural import HNSWIndex, CharNgramEmbedder
        emb = CharNgramEmbedder()
        idx = HNSWIndex(512)
        docs = ["python os", "javascript web", "rust systems", "python kernel"]
        for i, doc in enumerate(docs):
            vec = emb.embed(doc)
            if vec:
                idx.add(f"doc{i}", vec)
        query = emb.embed("python")
        results = idx.query(query, k=2)
        self.assertGreater(len(results), 0)
        # python-related docs should score higher
        ids = [r[0] for r in results]
        self.assertTrue(any("0" in i or "3" in i for i in ids))


# ─────────────────────────────────────────────────── Plugin manager
class TestPlugins(_TempDir, unittest.TestCase):
    """Tests for the plugin manager (plugins/manager.py)."""

    def setUp(self):
        """Set the up."""
        super().setUp()
        sos = _make_sos(self.td)
        class _FakeKernel:
            """Initialise the instance."""
            """Fake kernel."""
            def __init__(s): s.sos = sos; s.ai = type("A",(),({"tier":"rag","ask":lambda *a,**k:""}))()
        from plugins.manager import PluginManager
        self.pm = PluginManager(_FakeKernel())

    def test_install_builtin(self):
        """Installing a built-in app succeeds."""
        ok = self.pm.install("calculator")
        self.assertTrue(ok)

    def test_installed_list(self):
        """Installed app appears in installed()."""
        self.pm.install("notes")
        names = [a.name for a in self.pm.installed()]
        self.assertIn("notes", names)

    def test_remove(self):
        """remove() unregisters the app."""
        self.pm.install("clock")
        ok = self.pm.remove("clock")
        self.assertTrue(ok)
        names = [a.name for a in self.pm.installed()]
        self.assertNotIn("clock", names)

    def test_has_command_after_install(self):
        """Command is registered after install."""
        self.pm.install("calculator")
        self.assertTrue(self.pm.has_command("calc"))

    def test_search_builtin(self):
        """search() finds built-in apps by name and description."""
        results = self.pm.search("calc")
        self.assertTrue(any(r["name"] == "calculator" for r in results))

    def test_remove_unknown_returns_false(self):
        """Removing an unknown app returns False without raising."""
        ok = self.pm.remove("nonexistent_app_xyz")
        self.assertFalse(ok)


# ─────────────────────────────────────────────────── Security
class TestSecurity(_TempDir, unittest.TestCase):
    """Tests for the security auditor (security/audit.py)."""

    def setUp(self):
        """Set the up."""
        super().setUp()
        sos = _make_sos(self.td)
        class _FK:
            """Initialise the instance."""
            """F k."""
            def __init__(s): s.sos = sos; s.ai = type("A",(),({"tier":"rag"}))()
        from security.audit import SecurityAuditor
        self.auditor = SecurityAuditor(_FK())

    def test_audit_quick_runs(self):
        """Quick audit completes without exception."""
        findings = self.auditor.audit("quick")
        self.assertIsInstance(findings, list)

    def test_score_range(self):
        """Score is between 0 and 100 inclusive."""
        self.auditor.audit("quick")
        score = self.auditor.score()
        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 100)

    def test_report_is_string(self):
        """report() returns a non-empty string."""
        self.auditor.audit("quick")
        report = self.auditor.report()
        self.assertIsInstance(report, str)
        self.assertGreater(len(report), 0)


# ─────────────────────────────────────────────────── EFI builder
class TestEFIBuilder(unittest.TestCase):
    """Tests for the pure-Python EFI binary generator (boot/efi_builder.py)."""

    def test_builds_valid_pe32plus(self):
        """build_efi() produces a valid PE32+ binary."""
        import struct, tempfile, os
        from boot.efi_builder import build_efi
        with tempfile.NamedTemporaryFile(suffix=".efi", delete=False) as f:
            path = f.name
        try:
            size = build_efi(path, verify=True)
            self.assertGreater(size, 0)
            data = open(path, "rb").read()
            # MZ signature
            self.assertEqual(data[:2], b"MZ")
            # PE signature at e_lfanew offset
            pe_off = struct.unpack_from("<I", data, 60)[0]
            self.assertEqual(data[pe_off:pe_off+4], b"PE\x00\x00")
            # AMD64 machine type
            machine = struct.unpack_from("<H", data, pe_off+4)[0]
            self.assertEqual(machine, 0x8664)
            # EFI Application subsystem
            subsys  = struct.unpack_from("<H", data, pe_off+24+68)[0]
            self.assertEqual(subsys, 0x000A)
        finally:
            os.unlink(path)

    def test_different_message_different_binary(self):
        """Different boot messages produce different binaries."""
        import tempfile, os
        from boot.efi_builder import build_efi, _utf16le
        msg_a = _utf16le("Hello NOVA")
        msg_b = _utf16le("Hello World")
        with tempfile.NamedTemporaryFile(suffix=".efi", delete=False) as f:
            pa = f.name
        with tempfile.NamedTemporaryFile(suffix=".efi", delete=False) as f:
            pb = f.name
        try:
            build_efi(pa, message=msg_a, verify=False)
            build_efi(pb, message=msg_b, verify=False)
            self.assertNotEqual(
                open(pa, "rb").read(),
                open(pb, "rb").read(),
            )
        finally:
            os.unlink(pa); os.unlink(pb)


if __name__ == "__main__":
    unittest.main(verbosity=2)
