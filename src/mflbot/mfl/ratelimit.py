"""Client-side rate limiting and backoff.

MFL asks developers to cache aggressively and to keep polling modest. Those
expectations are encoded here as client policy so that no caller can opt out of
them: every request passes through :meth:`RateLimiter.acquire` before it is
sent, and every 429 or server error is fed back through
:meth:`RateLimiter.penalise`.
"""

from __future__ import annotations

import random
import threading
import time
from collections import deque
from dataclasses import dataclass

from ..errors import RateLimitError


@dataclass(slots=True)
class RateLimitPolicy:
    #: Hard ceiling on request rate, averaged over ``window_seconds``.
    max_requests: int = 30
    window_seconds: float = 60.0
    #: Never issue two requests closer together than this. MFL's own guidance:
    #: "Wait one second between making requests and you should be ok." Limits
    #: are undocumented and per-IP; registering a client (see
    #: mflbot.mfl.client.ENV_USER_AGENT) raises them roughly 2.5x, but this
    #: default targets the unregistered tier.
    min_interval_seconds: float = 1.0
    #: Backoff schedule applied after a 429 or 5xx.
    initial_backoff_seconds: float = 2.0
    max_backoff_seconds: float = 120.0
    max_retries: int = 5


class RateLimiter:
    """Token-bucket-ish limiter with a sliding window and penalty backoff.

    Thread-safe, because the scheduler runs jobs concurrently and they share one
    client instance.
    """

    def __init__(
        self,
        policy: RateLimitPolicy | None = None,
        *,
        sleep=time.sleep,
        monotonic=time.monotonic,
    ) -> None:
        self.policy = policy or RateLimitPolicy()
        self._sleep = sleep
        self._monotonic = monotonic
        self._events: deque[float] = deque()
        self._last_request: float | None = None
        self._penalty_until: float = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Block until it is polite to send the next request."""
        while True:
            with self._lock:
                now = self._monotonic()
                wait = self._wait_needed(now)
                if wait <= 0:
                    self._events.append(now)
                    self._last_request = now
                    return
            self._sleep(wait)

    def _wait_needed(self, now: float) -> float:
        policy = self.policy
        window_start = now - policy.window_seconds
        while self._events and self._events[0] < window_start:
            self._events.popleft()

        waits = [0.0]
        if self._penalty_until > now:
            waits.append(self._penalty_until - now)
        if self._last_request is not None:
            elapsed = now - self._last_request
            if elapsed < policy.min_interval_seconds:
                waits.append(policy.min_interval_seconds - elapsed)
        if len(self._events) >= policy.max_requests:
            waits.append(self._events[0] + policy.window_seconds - now)
        return max(waits)

    def penalise(self, attempt: int, retry_after: float | None = None) -> float:
        """Register an upstream throttle/error and return the delay to observe.

        ``attempt`` is 0-based. Honours an explicit ``Retry-After`` when MFL
        sends one; otherwise applies exponential backoff with jitter.
        """
        policy = self.policy
        if attempt >= policy.max_retries:
            raise RateLimitError(
                f"Gave up after {attempt} retries against MFL's rate limiting. "
                f"Reduce polling frequency in [schedule] before trying again."
            )
        if retry_after is not None and retry_after > 0:
            delay = min(retry_after, policy.max_backoff_seconds)
        else:
            delay = min(
                policy.initial_backoff_seconds * (2**attempt),
                policy.max_backoff_seconds,
            )
            delay *= 0.5 + random.random()  # jitter, to avoid lockstep retries
        with self._lock:
            self._penalty_until = max(
                self._penalty_until, self._monotonic() + delay
            )
        return delay

    def reset_penalty(self) -> None:
        with self._lock:
            self._penalty_until = 0.0
