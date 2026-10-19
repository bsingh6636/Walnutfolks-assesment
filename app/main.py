"""The FastAPI application: startup/shutdown and the three endpoints."""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from time import perf_counter
from uuid import uuid4

import aiomysql
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from app import db, worker
from app.config import settings
from app.schemas import WebhookPayload
from app.timing import measure, request_id

logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Runs before the first request (above the yield) and at shutdown (below it)."""
    await db.open_pool()
    await db.apply_schema()
    log.info("database ready")

    worker_task = None
    if settings.run_worker_in_api:
        # The worker loop runs alongside the request handlers in the same event loop.
        worker_task = asyncio.create_task(worker.run_worker())

    yield

    if worker_task is not None:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
    await db.close_pool()
    log.info("database pool closed")


app = FastAPI(title="Transaction Webhook Service", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_request_timing(request: Request, call_next):
    token = request_id.set(uuid4().hex)
    started = perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers["Server-Timing"] = f"app;dur={(perf_counter() - started) * 1000:.2f}"
        response.headers["X-Request-ID"] = request_id.get()
        return response
    finally:
        route = request.scope.get("route")
        log.info(
            "http method=%s route=%s status=%s elapsed_ms=%.2f request_id=%s",
            request.method, getattr(route, "path", "unmatched"), status_code,
            (perf_counter() - started) * 1000, request_id.get(),
        )
        request_id.reset(token)


def iso_z(value: datetime | None) -> str | None:
    """Render a UTC datetime the way the spec shows it: 2024-01-15T10:30:00Z."""
    if value is None:
        return None
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


@app.get("/")
async def health_check():
    return {
        "status": "HEALTHY",
        "current_time": iso_z(datetime.now(timezone.utc)),
    }


@app.post("/v1/webhooks/transactions", status_code=202)
async def receive_webhook(
    payload: WebhookPayload,
    conn: aiomysql.Connection = Depends(db.get_connection),
):
    # One INSERT. If the transaction_id already exists, the UNIQUE KEY makes
    # MySQL take the ON DUPLICATE KEY branch, which changes nothing.
    # Everything not listed here (status, attempts, timestamps) uses the
    # column defaults from schema.sql.
    with measure("sql.webhook.insert", transaction_id=payload.transaction_id):
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO transactions
                    (transaction_id, source_account, destination_account, amount, currency)
                VALUES (%s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE id = id
                """,
                (
                    payload.transaction_id,
                    payload.source_account,
                    payload.destination_account,
                    payload.amount,
                    payload.currency,
                ),
            )
            # rowcount is 1 when a row was inserted, 0 when the duplicate branch ran.
            inserted = cur.rowcount == 1
            row_id = cur.lastrowid

    if inserted:
        log.info("accepted transaction_id=%s row_id=%s request_id=%s",
                 payload.transaction_id, row_id, request_id.get())
        # The INSERT has committed (autocommit). Wake the local worker without
        # waiting for its next poll; MySQL remains the durable source of work.
        worker.notify_work()
    else:
        log.info("duplicate webhook for transaction %s ignored", payload.transaction_id)

    # Same answer either way: the sender only needs to know we have it.
    return {"status": "accepted", "transaction_id": payload.transaction_id}


@app.get("/v1/transactions/{transaction_id}")
async def get_transaction(
    transaction_id: str,
    conn: aiomysql.Connection = Depends(db.get_connection),
):
    with measure("sql.transaction.select", transaction_id=transaction_id):
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT transaction_id, source_account, destination_account,
                       amount, currency, status, created_at, processed_at
                FROM transactions
                WHERE transaction_id = %s
                """,
                (transaction_id,),
            )
            row = await cur.fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="transaction not found")

    # The spec wants a list containing the one transaction.
    return [
        {
            "transaction_id": row["transaction_id"],
            "source_account": row["source_account"],
            "destination_account": row["destination_account"],
            "amount": row["amount"],
            "currency": row["currency"],
            "status": row["status"],
            "created_at": iso_z(row["created_at"]),
            "processed_at": iso_z(row["processed_at"]),
        }
    ]
