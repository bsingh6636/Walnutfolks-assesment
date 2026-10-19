# Transaction Webhook Service

A small Python service that receives transaction webhooks from a payment
processor, acknowledges them immediately with `202 Accepted`, and processes
each transaction reliably in the background with a simulated 30 second delay.
Duplicate webhooks for the same transaction are accepted but never processed
twice.

**Live endpoint:** https://walnutfolks-assesment.brijeshhq.com

## API

| Method | Path                                 | Purpose                                   |
| ------ | ------------------------------------ | ----------------------------------------- |
| GET    | `/`                                  | Health check                              |
| POST   | `/v1/webhooks/transactions`          | Receive a webhook, returns 202 at once    |
| GET    | `/v1/transactions/{transaction_id}`  | Look up a transaction and its status      |

Health check:

```bash
curl https://walnutfolks-assesment.brijeshhq.com/
# {"status":"HEALTHY","current_time":"2026-10-17T07:18:19Z"}
```

Send a webhook:

```bash
curl -i -X POST https://walnutfolks-assesment.brijeshhq.com/v1/webhooks/transactions \
  -H "Content-Type: application/json" \
  -d '{"transaction_id":"txn_abc123def456","source_account":"acc_user_789","destination_account":"acc_merchant_456","amount":1500,"currency":"INR"}'
# HTTP/1.1 202 Accepted
# {"status":"accepted","transaction_id":"txn_abc123def456"}
```

Check its status (`PROCESSING` first, `PROCESSED` about 30 seconds later):

```bash
curl https://walnutfolks-assesment.brijeshhq.com/v1/transactions/txn_abc123def456
# [{"transaction_id":"txn_abc123def456","source_account":"acc_user_789",
#   "destination_account":"acc_merchant_456","amount":1500.0,"currency":"INR",
#   "status":"PROCESSED","created_at":"2026-10-17T07:13:44Z","processed_at":"2026-10-17T07:14:15Z"}]
```

Validation: ids are 1 to 64 characters, `amount` is positive with at most two
decimal places, `currency` is three uppercase letters. Anything else returns
`422` naming the bad field. An unknown transaction id returns `404`.

## How it works

```
POST /v1/webhooks/transactions
        |
        v
  INSERT INTO transactions ... ON DUPLICATE KEY UPDATE id = id      <- idempotent
        |
        v
  202 Accepted (a few milliseconds)

  background worker loop (same process)
        |
        v
  SELECT ... WHERE status = 'PROCESSING' AND next_attempt_at <= NOW()
         ... FOR UPDATE SKIP LOCKED                                   <- safe with many workers
        |
        v
  sleep 30 s (simulated external call), then UPDATE ... status = 'PROCESSED'
```

* **Idempotency** is enforced by MySQL, not by Python: `transaction_id` has a
  `UNIQUE KEY`, so a repeated webhook hits the `ON DUPLICATE KEY` branch and
  changes nothing. The sender gets the same `202` either way, because payment
  processors retry on anything else.
* **The table is the queue.** A row with status `PROCESSING` is a job waiting
  to be done. Nothing lives only in memory, so a restart or crash loses no
  transaction.
* **Batches, not slots.** Each loop claims every due row (up to
  `WORKER_BATCH_SIZE`) as one batch, waits the 30 seconds once for the batch,
  then marks all of them with a single `UPDATE ... WHERE id IN (...)`. The loop
  does not wait for a batch to finish before claiming the next, so rows that
  arrive during a batch's wait start their own batch within a second. A burst
  of 1,000 webhooks is therefore processed about 31 seconds after receipt and
  costs the database one UPDATE, not 1,000. Claiming rows with
  `FOR UPDATE SKIP LOCKED` means several worker processes can share the table
  without ever picking the same row. A newly inserted webhook wakes the worker
  in the same process immediately; polling remains as a fallback for retries,
  restarts, and rows inserted by other API processes.
* **Failures.** A failed attempt records the error, releases the row, and
  schedules a retry 30 seconds later. After 3 attempts the row is marked
  `FAILED`. A row claimed by a worker that died is reclaimed after 5 minutes.
  A clean shutdown releases in-flight claims so a restart resumes at once.

## Running locally

Requirements: Python 3.13 and a MySQL 8 database (a free hosted one from
Aiven works well).

```bash
python -m venv venv
venv\Scripts\activate          # Windows;  on macOS/Linux:  source venv/bin/activate
pip install -r requirements.txt
copy .env.example .env         # then fill in the DB_* values
uvicorn app.main:app --reload
```

The table is created automatically at startup from `app/schema.sql`.
The service is now at http://127.0.0.1:8000, with interactive docs at
http://127.0.0.1:8000/docs.

Windows note: keep `--reload`. Without it uvicorn on Windows picks an event
loop that cannot complete the TLS handshake the MySQL driver needs. Linux
and macOS have no such issue.

Configuration is read from environment variables or `.env`:

| Variable                     | Default | Meaning                                           |
| ---------------------------- | ------- | ------------------------------------------------- |
| `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME` | | MySQL connection |
| `DB_SSL`                     | `true`  | Encrypt the connection (required by hosted MySQL) |
| `DB_POOL_MINSIZE`            | `5`     | Connections opened at startup and kept ready     |
| `DB_POOL_MAXSIZE`            | `30`    | Maximum pooled connections per process           |
| `DB_POOL_RECYCLE_SECONDS`    | `300`   | Recycle a connection after this much idle time    |
| `LOG_LEVEL`                 | `INFO`  | Use `DEBUG` to include idle worker pool timings   |
| `PROCESSING_DELAY_SECONDS`   | `30`    | Simulated external call duration                  |
| `RUN_WORKER_IN_API`          | `true`  | Run the worker inside the API process             |
| `WORKER_BATCH_SIZE`          | `1000`  | Most rows one claim takes; one wait and one UPDATE per batch |
| `WORKER_POLL_INTERVAL_SECONDS` | `1`   | Fallback polling interval; local inserts wake immediately |
| `WORKER_MAX_ATTEMPTS`        | `3`     | Attempts before a row is marked `FAILED`          |
| `WORKER_RETRY_DELAY_SECONDS` | `30`    | Wait between attempts                             |
| `WORKER_STALE_CLAIM_SECONDS` | `300`   | Reclaim rows held by a dead worker after this     |

To run the worker as its own process instead, set `RUN_WORKER_IN_API=false`
for the API and start `python -m app.worker` separately. Several worker
processes can run at once.

## Running the tests

```bash
pip install -r requirements-dev.txt
pytest
```

The tests use a separate database named `walnutfolks_test` on the same MySQL
server (create it once with `CREATE DATABASE walnutfolks_test`). Delays are
shortened to about a second so the suite finishes in roughly 15 seconds. It
covers the health check, the 202 response time, duplicate prevention,
validation, 404, processing after the delay, 25 concurrent transactions, and
retry-then-`FAILED`.

To see the real 30 second behaviour, send a webhook with curl as shown above
and query the transaction twice, 30 seconds apart.

## Simulation & Load Testing (Production & Local)

A comprehensive simulation CLI tool is available in `scripts/simulate.py`:

```bash
# Single transaction lifecycle (health -> webhook -> immediate query -> duplicate idempotency -> 30s completion wait):
python scripts/simulate.py --mode single

# Low volume test (10 transactions, 5 concurrent):
python scripts/simulate.py --mode batch --count 10 --concurrency 5

# High volume load test (40 transactions, 10 concurrent, 20% duplicate injection, wait for completion):
python scripts/simulate.py --mode batch --count 40 --concurrency 10 --duplicates 20 --wait

# Test a local deployment:
python scripts/simulate.py --url http://127.0.0.1:8000 --mode single
```

It outputs throughput, p50/p90/p95/p99 latency distribution, SLA compliance (<500ms), and real-time background worker queue draining metrics.

To investigate latency, run:

```bash
python scripts/simulate.py --url http://127.0.0.1:8000 --mode single --timings
```

The simulator reuses an HTTP connection per thread and measures the entire
response, including its body. `--timings` prints client latency, whether the
socket was reused, the `Server-Timing` header, and `X-Request-ID`. The server
header measures application time until response headers are ready; the client
also measures network and body transfer time. The first request on each thread
includes TCP/TLS setup. Single-mode completion is checked once per second, so
the observed completion can lag the database update by a polling interval plus
request time.

Server logs use `elapsed_ms` and the same request ID for HTTP and database work:

* `db.pool.open`: startup cost of establishing the initial MySQL connections.
* `db.pool.acquire`: time borrowing a pooled connection, including any wait for
  capacity or reconnect; `pool_size` and `pool_free` are snapshots before acquiring.
* `db.connection.held`: time borrowed, with MySQL `connection_id` to track reuse.
  Returning it to the pool leaves its socket open. No connection is borrowed
  during the 30-second simulated external call.
* `sql.webhook.insert`, `sql.transaction.select`, `sql.worker.complete` (one per batch): SQL timings.
* `worker claimed`: batch claim duration and row age at selection. Row age uses
  second-resolution `created_at`, so it is approximate and includes earlier
  attempts on retries. `LOG_LEVEL=DEBUG` adds claim connection and empty-poll timings.
* `worker.external_call`: actual simulated-call duration and configured delay.
  `processing_ms` includes that call and the final status update after claiming.

The assessment requires a 30-second simulated call. A 32-second gap between
`created_at` and `processed_at` includes that delay plus queue/SQL overhead and
timestamp rounding. Holding a connection for 45 seconds would consume a pool
slot throughout the wait. Keep connections pooled between operations instead;
the default idle recycle interval is 300 seconds.

## Technical choices

* **FastAPI** for the API: async, small, automatic validation and docs.
* **MySQL** (Aiven free tier) as the only piece of infrastructure. It stores
  the transactions and doubles as the job queue.
* **Raw SQL with aiomysql, no ORM.** One table and five queries. The two that
  matter, the idempotent insert and the `SKIP LOCKED` claim, are MySQL
  features I wanted visible in the code rather than hidden behind a library.
* **Worker in the same process** as the API. One service to deploy, and the
  free hosting tier has no separate worker. The code also runs standalone
  (`python -m app.worker`) when the two need to scale independently.
* **No Redis, Celery or SQS.** They would add a second source of truth and a
  second service for a problem the database already solves at this scale.
* **All timestamps in UTC.** MySQL `DATETIME` has no timezone, so every
  connection sets `time_zone = '+00:00'` and the API renders times with `Z`.
* **Pinned dependencies** and a **Dockerfile**, so the deployed service runs
  exactly what was tested.

## Deployment

For an Ubuntu EC2 server with Docker and Nginx, follow the
[AWS deployment guide](docs/AWS-DEPLOYMENT.md). It includes every command,
security group settings, environment configuration, HTTPS, and update steps.

The service runs as a Docker container. `render.yaml` describes it for
Render; the same image runs on AWS App Runner or any container host.

Render, from this repo:

1. New, Blueprint, pick the repo. Render reads `render.yaml`.
2. Enter the `DB_*` values when asked. Use the Aiven service's host, port,
   user, password, and database.
3. Deploy. The health check path is `/`.

The database lives in Aiven's Singapore region and the service is deployed to
Render's Singapore region, so each query is a short round trip. A free uptime
monitor pings `/` every 5 minutes so the free instance never sleeps; even if it
did, pending rows wait in MySQL and are processed when it wakes.

## What I would do next

* Verify the processor's webhook signature (HMAC) before accepting a payload.
* Move the 30 second wait to an SQS queue with `DelaySeconds`, with a
  dead-letter queue, if volume outgrew a polling worker.
* Alembic migrations instead of `CREATE TABLE IF NOT EXISTS` once the schema
  starts changing.
* A `/stats` endpoint with counts by status for monitoring.
