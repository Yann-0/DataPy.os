"""
PyOS NOVA — Resilience & Observability Bundle
===============================================
Four reliability patterns in one module:

1. Circuit Breaker
   Open after N failures in window, half-open after recovery time.
   Prevents cascade failures in distributed NOVA networks.

2. Rate Limiter
   Token bucket algorithm for the REST API.
   Per-IP and global limits, configurable burst.

3. Retry with exponential backoff + jitter
   Wraps any callable with intelligent retry logic.

4. Distributed Tracing
   OpenTelemetry-like spans across kernel, SOS, AI, and network.
   Trace IDs propagate via HTTP headers.
   Gantt view stored as time-series in the SOS.

Shell commands:
  circuit list              — list circuit breaker states
  circuit reset <name>      — force-close a breaker
  ratelimit status          — show rate limit states
  ratelimit set <ip> <rps>  — set per-IP rate limit
  traces list               — show recent traces
  traces show <trace_id>    — show trace Gantt
  traces clear              — clear trace history
"""

from __future__ import annotations
import os, sys, time, threading, functools, uuid, json
from typing import Callable, Optional, Dict, List, Any, TYPE_CHECKING
from dataclasses import dataclass, field
from enum import Enum

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from store.sos import SemanticObjectStore


# ─────────────────────────────────────────────────── Circuit Breaker

class CircuitState(Enum):
    """Circuit breaker state."""
    CLOSED    = "closed"     # normal operation
    OPEN      = "open"       # refusing requests
    HALF_OPEN = "half_open"  # probing for recovery


@dataclass
class CircuitBreaker:
    """
    Circuit breaker to prevent cascade failures.

    States:
      CLOSED    → allows all requests
      OPEN      → rejects all requests (fail fast)
      HALF_OPEN → allows one probe request
    """

    name:           str
    failure_threshold: int   = 5
    recovery_timeout:  float = 60.0
    _state:          CircuitState = field(default=CircuitState.CLOSED,
                                           init=False)
    _failure_count:  int   = field(default=0, init=False)
    _last_failure:   float = field(default=0.0, init=False)
    _lock:           threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False)

    def call(self, fn: Callable, *args, **kwargs) -> Any:
        """
        Call a function through the circuit breaker.

        Args:
            fn (Callable): Function to call.
            *args, **kwargs: Arguments to fn.

        Returns:
            Any: Function result.

        Raises:
            RuntimeError: If circuit is OPEN.
        """
        with self._lock:
            state = self._check_state()

        if state == CircuitState.OPEN:
            raise RuntimeError(
                f"Circuit '{self.name}' is OPEN — failing fast"
            )

        try:
            result = fn(*args, **kwargs)
            self._on_success()
            return result
        except Exception as e:
            self._on_failure()
            raise

    def _check_state(self) -> CircuitState:
        """Check and possibly transition state."""
        if self._state == CircuitState.OPEN:
            if time.time() - self._last_failure > self.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
        return self._state

    def _on_success(self):
        """Record a success."""
        with self._lock:
            self._failure_count = 0
            self._state         = CircuitState.CLOSED

    def _on_failure(self):
        """Record a failure and possibly open the circuit."""
        with self._lock:
            self._failure_count += 1
            self._last_failure   = time.time()
            if self._failure_count >= self.failure_threshold:
                self._state = CircuitState.OPEN

    def reset(self):
        """Force-close the circuit."""
        with self._lock:
            self._state         = CircuitState.CLOSED
            self._failure_count = 0

    @property
    def state(self) -> str:
        """Return current state as string."""
        return self._state.value

    def status(self) -> dict:
        """Return circuit status dict."""
        return {
            "name":     self.name,
            "state":    self.state,
            "failures": self._failure_count,
            "threshold": self.failure_threshold,
        }


class CircuitBreakerRegistry:
    """Registry of named circuit breakers."""

    def __init__(self):
        """Initialise the registry."""
        self._circuits: Dict[str, CircuitBreaker] = {}

    def get(self, name: str, **kwargs) -> CircuitBreaker:
        """Get or create a circuit breaker by name."""
        if name not in self._circuits:
            self._circuits[name] = CircuitBreaker(name=name, **kwargs)
        return self._circuits[name]

    def list_all(self) -> List[dict]:
        """Return status of all circuits."""
        return [c.status() for c in self._circuits.values()]

    def reset(self, name: str) -> bool:
        """Reset a circuit by name."""
        if name in self._circuits:
            self._circuits[name].reset()
            return True
        return False


# ─────────────────────────────────────────────────── Rate Limiter

class TokenBucket:
    """
    Token bucket rate limiter.

    Tokens refill at rate tokens/second.
    Max burst = capacity tokens.
    """

    def __init__(self, rate: float = 10.0, capacity: float = 20.0):
        """
        Initialise token bucket.

        Args:
            rate (float): Token refill rate (tokens/second).
            capacity (float): Maximum burst capacity.
        """
        self.rate     = rate
        self.capacity = capacity
        self._tokens  = capacity
        self._last    = time.monotonic()
        self._lock    = threading.Lock()

    def allow(self, tokens: float = 1.0) -> bool:
        """
        Attempt to consume tokens.

        Args:
            tokens (float): Number of tokens to consume.

        Returns:
            bool: True if allowed, False if rate limited.
        """
        with self._lock:
            now    = time.monotonic()
            refill = (now - self._last) * self.rate
            self._tokens = min(self.capacity, self._tokens + refill)
            self._last   = now
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    @property
    def available(self) -> float:
        """Return current token count."""
        return self._tokens


class RateLimiter:
    """
    Per-IP and global rate limiter for the REST API.
    """

    def __init__(self, global_rps: float = 100.0,
                  per_ip_rps: float = 10.0):
        """
        Initialise rate limiter.

        Args:
            global_rps (float): Global requests per second limit.
            per_ip_rps (float): Per-IP requests per second limit.
        """
        self._global  = TokenBucket(global_rps, global_rps * 2)
        self._per_ip: Dict[str, TokenBucket] = {}
        self._per_ip_rps = per_ip_rps
        self._lock    = threading.Lock()
        self._rejected: Dict[str, int] = {}

    def allow(self, ip: str = "0.0.0.0") -> bool:
        """
        Check if a request from ip should be allowed.

        Args:
            ip (str): Client IP address.

        Returns:
            bool: True if request is allowed.
        """
        if not self._global.allow():
            return False
        with self._lock:
            if ip not in self._per_ip:
                self._per_ip[ip] = TokenBucket(
                    self._per_ip_rps, self._per_ip_rps * 3)
        if not self._per_ip[ip].allow():
            with self._lock:
                self._rejected[ip] = self._rejected.get(ip, 0) + 1
            return False
        return True

    def set_limit(self, ip: str, rps: float):
        """Set a custom rate limit for a specific IP."""
        with self._lock:
            self._per_ip[ip] = TokenBucket(rps, rps * 3)

    def status(self) -> dict:
        """Return rate limiter status."""
        return {
            "global_available": round(self._global.available, 1),
            "ip_buckets":       len(self._per_ip),
            "rejected_total":   sum(self._rejected.values()),
            "top_offenders":    sorted(
                self._rejected.items(),
                key=lambda x: x[1], reverse=True
            )[:5],
        }


# ─────────────────────────────────────────────────── Retry

def retry(max_attempts: int = 3,
           base_delay: float = 0.5,
           max_delay: float = 30.0,
           exceptions: tuple = (Exception,),
           jitter: bool = True):
    """
    Decorator: retry a function with exponential backoff + jitter.

    Args:
        max_attempts (int): Maximum number of attempts.
        base_delay (float): Initial delay in seconds.
        max_delay (float): Maximum delay in seconds.
        exceptions (tuple): Exception types that trigger retry.
        jitter (bool): Add random jitter to prevent thundering herd.

    Returns:
        Decorated function with retry logic.
    """
    import secrets as _sec

    def decorator(fn):
        """Apply retry logic to a function."""
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_attempts):
                try:
                    return fn(*args, **kwargs)
                except exceptions as e:
                    last_exc = e
                    if attempt < max_attempts - 1:
                        delay = min(base_delay * (2 ** attempt), max_delay)
                        if jitter:
                            delay *= 0.5 + (_sec.randbelow(1000) / 1000)
                        time.sleep(delay)
            raise last_exc
        return wrapper
    return decorator


# ─────────────────────────────────────────────────── Distributed Tracing

_THREAD_SPAN = threading.local()


@dataclass
class Span:
    """One trace span (a unit of work with timing)."""
    trace_id:  str
    span_id:   str
    parent_id: Optional[str]
    name:      str
    service:   str
    start:     float = field(default_factory=time.time)
    end:       float = 0.0
    tags:      Dict[str, str] = field(default_factory=dict)
    error:     Optional[str]  = None

    @property
    def duration_ms(self) -> float:
        """Return span duration in milliseconds."""
        end = self.end if self.end else time.time()
        return (end - self.start) * 1000

    def finish(self):
        """Mark span as complete."""
        self.end = time.time()

    def to_dict(self) -> dict:
        """Serialize to dict."""
        return {
            "trace_id":   self.trace_id,
            "span_id":    self.span_id,
            "parent_id":  self.parent_id,
            "name":       self.name,
            "service":    self.service,
            "start":      self.start,
            "end":        self.end,
            "duration_ms": round(self.duration_ms, 2),
            "tags":       self.tags,
            "error":      self.error,
        }


class Tracer:
    """
    Distributed tracer that collects spans across NOVA subsystems.

    Trace IDs propagate via threading.local and HTTP headers.
    Completed traces are stored in the SOS as time-series data.
    """

    TRACE_BASE = "/traces"

    def __init__(self, sos: "SemanticObjectStore" = None,
                  service: str = "nova"):
        """Initialise the tracer."""
        self.sos     = sos
        self.service = service
        self._traces: Dict[str, List[Span]] = {}
        self._lock   = threading.Lock()
        if sos and not sos.exists(self.TRACE_BASE):
            sos.mkdir(self.TRACE_BASE, parents=True)

    def start_span(self, name: str, trace_id: str = None,
                    parent_id: str = None, tags: dict = None) -> Span:
        """
        Start a new trace span.

        Args:
            name (str): Span name.
            trace_id (str): Trace ID (creates new if None).
            parent_id (str): Parent span ID.
            tags (dict): Key-value tags.

        Returns:
            Span: The started span.
        """
        if trace_id is None:
            trace_id = getattr(_THREAD_SPAN, "trace_id", None)
        if trace_id is None:
            trace_id = uuid.uuid4().hex[:16]
        span_id  = uuid.uuid4().hex[:8]
        if parent_id is None:
            parent_id = getattr(_THREAD_SPAN, "span_id", None)

        span = Span(
            trace_id=trace_id, span_id=span_id,
            parent_id=parent_id, name=name,
            service=self.service, tags=tags or {},
        )
        _THREAD_SPAN.trace_id = trace_id
        _THREAD_SPAN.span_id  = span_id

        with self._lock:
            self._traces.setdefault(trace_id, []).append(span)
        return span

    def finish_span(self, span: Span):
        """Mark a span as complete and store if trace is done."""
        span.finish()
        if self.sos:
            try:
                with self._lock:
                    spans = self._traces.get(span.trace_id, [])
                    all_done = all(s.end > 0 for s in spans)
                if all_done:
                    self._store_trace(span.trace_id)
            except Exception:
                pass

    def _store_trace(self, trace_id: str):
        """Persist a completed trace to SOS."""
        if not self.sos:
            return
        with self._lock:
            spans = self._traces.pop(trace_id, [])
        if not spans:
            return
        try:
            self.sos.write(
                f"{self.TRACE_BASE}/{trace_id}",
                json.dumps([s.to_dict() for s in spans]),
                tags=["trace"],
            )
        except Exception:
            pass

    def span(self, name: str, **tags):
        """
        Context manager / decorator for automatic span lifecycle.

        Usage:
            with tracer.span("sos.write", path="/home/root/f.py"):
                sos.write(...)

            @tracer.span("my_function")
            def my_function():
                ...
        """
        return _SpanContext(self, name, tags)

    def recent_traces(self, n: int = 20) -> List[dict]:
        """Return recent traces from SOS."""
        if not self.sos:
            return []
        results = []
        try:
            for name in list(self.sos.listdir(self.TRACE_BASE))[-n:]:
                path = f"{self.TRACE_BASE}/{name}"
                spans = json.loads(self.sos.read(path))
                total_ms = sum(s.get("duration_ms", 0) for s in spans)
                results.append({
                    "trace_id": name,
                    "spans":    len(spans),
                    "total_ms": round(total_ms, 1),
                    "service":  spans[0].get("service", "?") if spans else "?",
                })
        except Exception:
            pass
        return results


class _SpanContext:
    """Context manager for automatic span start/finish."""

    def __init__(self, tracer: Tracer, name: str, tags: dict):
        """Initialise span context."""
        self.tracer = tracer
        self.name   = name
        self.tags   = tags
        self.span:  Optional[Span] = None

    def __enter__(self) -> Span:
        """Start the span."""
        self.span = self.tracer.start_span(self.name, tags=self.tags)
        return self.span

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Finish the span, recording any error."""
        if self.span:
            if exc_type:
                self.span.error = str(exc_val)
            self.tracer.finish_span(self.span)
        return False

    def __call__(self, fn):
        """Use as decorator."""
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with _SpanContext(self.tracer, self.name, self.tags):
                return fn(*args, **kwargs)
        return wrapper
