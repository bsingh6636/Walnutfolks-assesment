import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import db, worker
from app.config import settings


@pytest.mark.parametrize("during_claim", [False, True])
def test_webhook_notification_wakes_worker_without_waiting_for_poll(monkeypatch, during_claim):
    async def scenario():
        querying = asyncio.Event()
        finish_query = asyncio.Event()
        processing = asyncio.Event()
        calls = 0

        async def claim(limit):
            nonlocal calls
            calls += 1
            if calls == 1:
                querying.set()
                if during_claim:
                    await finish_query.wait()
                return []
            # The real claim returns a row only once; after that the queue is empty.
            return [42] if calls == 2 else []

        async def process(row_ids):
            assert row_ids == [42]
            processing.set()
            await asyncio.Event().wait()

        release = AsyncMock()
        monkeypatch.setattr(settings, "worker_poll_interval_seconds", 60)
        monkeypatch.setattr(worker, "claim_batch", claim)
        monkeypatch.setattr(worker, "process_batch", process)
        monkeypatch.setattr(worker, "release_claims", release)
        task = asyncio.create_task(worker.run_worker())
        try:
            await asyncio.wait_for(querying.wait(), timeout=1)
            worker.notify_work()
            finish_query.set()
            await asyncio.wait_for(processing.wait(), timeout=1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        release.assert_awaited_once_with([42])
        assert worker._work_available is None

    asyncio.run(scenario())


def test_next_batch_starts_while_previous_batch_is_still_waiting(monkeypatch):
    """Rows arriving during a batch's 30 second wait must not wait for that batch."""
    async def scenario():
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        batches = iter([[1, 2], [3]])

        async def claim(limit):
            assert limit == settings.worker_batch_size
            return next(batches, [])

        async def process(row_ids):
            if row_ids == [1, 2]:
                first_started.set()
            else:
                assert row_ids == [3]
                second_started.set()
            await asyncio.Event().wait()  # simulate a batch that never finishes

        monkeypatch.setattr(settings, "worker_poll_interval_seconds", 60)
        monkeypatch.setattr(worker, "claim_batch", claim)
        monkeypatch.setattr(worker, "process_batch", process)
        release = AsyncMock()
        monkeypatch.setattr(worker, "release_claims", release)
        task = asyncio.create_task(worker.run_worker())
        try:
            await asyncio.wait_for(first_started.wait(), timeout=1)
            await asyncio.wait_for(second_started.wait(), timeout=1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        # Shutdown releases every row from every unfinished batch.
        release.assert_awaited_once_with([1, 2, 3])

    asyncio.run(scenario())


def test_batch_marks_all_rows_with_one_update_after_the_delay(monkeypatch):
    async def scenario():
        external_started = asyncio.Event()
        finish_external = asyncio.Event()
        borrowed = False
        execute = AsyncMock()

        async def external(row_ids):
            external_started.set()
            await finish_external.wait()

        @asynccontextmanager
        async def connection(operation):
            nonlocal borrowed
            borrowed = True
            cursor = MagicMock()
            cursor.execute = execute
            conn = MagicMock()
            conn.cursor.return_value.__aenter__ = AsyncMock(return_value=cursor)
            yield conn

        monkeypatch.setattr(worker, "call_external_api", external)
        monkeypatch.setattr(db, "connection", connection)
        task = asyncio.create_task(worker.process_batch([7, 8, 9]))
        try:
            await asyncio.wait_for(external_started.wait(), timeout=1)
            # No database connection is held during the simulated external call.
            assert not borrowed
            finish_external.set()
            await asyncio.wait_for(task, timeout=1)
            assert borrowed
            # One UPDATE for the whole batch, with every id in the IN list.
            execute.assert_awaited_once()
            sql, params = execute.await_args.args
            assert "UPDATE transactions" in sql and "IN %s" in sql
            assert params == ((7, 8, 9),)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
