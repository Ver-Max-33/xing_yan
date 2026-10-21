"""Rate-limited HTTP with retries, shared by all providers."""
import threading
import time
from collections import deque
from collections.abc import Callable

import httpx

RETRY_STATUS = {429, 500, 502, 503, 504}


class RateLimiter:
    """At most `per_minute` calls in any rolling 60 s window, however many threads share it (a waiting thread keeps
    the others behind it: they get their places in turn)."""

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.per_minute = per_minute
        self._clock, self._sleep = clock, sleep
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = self._clock()
            while self._calls and now - self._calls[0] >= 60:
                self._calls.popleft()
            if len(self._calls) >= self.per_minute:
                self._sleep(60 - (now - self._calls[0]))
                self._calls.popleft()
            self._calls.append(self._clock())

    def set_rate(self, per_minute: int) -> None:
        self.per_minute = max(1, per_minute)


class HttpClient:
    def __init__(self, per_minute: int, *, transport: httpx.BaseTransport | None = None,
                 timeout: float = 30.0, retries: int = 3,
                 sleep: Callable[[float], None] = time.sleep, headers: dict[str, str] | None = None):
        self.limiter = RateLimiter(per_minute, sleep=sleep)
        self.retries, self._sleep = retries, sleep
        self._client = httpx.Client(transport=transport, timeout=timeout, follow_redirects=True,
                                    headers={"User-Agent": "Mozilla/5.0", **(headers or {})})

    def request(self, method: str, url: str, **kw) -> httpx.Response:
        """Send with rate limiting; retry 429/5xx/network errors with backoff 5 s, 10 s, 20 s.
        Other statuses are returned to the caller unchanged."""
        for attempt in range(self.retries + 1):
            self.limiter.acquire()
            try:
                r = self._client.request(method, url, **kw)
            except httpx.TransportError:
                if attempt == self.retries:
                    raise
            else:
                if r.status_code not in RETRY_STATUS or attempt == self.retries:
                    return r
                if r.status_code == 429:  # per-minute quotas (J-Quants free: 5/min) need a full minute
                    self._sleep(max(60.0, float(r.headers.get("Retry-After", 0) or 0)))
                    continue
            self._sleep(5 * 2**attempt)
        raise AssertionError("unreachable")
