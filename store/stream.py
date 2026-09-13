"""
PyOS NOVA — Stream Processor
================================
Composable reactive pipeline over SOS events.

Events flow through operators connected with Python's | pipe syntax:

  stream("/logs/**") | filter(kind="code") | map(classify) | sink("/classified")
  stream("*.py")     | window(60) | aggregate(count) | sink("/metrics/writes_per_min")
  stream("tag:secret") | alert("Secret object written: {path}")

Operators:
  filter(fn)         — keep events matching predicate
  map(fn)            — transform events
  window(seconds)    — collect events into time windows
  aggregate(fn)      — reduce a window to one value
  throttle(n, per_s) — rate-limit to n events per second
  distinct()         — deduplicate consecutive identical paths
  take(n)            — stop after n events
  sink(path)         — write results to SOS path
  alert(template)    — push to advisor on match
  branch(fn, a, b)   — route to stream a or b based on predicate

Shell commands:
  stream "<pattern>" [| operators...]  — start a named stream
  streams list                          — show active streams
  streams stop <id>                     — stop a stream
  streams stats                         — show throughput stats
"""

from __future__ import annotations
import os, sys, time, threading, queue, re
from typing import (Callable, Iterator, Optional,
                    List, Dict, Any, TYPE_CHECKING)
from dataclasses import dataclass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from store.sos   import SemanticObjectStore
    from store.events import EventBus, SOSEvent


@dataclass
class StreamStats:
    """Per-stream throughput statistics."""
    stream_id: str
    received:  int = 0
    emitted:   int = 0
    dropped:   int = 0
    started_at: float = 0.0

    @property
    def elapsed_s(self) -> float:
        """Return elapsed time since stream started."""
        return time.time() - self.started_at if self.started_at else 0.0

    @property
    def throughput(self) -> float:
        """Return events per second."""
        return self.received / max(self.elapsed_s, 0.001)


class Operator:
    """
    Base class for stream operators. Supports | chaining.

    Usage:
        op1 | op2  →  chain op2 after op1
        op | sink  →  terminate into a sink
    """

    def __init__(self):
        """Initialise the operator."""
        self._next: Optional["Operator"] = None
        self._stats = StreamStats(stream_id="op")

    def process(self, event: "SOSEvent") -> Optional["SOSEvent"]:
        """
        Process one event and optionally pass it downstream.

        Args:
            event: Incoming SOSEvent.

        Returns:
            Transformed event, or None to drop it.
        """
        return event

    def _emit(self, event: "SOSEvent"):
        """Send event to the next operator in the chain."""
        if self._next:
            result = self._next.process(event)
            if result is not None:
                self._next._emit(result)

    def __or__(self, other: "Operator") -> "Operator":
        """Chain: self | other returns other with self as upstream."""
        # Find the last operator in self's chain
        tail = self
        while tail._next:
            tail = tail._next
        tail._next = other
        return self


class Filter(Operator):
    """Keep events matching a predicate."""

    def __init__(self, fn: Callable[["SOSEvent"], bool] = None,
                  **kwargs):
        """
        Initialise filter.

        Args:
            fn: Predicate function. If None, uses kwargs as attribute filters.
            **kwargs: Attribute equality filters (e.g. kind='code').
        """
        super().__init__()
        if fn:
            self._pred = fn
        else:
            def _pred(e):
                return all(getattr(e, k, None) == v
                           for k, v in kwargs.items())
            self._pred = _pred

    def process(self, event: "SOSEvent") -> Optional["SOSEvent"]:
        """Pass through events matching the predicate."""
        return event if self._pred(event) else None


class Map(Operator):
    """Transform events."""

    def __init__(self, fn: Callable[["SOSEvent"], Any]):
        """
        Initialise map.

        Args:
            fn: Transform function. Receives SOSEvent, returns Any.
        """
        super().__init__()
        self._fn = fn

    def process(self, event: "SOSEvent") -> Optional[Any]:
        """Transform the event."""
        try:
            return self._fn(event)
        except Exception:
            return None


class Window(Operator):
    """Collect events into fixed time windows."""

    def __init__(self, seconds: float):
        """
        Initialise tumbling window.

        Args:
            seconds (float): Window duration.
        """
        super().__init__()
        self._seconds  = seconds
        self._buf:     List = []
        self._deadline = 0.0
        self._lock     = threading.Lock()

    def process(self, event: "SOSEvent") -> Optional[list]:
        """Accumulate event and flush when window expires."""
        now = time.time()
        with self._lock:
            if self._deadline == 0.0:
                self._deadline = now + self._seconds
            self._buf.append(event)
            if now >= self._deadline:
                batch          = list(self._buf)
                self._buf      = []
                self._deadline = now + self._seconds
                return batch
        return None


class Aggregate(Operator):
    """Reduce a window (list) to a single value."""

    BUILT_INS = {
        "count":  lambda events: len(events),
        "paths":  lambda events: [e.path for e in events],
        "kinds":  lambda events: list({e.event_type for e in events}),
    }

    def __init__(self, fn: Callable[[list], Any] = "count"):
        """
        Initialise aggregator.

        Args:
            fn: Aggregation function or name ('count', 'paths', 'kinds').
        """
        super().__init__()
        if isinstance(fn, str):
            self._fn = self.BUILT_INS.get(fn, len)
        else:
            self._fn = fn

    def process(self, events) -> Any:
        """Aggregate a window batch."""
        if not isinstance(events, list):
            return events
        try:
            return self._fn(events)
        except Exception:
            return len(events)


class Throttle(Operator):
    """Rate-limit event flow."""

    def __init__(self, max_rate: float = 10.0):
        """
        Initialise throttle.

        Args:
            max_rate (float): Maximum events per second.
        """
        super().__init__()
        self._interval = 1.0 / max_rate
        self._last     = 0.0

    def process(self, event) -> Optional[Any]:
        """Drop event if rate limit exceeded."""
        now = time.time()
        if now - self._last < self._interval:
            return None
        self._last = now
        return event


class Distinct(Operator):
    """Deduplicate consecutive identical events by path."""

    def __init__(self):
        """Initialise distinct operator."""
        super().__init__()
        self._last_path = ""

    def process(self, event: "SOSEvent") -> Optional["SOSEvent"]:
        """Drop consecutive duplicates."""
        path = getattr(event, "path", str(event))
        if path == self._last_path:
            return None
        self._last_path = path
        return event


class Take(Operator):
    """Stop stream after n events."""

    def __init__(self, n: int):
        """Initialise take operator."""
        super().__init__()
        self._n   = n
        self._seen = 0

    def process(self, event) -> Optional[Any]:
        """Pass events until n is reached."""
        if self._seen >= self._n:
            return None
        self._seen += 1
        return event


class Sink(Operator):
    """Write stream output to an SOS path."""

    def __init__(self, sos: "SemanticObjectStore", path: str):
        """
        Initialise sink.

        Args:
            sos: The SOS to write to.
            path (str): SOS path to write results to.
        """
        super().__init__()
        self.sos  = sos
        self.path = path
        self._buf: List[Any] = []
        self._lock = threading.Lock()
        self._flush_thread = threading.Thread(
            target=self._flush_loop, daemon=True, name="nova-sink")
        self._flush_thread.start()

    def process(self, event: Any) -> None:
        """Buffer event for flushing."""
        with self._lock:
            self._buf.append(event)
        return None

    def _flush_loop(self):
        """Flush buffer to SOS every second."""
        while True:
            time.sleep(1.0)
            with self._lock:
                if not self._buf:
                    continue
                items      = list(self._buf)
                self._buf  = []
            try:
                import json as _json
                self.sos.write(self.path, _json.dumps(items, default=str),
                                kind="data", tags=["stream-output"])
            except Exception:
                pass


class Alert(Operator):
    """Push a notification to the advisor when events arrive."""

    def __init__(self, template: str, kernel=None):
        """
        Initialise alert operator.

        Args:
            template (str): Message template with {path}, {kind}, {type} vars.
            kernel: NovaKernel instance for advisor access.
        """
        super().__init__()
        self._template = template
        self._kernel   = kernel

    def process(self, event: "SOSEvent") -> "SOSEvent":
        """Send alert and pass event through."""
        if self._kernel:
            try:
                msg = self._template.format(
                    path=getattr(event, "path", "?"),
                    kind=getattr(event, "kind", "?"),
                    type=getattr(event, "event_type", "?"),
                )
                from ai.advisor import Advice
                self._kernel.advisor.push(Advice(
                    "Stream alert", msg,
                    severity="warn", source="stream",
                ))
            except Exception:
                pass
        return event


class Stream:
    """
    A named reactive stream subscribed to SOS events.

    Creates a subscription on the event bus and routes events
    through the operator chain.
    """

    def __init__(self, stream_id: str, pattern: str,
                  bus: "EventBus", pipeline: Operator):
        """Initialise the stream."""
        self.stream_id = stream_id
        self.pattern   = pattern
        self._bus      = bus
        self._pipeline = pipeline
        self._stats    = StreamStats(stream_id=stream_id,
                                      started_at=time.time())
        self._sub_id   = self._subscribe()

    def _subscribe(self) -> str:
        """Subscribe to the event bus."""
        def _cb(event: "SOSEvent"):
            self._stats.received += 1
            result = self._pipeline.process(event)
            if result is not None:
                self._stats.emitted += 1
                self._pipeline._emit(result)

        return self._bus.subscribe(
            self.pattern, _cb, owner=f"stream:{self.stream_id}"
        )

    def stop(self):
        """Unsubscribe and stop the stream."""
        self._bus.unsubscribe(self._sub_id)

    @property
    def stats(self) -> StreamStats:
        """Return current stream statistics."""
        return self._stats


class StreamManager:
    """
    Manages all active streams.

    Provides a fluent API for creating processing pipelines.
    """

    def __init__(self, sos: "SemanticObjectStore",
                  bus: "EventBus"):
        """Initialise the stream manager."""
        self.sos      = sos
        self.bus      = bus
        self._streams: Dict[str, Stream] = {}
        self._counter = 0

    def _next_id(self) -> str:
        """Generate a unique stream ID."""
        self._counter += 1
        return f"s{self._counter:04d}"

    def stream(self, pattern: str) -> "PipelineBuilder":
        """
        Start building a stream pipeline.

        Args:
            pattern (str): SOS event pattern to subscribe to.

        Returns:
            PipelineBuilder: Fluent builder for constructing the pipeline.
        """
        return PipelineBuilder(self, pattern)

    def _start(self, pattern: str, pipeline: Operator) -> Stream:
        """Start a stream with a given pipeline."""
        sid    = self._next_id()
        stream = Stream(sid, pattern, self.bus, pipeline)
        self._streams[sid] = stream
        return stream

    def stop(self, stream_id: str) -> bool:
        """Stop a stream by ID."""
        stream = self._streams.pop(stream_id, None)
        if stream:
            stream.stop()
            return True
        return False

    def stop_all(self):
        """Stop all active streams."""
        for stream in list(self._streams.values()):
            stream.stop()
        self._streams.clear()

    def list_streams(self) -> List[dict]:
        """Return statistics for all active streams."""
        return [
            {"id": sid, "pattern": s.pattern,
             "received": s.stats.received,
             "emitted": s.stats.emitted,
             "throughput": f"{s.stats.throughput:.1f}/s"}
            for sid, s in self._streams.items()
        ]


class PipelineBuilder:
    """Fluent builder for constructing stream pipelines."""

    def __init__(self, mgr: StreamManager, pattern: str):
        """Initialise the pipeline builder."""
        self._mgr     = mgr
        self._pattern = pattern
        self._ops:    List[Operator] = []

    def filter(self, fn=None, **kwargs) -> "PipelineBuilder":
        """Add a filter operator."""
        self._ops.append(Filter(fn, **kwargs))
        return self

    def map(self, fn: Callable) -> "PipelineBuilder":
        """Add a map operator."""
        self._ops.append(Map(fn))
        return self

    def window(self, seconds: float) -> "PipelineBuilder":
        """Add a tumbling window operator."""
        self._ops.append(Window(seconds))
        return self

    def aggregate(self, fn="count") -> "PipelineBuilder":
        """Add an aggregate operator."""
        self._ops.append(Aggregate(fn))
        return self

    def throttle(self, rate: float = 10.0) -> "PipelineBuilder":
        """Add a rate limiter."""
        self._ops.append(Throttle(rate))
        return self

    def distinct(self) -> "PipelineBuilder":
        """Add a dedup operator."""
        self._ops.append(Distinct())
        return self

    def take(self, n: int) -> "PipelineBuilder":
        """Stop after n events."""
        self._ops.append(Take(n))
        return self

    def alert(self, template: str, kernel=None) -> "PipelineBuilder":
        """Push advisor alert on match."""
        self._ops.append(Alert(template, kernel))
        return self

    def sink(self, path: str) -> Stream:
        """Terminate into an SOS sink and start the stream."""
        self._ops.append(Sink(self._mgr.sos, path))
        return self._start()

    def _start(self) -> Stream:
        """Chain operators and start the stream."""
        if not self._ops:
            self._ops.append(Operator())   # passthrough
        # Chain all operators
        head = self._ops[0]
        for i in range(1, len(self._ops)):
            head = head | self._ops[i]
        return self._mgr._start(self._pattern, self._ops[0])

    def start(self) -> Stream:
        """Start the stream without a sink (events are counted but dropped)."""
        return self._start()
