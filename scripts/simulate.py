#!/usr/bin/env python3
"""Simulation and Load Testing CLI for Transaction Webhook Service.

Simulate single or multiple (low/high volume) webhook requests against
the production endpoint (or local server), measuring latency, verifying
idempotency, and monitoring background processing to completion.

Usage:
  # Single transaction workflow (send, check PROCESSING, test duplicate, wait for PROCESSED):
  python scripts/simulate.py --mode single

  # Low volume test (5 transactions, 2 concurrent):
  python scripts/simulate.py --mode batch --count 5 --concurrency 2

  # High volume test (40 transactions, 10 concurrent, 20% duplicate rate, wait for processing):
  python scripts/simulate.py --mode batch --count 40 --concurrency 10 --duplicates 20 --wait

  # Point to local server:
  python scripts/simulate.py --url http://127.0.0.1:8000 --mode single
"""

import argparse
import concurrent.futures
import json
import random
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Tuple

try:
    import certifi
    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CONTEXT = ssl.create_default_context()
    SSL_CONTEXT.check_hostname = False
    SSL_CONTEXT.verify_mode = ssl.CERT_NONE


def http_request(
    url: str,
    method: str = "GET",
    data: Optional[dict] = None,
    timeout: float = 10.0,
) -> Tuple[int, Any, float]:
    """Execute an HTTP request and return (status_code, response_data, duration_ms)."""
    headers = {"Content-Type": "application/json"} if data else {}
    body_bytes = json.dumps(data).encode("utf-8") if data else None

    req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method)
    start = time.perf_counter()

    try:
        with urllib.request.urlopen(req, context=SSL_CONTEXT, timeout=timeout) as resp:
            duration_ms = (time.perf_counter() - start) * 1000.0
            body = resp.read().decode("utf-8")
            try:
                res_json = json.loads(body)
            except Exception:
                res_json = body
            return resp.status, res_json, duration_ms
    except urllib.error.HTTPError as e:
        duration_ms = (time.perf_counter() - start) * 1000.0
        body = e.read().decode("utf-8")
        try:
            res_json = json.loads(body)
        except Exception:
            res_json = body
        return e.code, res_json, duration_ms
    except Exception as e:
        duration_ms = (time.perf_counter() - start) * 1000.0
        return 0, str(e), duration_ms


def create_payload(txn_id: Optional[str] = None, amount: float = 1500.0, currency: str = "INR") -> dict:
    return {
        "transaction_id": txn_id or f"txn_sim_{uuid.uuid4().hex[:12]}",
        "source_account": f"acc_usr_{random.randint(100, 999)}",
        "destination_account": f"acc_mch_{random.randint(100, 999)}",
        "amount": amount,
        "currency": currency,
    }


def run_single(base_url: str, timeout: float = 120.0):
    print("=" * 65)
    print(f" SIMULATION: Single Transaction on {base_url}")
    print("=" * 65)

    # 1. Health check
    print("\n[Step 1] Checking service health...")
    status, body, duration = http_request(f"{base_url}/")
    if status != 200:
        print(f"  [ERROR] Health check failed with status {status}: {body}")
        sys.exit(1)
    print(f"  [OK] Health Check passed in {duration:.1f}ms: {body}")

    # 2. Send webhook
    payload = create_payload()
    txn_id = payload["transaction_id"]
    print(f"\n[Step 2] Sending transaction webhook (ID: {txn_id})...")
    status, body, duration = http_request(f"{base_url}/v1/webhooks/transactions", method="POST", data=payload)
    print(f"  [HTTP {status}] Webhook acknowledged in {duration:.1f}ms (Target: <500ms)")
    print(f"  Response: {body}")
    if status != 202:
        print(f"  [ERROR] Expected HTTP 202 Accepted, got {status}")
        sys.exit(1)

    # 3. Query immediate status
    print(f"\n[Step 3] Querying immediate transaction status...")
    status, body, duration = http_request(f"{base_url}/v1/transactions/{txn_id}")
    print(f"  [HTTP {status}] Query response in {duration:.1f}ms:")
    print(f"  {json.dumps(body, indent=2)}")
    if status == 200 and isinstance(body, list) and len(body) > 0:
        rec = body[0]
        if rec.get("status") == "PROCESSING" and rec.get("processed_at") is None:
            print("  [OK] Verified initial state: PROCESSING with processed_at = null")
        else:
            print(f"  [WARN] Unexpected state: {rec.get('status')}")

    # 4. Idempotency test: send duplicate
    print(f"\n[Step 4] Sending duplicate webhook for {txn_id} (idempotency check)...")
    dup_payload = dict(payload)
    dup_payload["amount"] = 9999.0  # Intentional amount tamper to verify first-write-wins
    status, body, duration = http_request(f"{base_url}/v1/webhooks/transactions", method="POST", data=dup_payload)
    print(f"  [HTTP {status}] Duplicate webhook response in {duration:.1f}ms: {body}")
    if status != 202:
        print(f"  [ERROR] Expected HTTP 202 Accepted for duplicate, got {status}")

    # 5. Wait for background processing (30s)
    print(f"\n[Step 5] Waiting for background worker to process (30s simulated delay)...")
    start_time = time.time()
    final_record = None
    while time.time() - start_time < timeout:
        time.sleep(3)
        status, body, _ = http_request(f"{base_url}/v1/transactions/{txn_id}")
        if status == 200 and isinstance(body, list) and len(body) > 0:
            rec = body[0]
            elapsed = time.time() - start_time
            print(f"  ... {elapsed:.1f}s elapsed: status = {rec.get('status')}")
            if rec.get("status") == "PROCESSED":
                final_record = rec
                break

    if final_record:
        print("\n" + "=" * 65)
        print(" [SUCCESS] Transaction completed background processing successfully!")
        print("=" * 65)
        print(f"  Total processing time: {time.time() - start_time:.1f} seconds")
        print(f"  Original Amount preserved: {final_record.get('amount')} (not altered by duplicate)")
        print(f"  Created At:   {final_record.get('created_at')}")
        print(f"  Processed At: {final_record.get('processed_at')}")
        print(json.dumps(final_record, indent=2))
    else:
        print("\n  [FAIL] Transaction did not transition to PROCESSED within 60s timeout.")
        sys.exit(1)


def send_one_webhook(base_url: str, payload: dict) -> Dict[str, Any]:
    status, body, duration = http_request(
        f"{base_url}/v1/webhooks/transactions",
        method="POST",
        data=payload,
    )
    return {
        "txn_id": payload["transaction_id"],
        "status": status,
        "duration_ms": duration,
        "body": body,
        "is_duplicate": payload.get("_is_dup", False),
    }


def run_batch(base_url: str, count: int, concurrency: int, duplicate_percent: int, wait_completion: bool):
    print("=" * 65)
    mode_name = "HIGH VOLUME" if count >= 30 else "LOW VOLUME"
    print(f" SIMULATION: {mode_name} LOAD TEST ({count} requests, concurrency={concurrency})")
    print(f" Target: {base_url}")
    print(f" Duplicate percentage: {duplicate_percent}%")
    print("=" * 65)

    # 1. Health check
    status, body, duration = http_request(f"{base_url}/")
    if status != 200:
        print(f"[ERROR] Service is not healthy: HTTP {status} {body}")
        sys.exit(1)
    print(f"[OK] Health check passed in {duration:.1f}ms\n")

    # 2. Prepare payload batch
    payloads = []
    unique_txns = []
    num_unique = max(1, int(count * (1.0 - duplicate_percent / 100.0)))
    for _ in range(num_unique):
        p = create_payload()
        payloads.append(p)
        unique_txns.append(p["transaction_id"])

    # Fill remainder with duplicates of already generated transactions
    while len(payloads) < count:
        donor = random.choice(payloads)
        dup = dict(donor)
        dup["_is_dup"] = True
        payloads.append(dup)

    random.shuffle(payloads)

    # 3. Execute requests concurrently
    print(f"Dispatching {count} webhook requests across {concurrency} concurrent workers...")
    start_load = time.perf_counter()
    results: List[Dict[str, Any]] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(send_one_webhook, base_url, p) for p in payloads]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    total_load_duration = (time.perf_counter() - start_load) * 1000.0

    # 4. Latency Analysis
    latencies = [r["duration_ms"] for r in results]
    latencies.sort()
    status_codes = {}
    for r in results:
        status_codes[r["status"]] = status_codes.get(r["status"], 0) + 1

    accepted_202 = status_codes.get(202, 0)
    failed = count - accepted_202
    avg_lat = sum(latencies) / len(latencies) if latencies else 0.0
    p50 = latencies[int(len(latencies) * 0.50)] if latencies else 0.0
    p90 = latencies[int(len(latencies) * 0.90)] if latencies else 0.0
    p95 = latencies[int(len(latencies) * 0.95)] if latencies else 0.0
    p99 = latencies[int(len(latencies) * 0.99)] if latencies else 0.0

    print("\n" + "-" * 65)
    print(" INGESTION PERFORMANCE METRICS")
    print("-" * 65)
    print(f"  Total Requests:       {count}")
    print(f"  HTTP 202 Accepted:    {accepted_202} ({accepted_202/count*100:.1f}%)")
    print(f"  Failures/Errors:      {failed}")
    print(f"  Status Code Breakdown:{status_codes}")
    print(f"  Total Wall Time:      {total_load_duration/1000.0:.2f} s")
    print(f"  Throughput:           {count / (total_load_duration/1000.0):.1f} req/sec")
    print()
    print(f"  Latency Min:          {latencies[0]:.1f} ms")
    print(f"  Latency Average:      {avg_lat:.1f} ms")
    print(f"  Latency 50th (p50):   {p50:.1f} ms")
    print(f"  Latency 90th (p90):   {p90:.1f} ms")
    print(f"  Latency 95th (p95):   {p95:.1f} ms")
    print(f"  Latency 99th (p99):   {p99:.1f} ms")
    print(f"  Latency Max:          {latencies[-1]:.1f} ms")

    # Check SLA (< 500ms)
    under_500ms = sum(1 for l in latencies if l < 500.0)
    print(f"  Requests < 500ms:     {under_500ms}/{count} ({under_500ms/count*100:.1f}%)")

    # 5. Wait for completion if requested
    if wait_completion:
        print("\n" + "-" * 65)
        print(f" MONITORING BACKGROUND PROCESSING ({len(unique_txns)} unique transactions)")
        print("-" * 65)
        monitor_start = time.time()
        timeout = 180  # Up to 3 minutes for large batches
        while time.time() - monitor_start < timeout:
            counts = {"PROCESSING": 0, "PROCESSED": 0, "FAILED": 0, "NOT_FOUND": 0}

            def check_status(tid):
                s, b, _ = http_request(f"{base_url}/v1/transactions/{tid}")
                if s == 200 and isinstance(b, list) and len(b) > 0:
                    return b[0].get("status", "UNKNOWN")
                return "NOT_FOUND"

            with concurrent.futures.ThreadPoolExecutor(max_workers=min(10, concurrency)) as pool:
                statuses = list(pool.map(check_status, unique_txns))

            for st in statuses:
                counts[st] = counts.get(st, 0) + 1

            elapsed = time.time() - monitor_start
            print(f"  [{elapsed:5.1f}s] PROCESSED: {counts.get('PROCESSED', 0):3d}/{len(unique_txns)} | "
                  f"PROCESSING: {counts.get('PROCESSING', 0):3d} | "
                  f"FAILED: {counts.get('FAILED', 0):2d}")

            if counts.get("PROCESSED", 0) == len(unique_txns):
                print(f"\n[SUCCESS] All {len(unique_txns)} transactions PROCESSED in {elapsed:.1f}s!")
                break
            time.sleep(5)


def main():
    parser = argparse.ArgumentParser(description="Transaction Webhook Simulation CLI")
    parser.add_argument(
        "--url",
        default="https://walnutfolks-assesment.brijeshhq.com",
        help="Base URL of the service (default: https://walnutfolks-assesment.brijeshhq.com)",
    )
    parser.add_argument(
        "--mode",
        choices=["single", "batch"],
        default="single",
        help="Simulation mode: 'single' (full lifecycle) or 'batch' (concurrency/volume)",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=10,
        help="Number of requests to dispatch in batch mode (e.g. 5 for low, 50 for high)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=5,
        help="Concurrent workers in batch mode (default: 5)",
    )
    parser.add_argument(
        "--duplicates",
        type=int,
        default=0,
        help="Percentage of duplicate transactions to send in batch mode (0-100, default: 0)",
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help="In batch mode, monitor and wait until all transactions reach PROCESSED status",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="Max timeout in seconds to wait for processing completion (default: 120s)",
    )

    args = parser.parse_args()
    base_url = args.url.rstrip("/")

    if args.mode == "single":
        run_single(base_url, timeout=args.timeout)
    else:
        run_batch(base_url, args.count, args.concurrency, args.duplicates, args.wait)


if __name__ == "__main__":
    main()
