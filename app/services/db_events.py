"""Database notifications for this API process: live streams and the finalizer.

Every uvicorn process keeps one connection that ``LISTEN``s on two channels
(the triggers are in ``app.models``, QUEUE NOTIFICATIONS):

* ``task_events`` (payload: deployment id) — the worker wrote an event;
  wakes the open SSE streams of that deployment in this process, which
  then read the new rows from ``task_events``.
* ``task_finished`` (payload: task id) — a task reached a terminal status;
  hands it to ``task_finalizer``, which makes sure only one process does
  the follow-up work.

A notification is only a wake-up call, and Postgres does not keep it for a
listener that is not connected. So after every (re)connect all streams are
woken and the finalizer catches up on whatever finished in between: nothing
is lost, it is only read later.

The connection goes straight to Postgres (``DATABASE_URL``); ``LISTEN``
does not work through a transaction-pooling PgBouncer.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict
from collections.abc import Callable, Iterator

import psycopg

logger = logging.getLogger(__name__)

CHANNEL_EVENTS = "task_events"
CHANNEL_FINISHED = "task_finished"

_RECONNECT_MIN_S = 1.0
_RECONNECT_MAX_S = 30.0

# Detect a silently dead connection (no RST) in about a minute, so the
# listener reconnects instead of waiting forever for notifications.
_KEEPALIVE = {"keepalives": 1, "keepalives_idle": 30, "keepalives_interval": 10, "keepalives_count": 3}


class DbEventDispatcher:
    """One ``LISTEN`` connection per process, fanned out to this process's waiters.

    Lives on the FastAPI event loop: ``subscribe`` (SSE handlers) and the
    listener task touch the waiter sets from the same loop, so no lock.
    """

    def __init__(self) -> None:
        self._waiters: dict[str, set[asyncio.Event]] = defaultdict(set)
        self._task: asyncio.Task[None] | None = None
        # Running handler tasks; kept referenced so they are not collected.
        self._handlers: set[asyncio.Task[None]] = set()
        # Whether the LISTEN connection is up (for health checks and tests).
        self.connected = False

    # ----- SSE side ------------------------------------------------------

    @contextlib.contextmanager
    def subscribe(self, deployment_id: str) -> Iterator[asyncio.Event]:
        """An event that is set whenever ``deployment_id`` may have new task events.

        Subscribe *before* the first read, so a notification between the
        read and the wait is not lost; clear the event before each read.
        """
        waiter = asyncio.Event()
        self._waiters[deployment_id].add(waiter)
        try:
            yield waiter
        finally:
            waiters = self._waiters.get(deployment_id)
            if waiters is not None:
                waiters.discard(waiter)
                if not waiters:
                    del self._waiters[deployment_id]

    def wake(self, deployment_id: str) -> None:
        """Wake the streams of one deployment."""
        for waiter in self._waiters.get(deployment_id, ()):
            waiter.set()

    def wake_all(self) -> None:
        """Wake every stream (after a reconnect, when notifications may have been missed)."""
        for waiters in self._waiters.values():
            for waiter in waiters:
                waiter.set()

    # ----- listener --------------------------------------------------------

    def start(
        self,
        conninfo: str,
        *,
        on_task_finished: Callable[[str], None],
        on_connected: Callable[[], None],
    ) -> None:
        """Start the listener task on the running loop.

        ``on_task_finished(task_id)`` and ``on_connected()`` are synchronous
        (database work) and run in a thread.
        """
        self._task = asyncio.create_task(self._run(conninfo, on_task_finished, on_connected))

    async def stop(self) -> None:
        """Cancel the listener task and wait for it."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        self.connected = False

    async def _run(
        self,
        conninfo: str,
        on_task_finished: Callable[[str], None],
        on_connected: Callable[[], None],
    ) -> None:
        delay = _RECONNECT_MIN_S
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(
                    conninfo, autocommit=True, **_KEEPALIVE
                ) as conn:
                    await conn.execute(f"LISTEN {CHANNEL_EVENTS}")
                    await conn.execute(f"LISTEN {CHANNEL_FINISHED}")
                    logger.info("listening on %s, %s", CHANNEL_EVENTS, CHANNEL_FINISHED)
                    delay = _RECONNECT_MIN_S
                    self.connected = True
                    # Read what happened while nobody listened.
                    self.wake_all()
                    await asyncio.to_thread(_safely, on_connected)
                    async for notify in conn.notifies():
                        if notify.channel == CHANNEL_EVENTS:
                            self.wake(notify.payload)
                        elif notify.channel == CHANNEL_FINISHED:
                            # Fire and forget: the finalizer must not hold up
                            # the stream wake-ups behind it.
                            handler = asyncio.create_task(
                                asyncio.to_thread(_safely, on_task_finished, notify.payload)
                            )
                            self._handlers.add(handler)
                            handler.add_done_callback(self._handlers.discard)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.connected = False
                logger.warning("database listener lost (%s); reconnecting in %.0fs", e, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, _RECONNECT_MAX_S)


def _safely(fn: Callable[..., None], *args: str) -> None:
    """Run a handler; log, never raise, so one bad task cannot stop the listener."""
    try:
        fn(*args)
    except Exception:
        logger.exception("database event handler %s failed", getattr(fn, "__name__", fn))


# One per process, used by main.py (start/stop) and the SSE endpoint.
dispatcher = DbEventDispatcher()
