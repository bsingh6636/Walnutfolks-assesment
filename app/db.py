"""Database connection pool.

A pool keeps a few MySQL connections open and lends one out per request,
instead of paying the cost of a new connection every time.
"""

import ssl
import warnings
from pathlib import Path

import aiomysql

from app.config import settings

# Filled in by open_pool() at startup, emptied by close_pool() at shutdown.
pool: aiomysql.Pool | None = None

SCHEMA_FILE = Path(__file__).with_name("schema.sql")


async def open_pool() -> None:
    global pool

    ssl_context = None
    if settings.db_ssl:
        # Encrypt the connection. The server certificate is not verified, which is
        # fine for this assessment; a production service would pin the provider's CA.
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

    pool = await aiomysql.create_pool(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        db=settings.db_name,
        ssl=ssl_context,
        minsize=settings.db_pool_minsize,
        maxsize=settings.db_pool_maxsize,
        pool_recycle=settings.db_pool_recycle_seconds,
        autocommit=True,  # each statement commits on its own unless we open a transaction
        cursorclass=aiomysql.DictCursor,  # rows come back as dicts: row["status"]
        init_command="SET time_zone = '+00:00'",  # every NOW() in this app is UTC
    )


async def close_pool() -> None:
    pool.close()
    await pool.wait_closed()


async def apply_schema() -> None:
    """Create the tables if they do not exist yet. Runs at every startup."""
    schema_sql = SCHEMA_FILE.read_text()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            # MySQL reports "table already exists" as a note when IF NOT EXISTS
            # skips the create; the driver turns it into a Python warning. Hide it.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                await cur.execute(schema_sql)


async def get_connection():
    """FastAPI dependency: borrow one connection for the request, return it after."""
    async with pool.acquire() as conn:
        yield conn
