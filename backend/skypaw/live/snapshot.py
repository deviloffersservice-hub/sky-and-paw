"""Fan-out of server messages to WebSocket clients, and the 5 Hz snapshot loop.

Each client gets a bounded queue; a slow client loses its oldest messages rather than
stalling the runtime (snapshots are full state, so dropping one is harmless).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import Any

from skypaw.logging import get_logger, log_event

log = get_logger(__name__)

SNAPSHOT_HZ = 5.0


class Hub:
    """Publish/subscribe of JSON-ready dicts."""

    def __init__(self, maxsize: int = 64) -> None:
        self.maxsize = maxsize
        self._subs: set[asyncio.Queue[dict[str, Any]]] = set()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        """A new queue that receives every published message."""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(self.maxsize)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        """Stop delivering to ``q``."""
        self._subs.discard(q)

    @property
    def subscribers(self) -> int:
        """Number of live subscribers."""
        return len(self._subs)

    def publish(self, message: dict[str, Any]) -> None:
        """Deliver to every subscriber (drops the oldest message of a full queue)."""
        for q in list(self._subs):
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            q.put_nowait(message)


async def snapshot_loop(
    hub: Hub, make_snapshot: Callable[[], dict[str, Any]], hz: float = SNAPSHOT_HZ
) -> None:
    """Publish ``make_snapshot()`` every ``1/hz`` s while anyone is subscribed (runs forever)."""
    period = 1.0 / hz
    while True:
        if hub.subscribers:
            try:
                hub.publish(make_snapshot())
            except Exception as exc:  # noqa: BLE001 - one bad snapshot must not kill the loop
                log_event(log, "snapshot_error", level=40, error=f"{type(exc).__name__}: {exc}")
        await asyncio.sleep(period)


__all__ = ["SNAPSHOT_HZ", "Hub", "snapshot_loop"]
