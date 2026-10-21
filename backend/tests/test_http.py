import httpx
import pytest

from wb.data.http import HttpClient, RateLimiter


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def test_rate_limiter_waits_for_window():
    c = FakeClock()
    lim = RateLimiter(2, clock=c, sleep=c.sleep)
    lim.acquire()
    c.t = 10
    lim.acquire()
    c.t = 20
    lim.acquire()  # third call inside the same minute must wait until t=60
    assert c.slept == [40]
    assert c.t == 60


def test_calls_from_several_threads_never_exceed_the_rate():
    # several workers share one limiter (a backfill fetches days side by side): each call is counted once and no
    # 60-second window ever holds more than the rate, however the threads interleave
    import threading
    import time as real

    lock, times = threading.Lock(), []
    c = FakeClock()

    def clock():
        real.sleep(0.0005)          # let another thread run between reading the window and taking a place in it
        with lock:
            return c.t

    def sleep(s):
        with lock:
            c.t += s

    lim = RateLimiter(3, clock=clock, sleep=sleep)

    def call():
        lim.acquire()
        with lock:
            times.append(c.t)

    threads = [threading.Thread(target=call) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(times) == 12
    assert max(sum(1 for u in times if t <= u < t + 60) for t in times) <= 3


def test_retries_then_succeeds():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503 if len(calls) < 3 else 200, json={"ok": True})

    slept = []
    http = HttpClient(1000, transport=httpx.MockTransport(handler), sleep=slept.append)
    r = http.request("GET", "https://example.test/x")
    assert r.status_code == 200 and len(calls) == 3 and slept == [5, 10]


def test_gives_up_on_network_error():
    def handler(request):
        raise httpx.ConnectError("down")

    http = HttpClient(1000, transport=httpx.MockTransport(handler), retries=1, sleep=lambda s: None)
    with pytest.raises(httpx.ConnectError):
        http.request("GET", "https://example.test/x")


def test_client_errors_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(403)

    http = HttpClient(1000, transport=httpx.MockTransport(handler), sleep=lambda s: None)
    assert http.request("GET", "https://example.test/x").status_code == 403 and len(calls) == 1


def test_429_waits_a_full_minute():
    # Per-minute quotas (J-Quants free plan: 5/min) need a full minute, not the 5 s 5xx backoff.
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429 if len(calls) == 1 else 200, json={})

    slept = []
    http = HttpClient(1000, transport=httpx.MockTransport(handler), sleep=slept.append)
    assert http.request("GET", "https://example.test/x").status_code == 200
    assert slept == [60.0]
