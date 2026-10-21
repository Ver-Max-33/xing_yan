"""The page's live feed (spec §8.5): every poll of the live thread to every open page, as server-sent events. A page
that does not keep up loses its oldest events — the poller never waits for a page."""
import asyncio
import threading


class Broadcaster:
    def __init__(self, size: int = 100):
        self.size = size
        self._subs: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._lock = threading.Lock()

    def subscribe(self) -> asyncio.Queue:
        """In the page's request (an event loop runs there)."""
        q: asyncio.Queue = asyncio.Queue(maxsize=self.size)
        with self._lock:
            self._subs.append((asyncio.get_running_loop(), q))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs = [(loop, x) for loop, x in self._subs if x is not q]

    def publish(self, event: dict) -> None:
        """From any thread."""
        with self._lock:
            subs = list(self._subs)
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(self._put, q, event)
            except RuntimeError:  # the page's loop is gone: it unsubscribes as it ends
                pass

    @staticmethod
    def _put(q: asyncio.Queue, event: dict) -> None:
        if q.full():
            q.get_nowait()
        q.put_nowait(event)
