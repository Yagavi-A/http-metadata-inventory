"""In-process background worker pool.

The challenge explicitly forbids "loops or external service-to-self HTTP
calls" for the asynchronous collection.  We therefore run a small pool of
``asyncio`` consumer tasks attached to the FastAPI application lifespan;
they pull jobs from an :class:`asyncio.Queue` and invoke the
:class:`MetadataService` directly.  No broker, no extra process, but the
fetch happens outside of the request/response cycle.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FetchJob:
    """Unit of work consumed by the worker pool."""

    normalized_url: str
    original_url: str


JobHandler = Callable[[FetchJob], Awaitable[None]]


class BackgroundWorker:
    """A bounded queue with a fixed-size pool of consumers."""

    def __init__(
        self,
        *,
        handler: JobHandler,
        concurrency: int = 2,
        queue_maxsize: int = 1000,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        self._handler = handler
        self._concurrency = concurrency
        self._queue: asyncio.Queue[FetchJob] = asyncio.Queue(maxsize=queue_maxsize)
        self._tasks: list[asyncio.Task[None]] = []
        self._in_flight: set[str] = set()
        self._lock = asyncio.Lock()
        self._started = False

    async def start(self) -> None:
        """Spawn the consumer tasks (idempotent)."""
        if self._started:
            return
        self._started = True
        for i in range(self._concurrency):
            task = asyncio.create_task(self._run(i), name=f"metadata-worker-{i}")
            self._tasks.append(task)
        logger.info("Started %d background worker(s)", self._concurrency)

    async def stop(self) -> None:
        """Cancel all consumer tasks and wait for them to exit."""
        if not self._started:
            return
        self._started = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                logger.exception("Worker task raised on shutdown")
        self._tasks.clear()
        logger.info("Background worker pool stopped")

    async def submit(self, job: FetchJob) -> bool:
        """Enqueue a job.  Returns ``False`` if the URL is already queued.

        We dedupe on the normalized URL so a burst of GETs for the same
        missing URL only triggers a single fetch.
        """
        async with self._lock:
            if job.normalized_url in self._in_flight:
                return False
            self._in_flight.add(job.normalized_url)
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            async with self._lock:
                self._in_flight.discard(job.normalized_url)
            logger.warning("Worker queue is full; dropping %s", job.normalized_url)
            return False
        return True

    @property
    def queue_size(self) -> int:
        """Current number of jobs waiting to be processed."""
        return self._queue.qsize()

    async def join(self) -> None:
        """Wait for the current backlog to drain.  Used by tests."""
        await self._queue.join()

    async def _run(self, idx: int) -> None:
        logger.debug("Worker %d ready", idx)
        while True:
            job = await self._queue.get()
            try:
                await self._handler(job)
            except Exception:  # noqa: BLE001 - handler must never crash the loop
                logger.exception("Background handler raised for %s", job.normalized_url)
            finally:
                async with self._lock:
                    self._in_flight.discard(job.normalized_url)
                self._queue.task_done()


_worker: BackgroundWorker | None = None


def set_worker(worker: BackgroundWorker | None) -> None:
    """Module-level holder used by API dependencies."""
    global _worker
    _worker = worker


def get_worker() -> BackgroundWorker:
    """Return the active worker; raises if it has not been started."""
    if _worker is None:
        raise RuntimeError("Background worker is not initialised")
    return _worker
