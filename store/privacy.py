"""
PyOS NOVA — Differential Privacy
==================================
Adds mathematically-bounded noise to aggregate SQL queries so statistics
can be shared safely without leaking individual records.

Uses the Laplace mechanism: noise drawn from Laplace(0, sensitivity/epsilon).

  sensitivity  = how much one record can change the query result
  epsilon      = privacy budget (smaller = more private, more noise)

Examples:
  SELECT COUNT(*) → sensitivity=1, noise ≈ 1/epsilon
  SELECT AVG(x)   → sensitivity=range(x)/n, noise calibrated accordingly
  SELECT SUM(x)   → sensitivity=max(x), noise calibrated accordingly

Shell commands:
  sql --private "SELECT COUNT(*) FROM objects"
  sql --private --epsilon 0.1 "SELECT AVG(size) FROM objects"
  privacy status        — current epsilon budget
  privacy budget reset  — reset the privacy budget tracker

Usage in Python:
  from store.privacy import DifferentialPrivacy
  dp = DifferentialPrivacy(sos, epsilon=1.0)
  result = dp.query("SELECT COUNT(*) FROM objects")
"""

from __future__ import annotations
import os, sys, json, math, secrets, time
from typing import Optional, Union, Tuple, TYPE_CHECKING

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from store.sos import SemanticObjectStore

BUDGET_PATH = "/security/privacy/budget.json"

# Global privacy budget — each query consumes epsilon
DEFAULT_EPSILON    = 1.0    # standard privacy parameter
DEFAULT_DELTA      = 1e-5   # probability of accidental privacy leak
MAX_BUDGET         = 10.0   # total epsilon budget before warning


def _laplace_noise(sensitivity: float, epsilon: float) -> float:
    """
    Draw noise from the Laplace distribution using the inverse CDF method.

    Args:
        sensitivity (float): Global sensitivity of the query.
        epsilon (float): Privacy parameter (smaller = more private).

    Returns:
        float: Noise value drawn from Laplace(0, sensitivity/epsilon).
    """
    if epsilon <= 0 or sensitivity <= 0:
        return 0.0
    scale = sensitivity / epsilon
    # Use inverse CDF: if U ~ Uniform(-0.5, 0.5), then -scale*sign(U)*log(1-2|U|) ~ Laplace(0,b)
    u = (secrets.randbelow(2**32) / 2**32) - 0.5
    if u == 0:
        return 0.0
    sign = 1 if u > 0 else -1
    return -scale * sign * math.log(1 - 2 * abs(u))


def _gaussian_noise(sensitivity: float, epsilon: float,
                    delta: float = 1e-5) -> float:
    """
    Draw noise from the Gaussian distribution (for (epsilon, delta)-DP).

    Args:
        sensitivity (float): L2 sensitivity of the query.
        epsilon (float): Privacy parameter.
        delta (float): Probability of privacy failure.

    Returns:
        float: Gaussian noise value.
    """
    import math
    if epsilon <= 0 or sensitivity <= 0:
        return 0.0
    # Sigma from analytic Gaussian mechanism
    c    = math.sqrt(2 * math.log(1.25 / delta))
    sigma = c * sensitivity / epsilon
    # Box-Muller transform for standard normal
    u1 = secrets.randbelow(2**32) / 2**32 + 1e-10
    u2 = secrets.randbelow(2**32) / 2**32
    z  = math.sqrt(-2 * math.log(u1)) * math.cos(2 * math.pi * u2)
    return sigma * z


class PrivacyBudget:
    """
    Tracks cumulative privacy budget consumption.
    Warns when the budget is nearly exhausted.
    """

    def __init__(self, sos: "SemanticObjectStore",
                 total_budget: float = MAX_BUDGET):
        """Initialise the privacy budget tracker."""
        self.sos          = sos
        self.total_budget = total_budget
        self._spent       = 0.0
        self._query_log:  list = []
        self._load()

    def _ensure_dirs(self):
        """Create privacy directory."""
        if not self.sos.exists("/security/privacy"):
            self.sos.mkdir("/security/privacy", parents=True)

    def _load(self):
        """Load budget state from SOS."""
        try:
            data = json.loads(self.sos.read(BUDGET_PATH))
            self._spent     = data.get("spent", 0.0)
            self._query_log = data.get("log", [])
        except Exception:
            self._spent     = 0.0
            self._query_log = []

    def _save(self):
        """Persist budget state to SOS."""
        self._ensure_dirs()
        data = {"spent": self._spent, "log": self._query_log[-100:]}
        self.sos.write(BUDGET_PATH, json.dumps(data))

    def consume(self, epsilon: float, query: str = "") -> bool:
        """
        Consume epsilon from the budget.

        Args:
            epsilon (float): Privacy cost of the query.
            query (str): The query being executed (for logging).

        Returns:
            bool: True if budget was available.
        """
        self._spent += epsilon
        self._query_log.append({
            "ts": time.time(), "epsilon": epsilon,
            "query": query[:80], "total_spent": self._spent,
        })
        self._save()
        return self._spent <= self.total_budget

    def remaining(self) -> float:
        """Return remaining privacy budget."""
        return max(0.0, self.total_budget - self._spent)

    def reset(self):
        """Reset the privacy budget."""
        self._spent     = 0.0
        self._query_log = []
        self._save()

    def status(self) -> dict:
        """Return budget status."""
        return {
            "spent":   round(self._spent, 4),
            "total":   self.total_budget,
            "remaining": round(self.remaining(), 4),
            "queries": len(self._query_log),
            "warning": self._spent > self.total_budget * 0.8,
        }


class DifferentialPrivacy:
    """
    Adds differential privacy to SOS SQL queries.
    
    Supports COUNT, SUM, AVG, MIN, MAX with calibrated Laplace noise.
    """

    def __init__(self, sos: "SemanticObjectStore",
                 epsilon: float = DEFAULT_EPSILON,
                 delta: float = DEFAULT_DELTA):
        """
        Initialise differential privacy engine.

        Args:
            sos: The Semantic Object Store.
            epsilon (float): Default privacy parameter per query.
            delta (float): Default failure probability.
        """
        self.sos     = sos
        self.epsilon = epsilon
        self.delta   = delta
        self.budget  = PrivacyBudget(sos)

    def query(self, sql: str, epsilon: float = None,
              sensitivity: float = None) -> Tuple[object, dict]:
        """
        Execute a SQL query with differential privacy noise.

        Args:
            sql (str): The SQL query to execute.
            epsilon (float): Privacy parameter for this query. Defaults to self.epsilon.
            sensitivity (float): Manual sensitivity override.

        Returns:
            Tuple[result, metadata]: Noised result and privacy metadata.
        """
        eps = epsilon or self.epsilon
        conn = self.sos._pool.get()

        try:
            rows = conn.execute(sql).fetchall()
        except Exception as e:
            return None, {"error": str(e)}

        if not rows:
            return [], {"epsilon": eps, "noise": 0, "rows": 0}

        # Detect query type and apply appropriate noise
        sql_upper = sql.strip().upper()
        metadata  = {"epsilon": eps, "query": sql[:80]}

        # Single aggregate value
        if len(rows) == 1 and len(rows[0]) == 1:
            raw_val = rows[0][0]
            if raw_val is None:
                return None, metadata

            # Determine sensitivity
            if sensitivity is None:
                sensitivity = self._estimate_sensitivity(sql_upper, rows)

            noise   = _laplace_noise(sensitivity, eps)
            noised  = raw_val + noise

            # Round for counts
            if "COUNT" in sql_upper:
                noised = max(0, round(noised))

            metadata.update({
                "noise_magnitude": abs(noise),
                "sensitivity": sensitivity,
                "mechanism": "Laplace",
            })
            self.budget.consume(eps, sql)
            return noised, metadata

        # Multiple rows or columns — noise each numeric cell
        result = []
        for row in rows:
            noised_row = []
            for val in row:
                if isinstance(val, (int, float)):
                    sens = sensitivity or 1.0
                    noised_row.append(val + _laplace_noise(sens, eps))
                else:
                    noised_row.append(val)
            result.append(tuple(noised_row))

        self.budget.consume(eps, sql)
        metadata["rows"] = len(result)
        return result, metadata

    def _estimate_sensitivity(self, sql_upper: str,
                               rows) -> float:
        """Estimate the global sensitivity of a query from its type."""
        if "COUNT" in sql_upper:
            return 1.0
        if "SUM" in sql_upper or "AVG" in sql_upper:
            # Sensitivity = max possible value (conservative)
            try:
                val = rows[0][0]
                return max(abs(float(val)) * 2, 1.0)
            except Exception:
                return 100.0
        if "MIN" in sql_upper or "MAX" in sql_upper:
            return 1.0
        return 1.0

    def private_count(self, table: str,
                       where: str = "",
                       epsilon: float = None) -> int:
        """
        Count rows with differential privacy.

        Args:
            table (str): Table name.
            where (str): Optional WHERE clause.
            epsilon (float): Privacy parameter.

        Returns:
            int: Noised count.
        """
        sql = f"SELECT COUNT(*) FROM {table}"
        if where:
            sql += f" WHERE {where}"
        result, _ = self.query(sql, epsilon=epsilon, sensitivity=1.0)
        return max(0, int(round(result or 0)))

    def private_avg(self, table: str, column: str,
                    value_range: float = 1000.0,
                    where: str = "",
                    epsilon: float = None) -> float:
        """
        Compute average with differential privacy.

        Args:
            table (str): Table name.
            column (str): Column to average.
            value_range (float): Expected range of values (for sensitivity).
            where (str): Optional WHERE clause.
            epsilon (float): Privacy parameter.

        Returns:
            float: Noised average.
        """
        n_sql = f"SELECT COUNT(*) FROM {table}" + (f" WHERE {where}" if where else "")
        n_rows, _ = self.query(n_sql, epsilon=(epsilon or self.epsilon) / 2, sensitivity=1.0)
        n = max(1, int(n_rows or 1))

        s_sql = f"SELECT SUM({column}) FROM {table}" + (f" WHERE {where}" if where else "")
        total, _ = self.query(s_sql, epsilon=(epsilon or self.epsilon) / 2,
                               sensitivity=value_range)
        return (total or 0) / n
