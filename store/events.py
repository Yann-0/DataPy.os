"""
PyOS NOVA — Reactive SOS Event Bus
=====================================
Every SOS write, read, delete, and tag operation publishes an event.
Subscribers match on path glob, event type, kind, tags, or content regex.

This turns NOVA from a pull model into a push model:
  - `watch /logs/**` → streams live log updates to terminal
  - Agents react to changes instead of polling every N seconds
  - HELIX reflects remote edits as they happen
  - Security alerts trigger immediately on secret access
  - The dashboard updates in real time without polling

Events:
  write  (path, oid, kind, size, tags)
  read   (path, oid)
  delete (path)
  tag    (path, tags_added)
  move   (src_path, dst_path)
  batch  (count, paths)     — WAQ batch flush

Shell commands:
  watch <pattern>         — stream matching events to terminal
  watch --once <pattern>  — wait for one matching event then exit
  on <pattern> <command>  — run a shell command when pattern matches
  events list             — show registered subscriptions
  events clear            — clear all subscriptions
"""

from __future__ import annotations
import os, sys, re, fnmatch, time, threading, queue
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Callable, Any, TYPE_CHECKING

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

if TYPE_CHECKING:
    from store.sos import SemanticObjectStore


@dataclass
class SOSEvent:
    """An event emitted by the Semantic Object Store."""
    event_type: str          # write|read|delete|tag|move|batch
    path:       str          # primary path
    oid:        str = ""     # object OID (if applicable)
    kind:       str = ""     # object kind
    size:       int = 0
    tags:       List[str] = field(default_factory=list)
    detail:     str = ""     # extra info
    timestamp:  float = field(default_factory=time.time)

    def matches(self, pattern: str, event_types: List[str] = None) -> bool:
        """
        Return True if this event matches the given pattern.

        Pattern matching rules:
          /home/**   — glob on path
          *.py       — glob on filename
          kind:code  — match by object kind
          tag:secret — match by tag
          event:write — match by event type

        Args:
            pattern (str): Pattern string to match against.
            event_types (List[str]): Optional event type filter.

        Returns:
            bool: True if this event matches the pattern.
        """
        if event_types and self.event_type not in event_types:
            return False
        p = pattern.strip()
        # Event type prefix
        if p.startswith("event:"):
            return self.event_type == p[6:]
        # Kind prefix
        if p.startswith("kind:"):
            return self.kind == p[5:]
        # Tag prefix
        if p.startswith("tag:"):
            return p[4:] in self.tags
        # Path glob
        return fnmatch.fnmatch(self.path, p) or fnmatch.fnmatch(
            self.path, p.rstrip("/") + "/*"
        )


@dataclass
class Subscription:
    """A registered event subscription."""
    sub_id:      str
    pattern:     str
    event_types: List[str]
    callback:    Callable[[SOSEvent], None]
    once:        bool = False      # auto-remove after first match
    owner:       str = "system"
    created_at:  float = field(default_factory=time.time)
    match_count: int = 0


class EventBus:
    """
    Reactive event bus for the Semantic Object Store.

    Thread-safe publish/subscribe with pattern matching.
    Callbacks run in a dedicated dispatcher thread to avoid
    blocking the write path.
    """

    def __init__(self):
        """Initialise the event bus."""
        self._subs:    Dict[str, Subscription] = {}
        self._lock     = threading.Lock()
        self._queue    = queue.Queue(maxsize=50_000)
        self._running  = True
        self._pub_count   = 0
        self._match_count = 0
        self._thread   = threading.Thread(
            target=self._dispatch_loop, daemon=True, name="nova-events"
        )
        self._thread.start()

    def publish(self, event: SOSEvent):
        """
        Publish an event to all matching subscribers.

        Non-blocking: event is queued and dispatched asynchronously.

        Args:
            event (SOSEvent): The event to publish.
        """
        self._pub_count += 1
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            pass   # drop oldest if queue is full (back-pressure)

    def subscribe(self, pattern: str,
                   callback: Callable[[SOSEvent], None],
                   event_types: List[str] = None,
                   once: bool = False,
                   owner: str = "system") -> str:
        """
        Register a subscription.

        Args:
            pattern (str): Path glob or kind:/tag:/event: pattern.
            callback (Callable): Called with matching SOSEvent.
            event_types (List[str]): Filter to specific event types.
            once (bool): Auto-remove after first match.
            owner (str): Who registered this subscription.

        Returns:
            str: Subscription ID.
        """
        sub_id = f"sub_{id(callback)}_{time.time_ns()}"
        sub    = Subscription(
            sub_id=sub_id, pattern=pattern,
            event_types=event_types or [],
            callback=callback, once=once, owner=owner,
        )
        with self._lock:
            self._subs[sub_id] = sub
        return sub_id

    def unsubscribe(self, sub_id: str) -> bool:
        """
        Remove a subscription.

        Args:
            sub_id (str): The subscription ID to remove.

        Returns:
            bool: True if found and removed.
        """
        with self._lock:
            return bool(self._subs.pop(sub_id, None))

    def _dispatch_loop(self):
        """Background thread: dequeue and dispatch events."""
        while self._running:
            try:
                event = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            with self._lock:
                subs = list(self._subs.values())
            to_remove = []
            for sub in subs:
                try:
                    if event.matches(sub.pattern, sub.event_types or None):
                        sub.match_count += 1
                        self._match_count += 1
                        sub.callback(event)
                        if sub.once:
                            to_remove.append(sub.sub_id)
                except Exception:
                    pass
            if to_remove:
                with self._lock:
                    for sid in to_remove:
                        self._subs.pop(sid, None)

    def stop(self):
        """Stop the dispatcher thread."""
        self._running = False

    def list_subs(self) -> List[dict]:
        """Return all active subscriptions."""
        with self._lock:
            return [{
                "id":      s.sub_id[:16],
                "pattern": s.pattern,
                "owner":   s.owner,
                "hits":    s.match_count,
                "once":    s.once,
            } for s in self._subs.values()]

    def stats(self) -> dict:
        """Return event bus statistics."""
        return {
            "subscriptions": len(self._subs),
            "published":     self._pub_count,
            "matched":       self._match_count,
            "queue_depth":   self._queue.qsize(),
        }

    def wait_for(self, pattern: str, timeout: float = 30.0,
                  event_types: List[str] = None) -> Optional[SOSEvent]:
        """
        Block until a matching event arrives or timeout.

        Args:
            pattern (str): Pattern to wait for.
            timeout (float): Maximum wait time in seconds.
            event_types (List[str]): Filter to specific event types.

        Returns:
            SOSEvent: The matching event, or None on timeout.
        """
        result_q: queue.Queue = queue.Queue(maxsize=1)

        def _cb(event: SOSEvent):
            try:
                result_q.put_nowait(event)
            except queue.Full:
                pass

        sub_id = self.subscribe(pattern, _cb,
                                 event_types=event_types, once=True)
        try:
            return result_q.get(timeout=timeout)
        except queue.Empty:
            self.unsubscribe(sub_id)
            return None


# Global event bus singleton
_bus: Optional[EventBus] = None


def get_bus() -> EventBus:
    """Return the global event bus, creating it if needed."""
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus


def patch_sos_with_events(sos: "SemanticObjectStore",
                           bus: Optional[EventBus] = None) -> EventBus:
    """
    Monkey-patch the SOS to emit events on every operation.

    Args:
        sos: The SemanticObjectStore to patch.
        bus (EventBus): The event bus to publish to. Creates one if None.

    Returns:
        EventBus: The active event bus.
    """
    if bus is None:
        bus = get_bus()

    orig_write  = sos.write
    orig_read   = sos.read
    orig_remove = sos.remove
    orig_tag    = sos.tag

    def _write(path, content, kind="text", tags=None, **kw):
        oid = orig_write(path, content, kind=kind, tags=tags, **kw)
        bus.publish(SOSEvent(
            event_type = "write",
            path       = path,
            oid        = oid,
            kind       = kind,
            size       = len(content) if isinstance(content, (str, bytes)) else 0,
            tags       = list(tags or []),
        ))
        return oid

    def _read(path):
        result = orig_read(path)
        oid    = sos.resolve(path) or ""
        bus.publish(SOSEvent(event_type="read", path=path, oid=oid))
        return result

    def _remove(path, **kw):
        bus.publish(SOSEvent(event_type="delete", path=path))
        return orig_remove(path, **kw)

    def _tag(path, *tags):
        result = orig_tag(path, *tags)
        bus.publish(SOSEvent(event_type="tag", path=path,
                              tags=list(tags)))
        return result

    sos.write  = _write
    sos.read   = _read
    sos.remove = _remove
    sos.tag    = _tag
    sos._bus   = bus
    return bus
