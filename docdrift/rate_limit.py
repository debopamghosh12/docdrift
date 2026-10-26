"""Client-side tokens-per-minute limiter, so we pace calls instead of tripping 429s."""

import threading
import time
from collections.abc import Callable

WINDOW_SECONDS = 60.0


class TokenRateLimiter:
    """Sliding-window TPM budget shared across threads.

    `acquire` reserves an estimate before a call; `settle` replaces it with the real usage.
    """

    def __init__(
        self,
        tokens_per_minute: int,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._limit = tokens_per_minute
        self._clock = clock
        self._sleep = sleep
        self._events: list[list[float]] = []  # [timestamp, tokens]
        self._lock = threading.Lock()

    def acquire(self, estimate: int) -> list[float]:
        estimate = min(estimate, self._limit)
        with self._lock:
            while True:
                now = self._clock()
                self._events = [e for e in self._events if now - e[0] < WINDOW_SECONDS]
                used = sum(e[1] for e in self._events)
                if used + estimate <= self._limit:
                    event = [now, float(estimate)]
                    self._events.append(event)
                    return event
                self._sleep(self._events[0][0] + WINDOW_SECONDS - now + 0.05)

    def settle(self, event: list[float], actual_tokens: int) -> None:
        with self._lock:
            event[1] = float(actual_tokens)
