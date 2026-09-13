"""
PyOS NOVA — Natural Language SOS Query
========================================
Query the SOS using plain English. The AI translates intent into
the most efficient query — SQL, FTS5, tag search, or vector search.

Examples:
  find all Python files modified this week
  → SELECT oid FROM aliases JOIN objects USING(oid)
    WHERE path LIKE '%.py' AND created_at > (strftime('%s','now')-604800)

  show me large files tagged important
  → Tag search: 'important', filter by size > 100KB

  what files mention authentication
  → FTS5: SELECT oid, snippet(...) FROM fts WHERE fts MATCH 'authentication'

  files I wrote yesterday about networking
  → Combined: recent + FTS + kind filter

Shell commands:
  find <natural language query>    — search in natural language
  nl <query>                       — alias for find
  find --explain <query>           — show the generated query before running
"""

from __future__ import annotations
import os, sys, re, time, json
from typing import List, Dict, Optional, Tuple, TYPE_CHECKING

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from store.sos import SemanticObjectStore
    from ai.engine import AIEngine

QUERY_CACHE_PATH = "/search/nl_cache"

NL_SYSTEM_PROMPT = """You are a SQL query generator for PyOS NOVA's Semantic Object Store.

The database has these tables:
  objects(oid TEXT, content BLOB, kind TEXT, meta_json TEXT, tags_json TEXT,
          links_json TEXT, parent_oid TEXT, version INTEGER, created_at REAL, size INTEGER)
  aliases(path TEXT, oid TEXT, is_dir INTEGER)
  tag_index(tag TEXT, oid TEXT)
  fts(oid, content)  -- FTS5 virtual table, use: fts MATCH 'term'

Given a natural language query, respond with JSON:
{
  "sql": "SELECT ... (valid SQLite, use aliases JOIN objects USING(oid) for path queries)",
  "fts_terms": ["term1", "term2"],  // optional FTS terms to also search
  "tags": ["tag1"],                 // optional tag filter
  "explanation": "one sentence explaining the query"
}

Rules:
- Always include path in SELECT when querying aliases
- Use created_at for time filters (Unix timestamp, strftime for date math)
- LIMIT results to 50 unless asked for more
- For content search, prefer fts MATCH over LIKE
- kind values: text, code, data, dir
- Return ONLY the JSON object, no markdown"""


class QueryPlan:
    """A structured query plan generated from natural language."""

    def __init__(self, nl_query: str, sql: str,
                 fts_terms: List[str] = None,
                 tags: List[str] = None,
                 explanation: str = ""):
        """Initialise a query plan."""
        self.nl_query    = nl_query
        self.sql         = sql
        self.fts_terms   = fts_terms or []
        self.tags        = tags or []
        self.explanation = explanation

    def to_dict(self) -> dict:
        """Serialize to dict."""
        return {
            "nl_query": self.nl_query, "sql": self.sql,
            "fts_terms": self.fts_terms, "tags": self.tags,
            "explanation": self.explanation,
        }

    @staticmethod
    def from_dict(d: dict) -> "QueryPlan":
        """Deserialize from dict."""
        return QueryPlan(**d)


class NLQueryEngine:
    """
    Natural language query engine for the SOS.
    
    Translates plain-English queries into SQL + FTS5 + tag searches,
    executes them, and returns formatted results.
    """

    # Heuristic patterns for fast local translation (no AI needed)
    HEURISTIC_RULES = [
        # Python files
        (r"\bpython\s+files?\b",
         "SELECT path, size, created_at FROM aliases JOIN objects USING(oid) "
         "WHERE path LIKE '%.py' AND kind != 'dir' LIMIT 50"),
        # Large files
        (r"\blarge\s+files?\b|\bfiles?\s+over\s+(\d+)\s*(kb|mb)?\b",
         "SELECT path, size FROM aliases JOIN objects USING(oid) "
         "WHERE size > 102400 AND kind != 'dir' ORDER BY size DESC LIMIT 20"),
        # Recent files (last 24h)
        (r"\brecent\s+files?\b|\btoday\b|\blast\s+24\s+hours?\b",
         "SELECT path, created_at FROM aliases JOIN objects USING(oid) "
         "WHERE created_at > (strftime('%s','now') - 86400) AND kind != 'dir' "
         "ORDER BY created_at DESC LIMIT 30"),
        # Recent week
        (r"\bthis\s+week\b|\blast\s+7\s+days?\b|\bweek\b",
         "SELECT path, created_at FROM aliases JOIN objects USING(oid) "
         "WHERE created_at > (strftime('%s','now') - 604800) AND kind != 'dir' "
         "ORDER BY created_at DESC LIMIT 50"),
        # Code files
        (r"\bcode\s+files?\b|\bscripts?\b",
         "SELECT path, size FROM aliases JOIN objects USING(oid) "
         "WHERE kind = 'code' LIMIT 50"),
        # Directories
        (r"\bdirector(?:y|ies)\b|\bfolders?\b",
         "SELECT path FROM aliases JOIN objects USING(oid) WHERE kind = 'dir'"),
        # Count
        (r"\bhow\s+many\s+(?:files?|objects?)\b|\bcount\b",
         "SELECT COUNT(*) as count, SUM(size) as total_bytes FROM objects WHERE kind != 'dir'"),
        # Largest files
        (r"\bbiggest|largest\s+files?\b",
         "SELECT path, size FROM aliases JOIN objects USING(oid) "
         "WHERE kind != 'dir' ORDER BY size DESC LIMIT 10"),
        # Encrypted / secret
        (r"\bencrypted?\b|\bsecret\s+files?\b",
         "SELECT path FROM aliases JOIN objects o USING(oid) "
         "JOIN tag_index ti ON o.oid = ti.oid "
         "WHERE ti.tag IN ('secret','encrypted') LIMIT 30"),
    ]

    def __init__(self, sos: "SemanticObjectStore",
                 ai: Optional["AIEngine"] = None):
        """Initialise the NL query engine."""
        self.sos   = sos
        self.ai    = ai
        self._cache: Dict[str, QueryPlan] = {}
        self._load_cache()

    def _load_cache(self):
        """Load query plan cache from SOS."""
        try:
            for name in self.sos.listdir(QUERY_CACHE_PATH):
                path = f"{QUERY_CACHE_PATH}/{name}"
                data = json.loads(self.sos.read(path))
                self._cache[data["nl_query"]] = QueryPlan.from_dict(data)
        except Exception:
            pass

    def _save_cache(self, plan: QueryPlan):
        """Cache a query plan to SOS."""
        try:
            if not self.sos.exists(QUERY_CACHE_PATH):
                self.sos.mkdir(QUERY_CACHE_PATH, parents=True)
            import hashlib
            key  = hashlib.sha256(plan.nl_query.encode()).hexdigest()[:16]
            path = f"{QUERY_CACHE_PATH}/{key}"
            self.sos.write(path, json.dumps(plan.to_dict()),
                           tags=["nl-cache"])
            self._cache[plan.nl_query] = plan
        except Exception:
            pass

    def translate(self, nl_query: str) -> QueryPlan:
        """
        Translate a natural language query to a QueryPlan.

        First tries heuristic rules for common patterns, then
        falls back to the AI engine if available.

        Args:
            nl_query (str): The natural language query.

        Returns:
            QueryPlan: The generated query plan.
        """
        # Check cache
        cached = self._cache.get(nl_query.lower())
        if cached:
            return cached

        # Try heuristic rules
        plan = self._heuristic_translate(nl_query)
        if plan:
            return plan

        # Try AI translation
        if self.ai and self.ai.tier != "rag":
            plan = self._ai_translate(nl_query)
            if plan:
                self._save_cache(plan)
                return plan

        # Fallback: FTS search on the query terms
        words = re.sub(r'[^\w\s]', '', nl_query).split()
        fts_terms = [w for w in words if len(w) > 3]
        return QueryPlan(
            nl_query    = nl_query,
            sql         = ("SELECT path, size FROM aliases JOIN objects USING(oid) "
                           "WHERE kind != 'dir' LIMIT 30"),
            fts_terms   = fts_terms,
            explanation = f"FTS search for: {', '.join(fts_terms)}",
        )

    def _heuristic_translate(self, query: str) -> Optional[QueryPlan]:
        """Try to match against known patterns."""
        q_lower = query.lower()
        for pattern, sql in self.HEURISTIC_RULES:
            if re.search(pattern, q_lower):
                # Extract any FTS terms from the query
                stop_words = {"files", "show", "me", "all", "the", "find",
                              "list", "give", "what", "are", "is", "any"}
                words      = set(q_lower.split()) - stop_words
                fts_terms  = [w for w in words if len(w) > 3
                               and not re.match(r'^(file|code|text|dir)', w)]
                return QueryPlan(
                    nl_query    = query,
                    sql         = sql,
                    fts_terms   = fts_terms[:3],
                    explanation = f"Matched pattern: {pattern}",
                )
        return None

    def _ai_translate(self, query: str) -> Optional[QueryPlan]:
        """Use AI to translate a query."""
        try:
            raw = self.ai.ask(
                f"Natural language query: {query}",
                system_key="assistant",
                max_tokens=300,
            )
            # Strip markdown fences
            raw = re.sub(r"^```(?:json)?\n?", "", raw.strip())
            raw = re.sub(r"\n?```$", "", raw)
            data = json.loads(raw)
            return QueryPlan(
                nl_query    = query,
                sql         = data.get("sql", ""),
                fts_terms   = data.get("fts_terms", []),
                tags        = data.get("tags", []),
                explanation = data.get("explanation", "AI-generated query"),
            )
        except Exception:
            return None

    def execute(self, nl_query: str,
                explain: bool = False) -> Tuple[List[dict], QueryPlan]:
        """
        Translate and execute a natural language query.

        Args:
            nl_query (str): The query in plain English.
            explain (bool): If True, return the plan without executing.

        Returns:
            Tuple[List[dict], QueryPlan]: (results, query_plan)
        """
        plan = self.translate(nl_query)

        if explain:
            return [], plan

        conn    = self.sos._pool.get()
        results = []

        # Execute main SQL
        if plan.sql:
            try:
                rows = conn.execute(plan.sql).fetchall()
                for row in rows:
                    results.append(dict(row))
            except Exception as e:
                results.append({"error": str(e), "sql": plan.sql})

        # Execute FTS search
        if plan.fts_terms:
            try:
                fts_query = " OR ".join(f'"{t}"' for t in plan.fts_terms)
                fts_rows  = conn.execute(
                    "SELECT oid, snippet(fts,1,'>>','<<','...',8) AS snip "
                    "FROM fts WHERE fts MATCH ? LIMIT 20",
                    (fts_query,)
                ).fetchall()
                seen_oids = {r.get("oid") for r in results}
                for row in fts_rows:
                    if row["oid"] not in seen_oids:
                        obj = self.sos.get(row["oid"])
                        if obj:
                            results.append({
                                "path":    obj.meta.get("path", row["oid"]),
                                "snippet": row["snip"],
                                "oid":     row["oid"],
                            })
            except Exception:
                pass

        # Tag filter
        for tag in plan.tags:
            tag_oids = {r.oid for r in self.sos.find_by_tag(tag)}
            results  = [r for r in results if r.get("oid") in tag_oids]

        return results[:50], plan

    def format_results(self, results: List[dict],
                       plan: QueryPlan) -> str:
        """
        Format query results for terminal output.

        Args:
            results (List[dict]): Query results.
            plan (QueryPlan): The query plan (for explanation).

        Returns:
            str: Formatted output string.
        """
        lines = [
            f"\n  Query: {plan.nl_query}",
            f"  Plan : {plan.explanation}",
            f"  Found: {len(results)} result(s)",
            "  " + "─" * 55,
        ]
        if not results:
            lines.append("  (no results)")
        for r in results[:30]:
            path    = r.get("path", r.get("oid", "?"))
            size    = r.get("size")
            snippet = r.get("snippet", "")
            error   = r.get("error")
            if error:
                lines.append(f"  Error: {error}")
                continue
            size_str = f"  {size//1024}KB" if size else ""
            lines.append(f"  {path}{size_str}")
            if snippet:
                lines.append(f"    …{snippet}…")
        return "\n".join(lines) + "\n"
