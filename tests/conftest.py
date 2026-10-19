"""Shared test setup.

Everything in the `os.environ` block must run BEFORE the app is imported,
because app.config builds its Settings object at import time. The tests
point the app at a separate database and shorten every delay so the whole
suite finishes in seconds.
"""

import asyncio
import os
import ssl
import sys
import time

import pymysql
import pytest

os.environ["DB_NAME"] = "walnutfolks_test"
os.environ["PROCESSING_DELAY_SECONDS"] = "1"
os.environ["WORKER_POLL_INTERVAL_SECONDS"] = "0.2"
os.environ["WORKER_RETRY_DELAY_SECONDS"] = "1"
os.environ["WORKER_MAX_ATTEMPTS"] = "2"

if sys.platform == "win32":
    # Windows' default event loop cannot do the SSL handshake aiomysql needs.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from fastapi.testclient import TestClient  # noqa: E402  (must come after the env setup)

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402


def run_sql(query, args=()):
    """Run one statement against the test database and return all rows.

    Uses a plain synchronous connection, separate from the app's pool, so
    the tests can inspect the table without touching the app's internals.
    """
    ssl_context = None
    if settings.db_ssl:
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
    conn = pymysql.connect(
        host=settings.db_host, port=settings.db_port, user=settings.db_user,
        password=settings.db_password, database=settings.db_name, ssl=ssl_context,
        autocommit=True, cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(query, args)
            return cur.fetchall()
    finally:
        conn.close()


@pytest.fixture(scope="module")
def client():
    """One running app per API test module, stopped before isolated worker tests."""
    with TestClient(app) as test_client:  # entering starts lifespan: pool, schema, worker
        run_sql("DELETE FROM transactions")
        yield test_client


def wait_for_status(client, transaction_id, wanted, timeout=10):
    """Poll the query endpoint until the transaction reaches `wanted` or time runs out."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = client.get(f"/v1/transactions/{transaction_id}").json()[0]
        if row["status"] == wanted:
            return row
        time.sleep(0.2)
    raise AssertionError(f"{transaction_id} did not reach {wanted} within {timeout}s (last: {row['status']})")
