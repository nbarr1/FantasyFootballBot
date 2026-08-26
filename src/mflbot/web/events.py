"""A tiny in-process pub/sub bus, so the dashboard can push instead of poll.

Job output, job state changes and "something you are looking at has changed"
notices are published here; the SSE endpoint subscribes one queue per connected
browser tab and forwards what it receives.

Two properties matter:

* **A publisher never blocks.** Jobs run on a worker thread; a browser tab that
  has stopped reading must not be able to stall an ingest run. A full subscriber
  queue drops the event and records the drop.
* **Nothing is retained.** The bus holds no history, so there is nothing here to
  leak into a later session. State that must survive a reconnect lives in the
  database or in the job registry, both of which the client re-reads on connect.
"""

from __future__ import annotations

import json
import queue
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class Event:
    name: str
    data: dict[str, Any]
    at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def sse(self) -> str:
        """Format as a Server-Sent Events frame."""
        body = json.dumps({**self.data, "at": self.at.isoformat()}, default=str)
        return f"event: {self.name}\ndata: {body}\n\n"


class Subscription:
    """One connected client. Iterate it to receive events."""

    def __init__(self, bus: EventBus, maxsize: int = 256) -> None:
        self._bus = bus
        self._queue: queue.Queue[Event] = queue.Queue(maxsize=maxsize)
        self.dropped = 0

    def put(self, event: Event) -> None:
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self.dropped += 1

    def get(self, timeout: float) -> Event | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        self._bus.unsubscribe(self)

    def __enter__(self) -> Subscription:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[Subscription] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> Subscription:
        subscription = Subscription(self)
        with self._lock:
            self._subscribers.add(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        with self._lock:
            self._subscribers.discard(subscription)

    def publish(self, name: str, **data: Any) -> Event:
        event = Event(name, data)
        with self._lock:
            targets = list(self._subscribers)
        for subscription in targets:
            subscription.put(event)
        return event

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


def drain(subscription: Subscription, max_items: int = 64) -> list[Event]:
    """Take everything currently queued, without blocking.

    The SSE endpoint is asynchronous and must never block the event loop, so it
    polls with this rather than waiting on the queue.
    """
    out: list[Event] = []
    while len(out) < max_items:
        event = subscription.get(timeout=0)
        if event is None:
            break
        out.append(event)
    return out
