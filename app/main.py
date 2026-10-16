"""The FastAPI application: startup/shutdown and the three endpoints."""

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import aiomysql
from fastapi import Depends, FastAPI, HTTPException

from app import db
from app.schemas import WebhookPayload

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Runs before the first request (above the yield) and at shutdown (below it)."""
    await db.open_pool()
    await db.apply_schema()
    log.info("database ready")
    yield
    await db.close_pool()
    log.info("database pool closed")


app = FastAPI(title="Transaction Webhook Service", lifespan=lifespan)


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

    if inserted:
        log.info("accepted transaction %s", payload.transaction_id)
    else:
        log.info("duplicate webhook for transaction %s ignored", payload.transaction_id)

    # Same answer either way: the sender only needs to know we have it.
    return {"status": "accepted", "transaction_id": payload.transaction_id}


@app.get("/v1/transactions/{transaction_id}")
async def get_transaction(
    transaction_id: str,
    conn: aiomysql.Connection = Depends(db.get_connection),
):
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
