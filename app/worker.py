"""Background worker: turns PROCESSING rows into PROCESSED ones.

The transactions table is the job queue. A row with status PROCESSING is a
job waiting to be done. The worker loop:

  1. claims up to N due rows (SELECT ... FOR UPDATE SKIP LOCKED)
  2. starts one task per row: wait the simulated delay, then mark PROCESSED
  3. on failure, records the error and schedules a retry, or marks FAILED
  4. sleeps briefly when there is nothing to do, then repeats

Runs inside the API process by default (see main.py), or on its own with
`python -m app.worker`.
"""

import asyncio
import logging
import sys

from app import db
from app.config import settings

log = logging.getLogger(__name__)


async def claim_batch(limit: int) -> list[int]:
    """Pick up to `limit` rows that are due, mark them claimed, return their ids.

    Runs in one transaction so the row locks from FOR UPDATE are held until
    the UPDATE commits. SKIP LOCKED makes a second worker skip rows this one
    is holding instead of waiting, so several workers never pick the same row.
    """
    async with db.pool.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT id
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
                ids = [row["id"] for row in await cur.fetchall()]
                if ids:
                    # attempts counts how many times a row has been claimed.
                    await cur.execute(
                        "UPDATE transactions SET claimed_at = NOW(), attempts = attempts + 1 WHERE id IN %s",
                        (ids,),
                    )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
    return ids


async def call_external_api(row_id: int) -> None:
    """Stand-in for the real payment-processor call the assessment asks us to simulate."""
    await asyncio.sleep(settings.processing_delay_seconds)


async def process_one(row_id: int) -> None:
    try:
        await call_external_api(row_id)
        async with db.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE transactions
                    SET status = 'PROCESSED', processed_at = NOW(), claimed_at = NULL
                    WHERE id = %s AND status = 'PROCESSING'
                    """,
                    (row_id,),
                )
        log.info("processed row %s", row_id)
    except Exception as exc:
        log.exception("processing row %s failed", row_id)
        try:
            await record_failure(row_id, str(exc))
        except Exception:
            # Database unreachable. The row stays claimed; the stale-claim
            # timeout in claim_batch will hand it to a worker again later.
            log.exception("could not record the failure for row %s", row_id)


async def record_failure(row_id: int, error: str) -> None:
    """Release the row for a retry after a delay, or mark it FAILED after the last attempt."""
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE transactions
                SET status = IF(attempts >= %s, 'FAILED', 'PROCESSING'),
                    last_error = %s,
                    claimed_at = NULL,
                    next_attempt_at = NOW() + INTERVAL %s SECOND
                WHERE id = %s
                """,
                (settings.worker_max_attempts, error[:2000], settings.worker_retry_delay_seconds, row_id),
            )


async def release_claims(row_ids: list[int]) -> None:
    """On shutdown, un-claim rows we were still working on so a restart picks them up at once."""
    if not row_ids:
        return
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE transactions SET claimed_at = NULL WHERE id IN %s AND status = 'PROCESSING'",
                (tuple(row_ids),),
            )


async def run_worker() -> None:
    log.info("worker started, concurrency=%s", settings.worker_concurrency)
    in_flight: dict[asyncio.Task, int] = {}  # task -> row id
    try:
        while True:
            # Forget tasks that have finished.
            for task in [t for t in in_flight if t.done()]:
                del in_flight[task]

            # Only claim as many rows as we have free slots for.
            free_slots = settings.worker_concurrency - len(in_flight)
            ids: list[int] = []
            if free_slots > 0:
                try:
                    ids = await claim_batch(free_slots)
                except Exception:
                    log.exception("could not look for work, will retry")

            for row_id in ids:
                in_flight[asyncio.create_task(process_one(row_id))] = row_id

            if not ids:
                await asyncio.sleep(settings.worker_poll_interval_seconds)
    finally:
        # Shutdown: stop the in-flight tasks and give their rows back to the queue.
        for task in in_flight:
            task.cancel()
        if in_flight:
            try:
                await asyncio.shield(release_claims(list(in_flight.values())))
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
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if sys.platform == "win32":
        # Windows' default event loop cannot do the SSL handshake aiomysql needs.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
