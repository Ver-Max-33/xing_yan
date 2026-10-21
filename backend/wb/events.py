"""In-process event bus (spec §3.5, mechanism 4).

Producers call `publish`; consumers subscribe with `@on`. A failing consumer
never breaks the producer: its exception is returned, not raised.
"""
from collections import defaultdict
from collections.abc import Callable
from typing import Any

Handler = Callable[[dict[str, Any]], None]
_handlers: dict[str, list[Handler]] = defaultdict(list)


def on(event: str):
    def deco(fn: Handler) -> Handler:
        _handlers[event].append(fn)
        return fn

    return deco


def publish(event: str, payload: dict[str, Any]) -> list[Exception]:
    errors: list[Exception] = []
    for fn in list(_handlers[event]):
        try:
            fn(payload)
        except Exception as e:  # noqa: BLE001 - isolate consumers from producers
            errors.append(e)
    return errors


def clear() -> None:
    """Remove all handlers (tests only)."""
    _handlers.clear()
