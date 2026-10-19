"""Background worker: turns PROCESSING rows into PROCESSED ones.

The transactions table is the job queue. A row with status PROCESSING is a
job waiting to be done. The worker loop:

  1. claims every due row, up to a batch size (SELECT ... FOR UPDATE SKIP LOCKED)
  2. starts one task for the whole batch: wait the simulated delay once, then
     mark every row PROCESSED with a single UPDATE ... WHERE id IN (...)
  3. on failure, records the error for the batch and schedules a retry,
     or marks the rows FAILED
  4. sleeps briefly when there is nothing to do, then repeats

Batches overlap: the loop does not wait for a batch to finish before claiming
the next one, so a webhook that arrives while an earlier batch is sleeping
gets its own batch within one poll interval. Every row therefore finishes
about `processing_delay_seconds` after it was received, however many arrive
at once, and 1,000 rows cost the database one UPDATE instead of 1,000.

Runs inside the API process by default (see main.py), or on its own with
`python -m app.worker`.
"""

import asyncio
import logging
import sys
from time import perf_counter

from app import db
from app.config import settings
from app.timing import measure

log = logging.getLogger(__name__)
_work_available: asyncio.Event | None = None


def notify_work() -> None:
    """Wake this process's worker; polling still discovers work from other processes."""
    if _work_available is not None:
        _work_available.set()


async def claim_batch(limit: int) -> list[int]:
    """Pick up to `limit` rows that are due, mark them claimed, return their ids.

    Runs in one transaction so the row locks from FOR UPDATE are held until
    the UPDATE commits. SKIP LOCKED makes a second worker skip rows this one
    is holding instead of waiting, so several workers never pick the same row.
    """
    started = perf_counter()
    async with db.connection("worker.claim", level=logging.DEBUG) as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT id,
                           TIMESTAMPDIFF(MICROSECOND, created_at, NOW(6)) / 1000 AS row_age_ms
                    FROM transactions
                    WHERE status = 'PROCESSING'
                      AND next_attempt_at <= NOW()
                      AND (claimed_at IS NULL
                           OR claimed_at < NOW() - INTERVAL %s SECOND)
                    ORDER BY next_attempt_at
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                    """,
                    (settings.worker_stale_claim_seconds, limit),
                )
                rows = await cur.fetchall()
                ids = [row["id"] for row in rows]
                if ids:
                    # One UPDATE for the whole batch. The driver renders the
                    # tuple as (1, 2, 3), so IN %s works for any batch size.
                    # attempts counts how many times a row has been claimed.
                    await cur.execute(
                        "UPDATE transactions SET claimed_at = NOW(), attempts = attempts + 1 WHERE id IN %s",
                        (tuple(ids),),
                    )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
    elapsed_ms = (perf_counter() - started) * 1000
    if ids:
        oldest_ms = max(row["row_age_ms"] for row in rows)
        log.info("worker claimed count=%s oldest_row_age_ms=%s claim_batch_ms=%.2f",
                 len(ids), oldest_ms, elapsed_ms)
    else:
        log.debug("worker empty claim_batch_ms=%.2f", elapsed_ms)
    return ids


async def call_external_api(row_ids: list[int]) -> None:
    """Stand-in for the real payment-processor call the assessment asks us to simulate.

    One wait covers the whole batch: sleeping once for 1,000 rows is the same
    simulated delay as 1,000 rows each sleeping at the same time.
    """
    await asyncio.sleep(settings.processing_delay_seconds)


async def process_batch(row_ids: list[int]) -> None:
    started = perf_counter()
    try:
        # No database connection is borrowed during this wait.
        with measure("worker.external_call", count=len(row_ids),
                     configured_delay_seconds=settings.processing_delay_seconds):
            await call_external_api(row_ids)
        async with db.connection("worker.complete") as conn:
            with measure("sql.worker.complete", count=len(row_ids)):
                async with conn.cursor() as cur:
                    # The single bulk update: every id in the batch at once.
                    await cur.execute(
                        """
                        UPDATE transactions
                        SET status = 'PROCESSED', processed_at = NOW(), claimed_at = NULL
                        WHERE id IN %s AND status = 'PROCESSING'
                        """,
                        (tuple(row_ids),),
                    )
        log.info("processed count=%s processing_ms=%.2f", len(row_ids),
                 (perf_counter() - started) * 1000)
    except Exception as exc:
        log.exception("processing batch of %s rows failed", len(row_ids))
        try:
            await record_failure(row_ids, str(exc))
        except Exception:
            # Database unreachable. The rows stay claimed; the stale-claim
            # timeout in claim_batch will hand them to a worker again later.
            log.exception("could not record the failure for %s rows", len(row_ids))


async def record_failure(row_ids: list[int], error: str) -> None:
    """Release the rows for a retry after a delay, or mark them FAILED after the last attempt."""
    async with db.connection("worker.failure") as conn, conn.cursor() as cur:
        with measure("sql.worker.failure", count=len(row_ids)):
            await cur.execute(
                """
                UPDATE transactions
                SET status = IF(attempts >= %s, 'FAILED', 'PROCESSING'),
                    last_error = %s,
                    claimed_at = NULL,
                    next_attempt_at = NOW() + INTERVAL %s SECOND
                WHERE id IN %s
                """,
                (settings.worker_max_attempts, error[:2000], settings.worker_retry_delay_seconds,
                 tuple(row_ids)),
            )


async def release_claims(row_ids: list[int]) -> None:
    """On shutdown, un-claim rows we were still working on so a restart picks them up at once."""
    if not row_ids:
        return
    async with db.connection("worker.release") as conn, conn.cursor() as cur:
        with measure("sql.worker.release", count=len(row_ids)):
            await cur.execute(
                "UPDATE transactions SET claimed_at = NULL WHERE id IN %s AND status = 'PROCESSING'",
                (tuple(row_ids),),
            )


async def run_worker() -> None:
    global _work_available
    # A new Event per run avoids retaining an event loop across app restarts.
    work_available = asyncio.Event()
    _work_available = work_available
    log.info("worker started, batch_size=%s", settings.worker_batch_size)
    in_flight: dict[asyncio.Task, list[int]] = {}  # task -> row ids in that batch
    try:
        while True:
            # Clear before querying: notifications arriving during the query
            # must remain set so a just-committed row is not delayed by a poll.
            work_available.clear()
            # Forget batches that have finished.
            for task in [t for t in in_flight if t.done()]:
                del in_flight[task]

            ids: list[int] = []
            try:
                ids = await claim_batch(settings.worker_batch_size)
            except Exception:
                log.exception("could not look for work, will retry")

            if ids:
                # Start this batch and go straight back to claiming: more rows
                # may have arrived, or more than one batch may have been due.
                task = asyncio.create_task(process_batch(ids))
                in_flight[task] = ids
            else:
                try:
                    await asyncio.wait_for(
                        work_available.wait(), timeout=settings.worker_poll_interval_seconds,
                    )
                except TimeoutError:
                    pass
    finally:
        _work_available = None
        # Shutdown: stop the in-flight batches and give their rows back to the queue.
        for task in in_flight:
            task.cancel()
        if in_flight:
            await asyncio.gather(*in_flight, return_exceptions=True)
            held = [row_id for ids in in_flight.values() for row_id in ids]
            try:
                await asyncio.shield(release_claims(held))
            except Exception:
                log.exception("failed to release claims on shutdown")
        log.info("worker stopped")


async def main() -> None:
    """Entry point for running the worker as its own process."""
    await db.open_pool()
    await db.apply_schema()
    try:
        await run_worker()
    finally:
        await db.close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if sys.platform == "win32":
        # Windows' default event loop cannot do the SSL handshake aiomysql needs.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
