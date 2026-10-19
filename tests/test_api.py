import logging
import time
import uuid

from app import worker
from tests.conftest import run_sql, wait_for_status


def payload(**overrides):
    """A valid webhook body with a fresh transaction_id, overridable per test."""
    body = {
        "transaction_id": f"txn_{uuid.uuid4().hex[:12]}",
        "source_account": "acc_user_789",
        "destination_account": "acc_merchant_456",
        "amount": 1500,
        "currency": "INR",
    }
    body.update(overrides)
    return body


def test_health_check(client):
    response = client.get("/")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "HEALTHY"
    assert body["current_time"].endswith("Z")


def test_request_timings_correlate_database_work(client, caplog):
    with caplog.at_level(logging.INFO):
        response = client.get("/v1/transactions/timing_missing")
    assert response.status_code == 404
    assert response.headers["Server-Timing"].startswith("app;dur=")
    request_id = response.headers["X-Request-ID"]
    messages = [record.getMessage() for record in caplog.records
                if f"request_id={request_id}" in record.getMessage()]
    assert any("operation=db.pool.acquire" in message for message in messages)
    assert any("operation=sql.transaction.select" in message for message in messages)
    assert any("operation=db.connection.held" in message for message in messages)
    assert any("http method=GET" in message and "status=404" in message for message in messages)


def test_webhook_is_accepted_quickly(client):
    body = payload()
    started = time.perf_counter()
    response = client.post("/v1/webhooks/transactions", json=body)
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert response.status_code == 202
    assert response.json() == {"status": "accepted", "transaction_id": body["transaction_id"]}
    assert elapsed_ms < 500


def test_duplicate_webhooks_create_one_transaction(client):
    body = payload()
    for _ in range(3):
        assert client.post("/v1/webhooks/transactions", json=body).status_code == 202

    # a different amount with the same id is also ignored: first write wins
    changed = payload(transaction_id=body["transaction_id"], amount=1)
    assert client.post("/v1/webhooks/transactions", json=changed).status_code == 202

    rows = run_sql("SELECT amount FROM transactions WHERE transaction_id = %s", (body["transaction_id"],))
    assert len(rows) == 1
    assert rows[0]["amount"] == 1500


def test_invalid_payload_is_rejected(client):
    response = client.post("/v1/webhooks/transactions", json=payload(amount=-5, currency="inr"))
    assert response.status_code == 422
    bad_fields = {error["loc"][-1] for error in response.json()["detail"]}
    assert bad_fields == {"amount", "currency"}


def test_unknown_transaction_returns_404(client):
    response = client.get("/v1/transactions/does_not_exist")
    assert response.status_code == 404
    assert response.json() == {"detail": "transaction not found"}


def test_transaction_is_processed_after_delay(client):
    body = payload()
    client.post("/v1/webhooks/transactions", json=body)

    row = client.get(f"/v1/transactions/{body['transaction_id']}").json()[0]
    assert row["status"] == "PROCESSING"
    assert row["processed_at"] is None

    row = wait_for_status(client, body["transaction_id"], "PROCESSED")
    assert row["processed_at"] is not None
    assert row["processed_at"] >= row["created_at"]
    assert row["amount"] == 1500
    assert row["currency"] == "INR"


def test_many_webhooks_are_all_processed(client):
    ids = []
    for _ in range(25):
        body = payload()
        assert client.post("/v1/webhooks/transactions", json=body).status_code == 202
        ids.append(body["transaction_id"])

    for transaction_id in ids:
        wait_for_status(client, transaction_id, "PROCESSED", timeout=15)


def test_failed_processing_is_retried_then_marked_failed(client, monkeypatch):
    # Insert the row directly with next_attempt_at in the future, so the worker
    # cannot claim it until the rigged failure below is in place.
    transaction_id = payload()["transaction_id"]
    run_sql(
        """
        INSERT INTO transactions (transaction_id, source_account, destination_account, amount, currency, next_attempt_at)
        VALUES (%s, 'a', 'b', 1, 'INR', NOW() + INTERVAL 1 HOUR)
        """,
        (transaction_id,),
    )
    doomed_row = run_sql("SELECT id FROM transactions WHERE transaction_id = %s", (transaction_id,))[0]["id"]

    real_external_api = worker.call_external_api

    async def failing_external_api(row_ids):
        if doomed_row in row_ids:
            raise RuntimeError("simulated payment processor timeout")
        await real_external_api(row_ids)

    monkeypatch.setattr(worker, "call_external_api", failing_external_api)

    # Release the row to the worker.
    run_sql("UPDATE transactions SET next_attempt_at = NOW() WHERE id = %s", (doomed_row,))

    wait_for_status(client, transaction_id, "FAILED")
    row = run_sql("SELECT attempts, last_error FROM transactions WHERE id = %s", (doomed_row,))[0]
    assert row["attempts"] == 2
    assert "simulated payment processor timeout" in row["last_error"]
