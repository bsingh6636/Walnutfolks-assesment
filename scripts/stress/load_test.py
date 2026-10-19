#!/usr/bin/env python3
"""Run bounded webhook load tests against the transaction service.

Only POST requests count toward --count/--counts. The script also makes one
health request before the run and a few status requests after each stage.
No database credentials are needed. Every request has a traceable stress ID.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import http.client
import json
import math
import random
import ssl
import statistics
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


LOCAL = threading.local()


def parse_counts(value: str) -> list[int]:
    try:
        counts = [int(part.strip()) for part in value.split(",")]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("counts must be comma-separated integers") from exc
    if not counts or any(count < 1 for count in counts):
        raise argparse.ArgumentTypeError("all counts must be positive")
    return counts


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * fraction
    low = math.floor(position)
    high = math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def parse_server_timing(header: str | None) -> float | None:
    """The API sends `Server-Timing: app;dur=12.34`: time spent inside the app, no network."""
    if not header:
        return None
    for part in header.split(","):
        name, _, rest = part.strip().partition(";")
        if name == "app" and rest.startswith("dur="):
            try:
                return round(float(rest[4:]), 2)
            except ValueError:
                return None
    return None


def parse_utc(value: str | None) -> datetime | None:
    """API timestamps look like 2024-01-15T10:30:00Z."""
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def check_status(target, timeout: float, transaction_id: str) -> dict:
    checked = exchange(target, timeout, "GET", f"/v1/transactions/{transaction_id}")
    row = checked["body"][0] if checked["status"] == 200 and isinstance(checked["body"], list) and checked["body"] else {}
    return {"transaction_id": transaction_id, "http_status": checked["status"], "state": row.get("status"),
            "created_at": row.get("created_at"), "processed_at": row.get("processed_at")}


def wait_for_processing(target, timeout: float, ids: list[str], concurrency: int,
                        wait_seconds: float, target_seconds: float) -> dict:
    """Poll the status endpoint until every id is PROCESSED/FAILED or time runs out.

    Processing time comes from the API itself: processed_at - created_at,
    both set by the database, so the clock on this machine never matters.
    """
    pending = list(ids)
    finished: dict[str, dict] = {}
    deadline = time.perf_counter() + wait_seconds
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        while pending and time.perf_counter() < deadline:
            rows = list(pool.map(lambda tid: check_status(target, timeout, tid), pending))
            pending = []
            for row in rows:
                if row["state"] in {"PROCESSED", "FAILED"}:
                    finished[row["transaction_id"]] = row
                else:
                    pending.append(row["transaction_id"])
            if pending:
                time.sleep(2)
    durations = []
    for row in finished.values():
        created, processed = parse_utc(row["created_at"]), parse_utc(row["processed_at"])
        if row["state"] == "PROCESSED" and created and processed:
            row["processing_seconds"] = (processed - created).total_seconds()
            durations.append(row["processing_seconds"])
    return {
        "transactions": len(ids),
        "processed": sum(row["state"] == "PROCESSED" for row in finished.values()),
        "failed": sum(row["state"] == "FAILED" for row in finished.values()),
        "still_pending": len(pending),
        "target_seconds": target_seconds,
        "processed_within_target": sum(seconds <= target_seconds for seconds in durations),
        "processing_seconds": {
            "min": min(durations) if durations else None,
            "p50": round(percentile(durations, 0.50), 1) if durations else None,
            "p95": round(percentile(durations, 0.95), 1) if durations else None,
            "max": max(durations) if durations else None,
        },
        "rows": sorted(finished.values(), key=lambda row: row["transaction_id"]) + [
            {"transaction_id": tid, "state": "PENDING_AT_TIMEOUT"} for tid in pending],
    }


def make_connection(target, timeout: float):
    port = target.port or (443 if target.scheme == "https" else 80)
    if target.scheme == "https":
        return http.client.HTTPSConnection(
            target.hostname, port, timeout=timeout, context=ssl.create_default_context()
        )
    return http.client.HTTPConnection(target.hostname, port, timeout=timeout)


def exchange(target, timeout: float, method: str, path: str, payload: dict | None = None) -> dict:
    connect_ms = 0.0
    connection = getattr(LOCAL, "connection", None)
    try:
        if connection is None:
            connection = make_connection(target, timeout)
            start = time.perf_counter()
            connection.connect()
            connect_ms = (time.perf_counter() - start) * 1000
            LOCAL.connection = connection

        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        start = time.perf_counter()
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        latency_ms = (time.perf_counter() - start) * 1000
        server_ms = parse_server_timing(response.getheader("Server-Timing"))
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            body = raw.decode("utf-8", errors="replace")[:300]
        return {
            "status": response.status,
            "latency_ms": round(latency_ms, 2),
            "server_ms": server_ms,
            "connect_ms": round(connect_ms, 2),
            "body": body,
            "error": None,
        }
    except Exception as exc:
        if connection is not None:
            connection.close()
        LOCAL.connection = None
        return {
            "status": 0,
            "latency_ms": None,
            "server_ms": None,
            "connect_ms": round(connect_ms, 2),
            "body": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


def send_webhook(target, timeout: float, payload: dict) -> dict:
    result = exchange(target, timeout, "POST", "/v1/webhooks/transactions", payload)
    result["transaction_id"] = payload["transaction_id"]
    result["valid_ack"] = (
        result["status"] == 202
        and isinstance(result["body"], dict)
        and result["body"].get("transaction_id") == payload["transaction_id"]
        and result["body"].get("status") == "accepted"
    )
    result.pop("body")
    return result


def make_payloads(count: int, duplicate_percent: int, run_id: str, stage: int) -> tuple[list[dict], list[str]]:
    unique_count = max(1, math.ceil(count * (100 - duplicate_percent) / 100))
    unique = []
    for index in range(unique_count):
        unique.append(
            {
                "transaction_id": f"txn_stress_{run_id}_{stage}_{index:05d}",
                "source_account": "acc_stress_test",
                "destination_account": "acc_stress_sink",
                "amount": 1,
                "currency": "INR",
            }
        )
    payloads = unique + [unique[index % unique_count] for index in range(count - unique_count)]
    random.Random(f"{run_id}-{stage}").shuffle(payloads)
    return payloads, [item["transaction_id"] for item in unique]


def summarize(results: list[dict], wall_seconds: float, threshold_ms: float) -> dict:
    latencies = [item["latency_ms"] for item in results if item["latency_ms"] is not None]
    server = [item["server_ms"] for item in results if item.get("server_ms") is not None]
    statuses = Counter(str(item["status"]) for item in results)
    accepted = sum(item["valid_ack"] for item in results)
    under_threshold = sum(
        item["valid_ack"] and item["latency_ms"] is not None and item["latency_ms"] < threshold_ms
        for item in results
    )
    return {
        "requests": len(results),
        "accepted": accepted,
        "failures": len(results) - accepted,
        "status_codes": dict(sorted(statuses.items())),
        "wall_seconds": round(wall_seconds, 2),
        "throughput_rps": round(len(results) / wall_seconds, 2) if wall_seconds else 0,
        "threshold_ms": threshold_ms,
        "accepted_under_threshold": under_threshold,
        "latency_ms": {
            "min": round(min(latencies), 2) if latencies else None,
            "mean": round(statistics.mean(latencies), 2) if latencies else None,
            "p50": round(percentile(latencies, 0.50), 2) if latencies else None,
            "p95": round(percentile(latencies, 0.95), 2) if latencies else None,
            "p99": round(percentile(latencies, 0.99), 2) if latencies else None,
            "max": round(max(latencies), 2) if latencies else None,
        },
        # Time inside the app only (Server-Timing header); latency minus this is network.
        "server_ms": {
            "p50": round(percentile(server, 0.50), 2) if server else None,
            "p95": round(percentile(server, 0.95), 2) if server else None,
            "max": round(max(server), 2) if server else None,
        },
        "error_examples": [item["error"] or f"HTTP {item['status']} / invalid acknowledgment"
                           for item in results if not item["valid_ack"]][:5],
    }


def checkpoint(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="https://walnutfolks-assesment.brijeshhq.com")
    stage_args = parser.add_mutually_exclusive_group()
    stage_args.add_argument("--count", type=int, help="One stage with this many POST requests")
    stage_args.add_argument("--counts", type=parse_counts, default=[50, 100, 200, 500, 1000],
                            help="Comma-separated ramp; default: 50,100,200,500,1000")
    parser.add_argument("--concurrency", type=int, default=20, help="Maximum simultaneous POSTs (default: 20)")
    parser.add_argument("--duplicates", type=int, default=0,
                        help="Approximate duplicate percentage in each stage (default: 0)")
    parser.add_argument("--timeout", type=float, default=20.0, help="Per-request timeout in seconds")
    parser.add_argument("--threshold-ms", type=float, default=500.0)
    parser.add_argument("--stop-error-percent", type=float, default=2.0,
                        help="Stop the ramp if a stage exceeds this failure percentage")
    parser.add_argument("--stop-p95-ms", type=float, default=10000.0,
                        help="Stop the ramp if a stage exceeds this p95 latency")
    parser.add_argument("--verify-sample", type=int, default=3,
                        help="GET this many distinct IDs after each stage (default: 3)")
    parser.add_argument("--wait-processing", type=float, default=0.0, metavar="SECONDS",
                        help="After each stage, poll every unique transaction for up to this many seconds "
                             "and report how long processing took (0 = skip, the default)")
    parser.add_argument("--processing-target-seconds", type=float, default=35.0,
                        help="Processing time (processed_at - created_at) counted as on time (default: 35)")
    parser.add_argument("--output", type=Path, help="JSON report path; defaults to scripts/stress/results/")
    args = parser.parse_args()

    counts = [args.count] if args.count is not None else args.counts
    target = urlsplit(args.url.rstrip("/"))
    if target.scheme not in {"http", "https"} or not target.hostname or target.path not in {"", "/"}:
        parser.error("--url must be an http(s) origin without a path")
    if any(count < 1 for count in counts) or args.concurrency < 1 or args.timeout <= 0:
        parser.error("counts, concurrency, and timeout must be positive")
    if not 0 <= args.duplicates < 100 or args.verify_sample < 0:
        parser.error("--duplicates must be 0..99 and --verify-sample must be >= 0")
    if not 0 <= args.stop_error_percent <= 100 or args.stop_p95_ms <= 0:
        parser.error("invalid stop threshold")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + uuid.uuid4().hex[:6]
    output = args.output or Path(__file__).resolve().parent / "results" / f"stress-{run_id}.json"
    report = {
        "run_id": run_id,
        "url": args.url.rstrip("/"),
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "concurrency": args.concurrency,
        "duplicate_percent": args.duplicates,
        "stages": [],
    }

    health = exchange(target, args.timeout, "GET", "/")
    if health["status"] != 200 or not isinstance(health["body"], dict) or health["body"].get("status") != "HEALTHY":
        print(f"Health check failed: HTTP {health['status']} {health['error'] or health['body']}")
        return 2
    print(f"Run {run_id} | health OK | target {args.url} | output {output}", flush=True)

    for stage_number, count in enumerate(counts, 1):
        payloads, unique_ids = make_payloads(count, args.duplicates, run_id, stage_number)
        print(f"Stage {stage_number}: {count} POSTs, {len(unique_ids)} unique IDs, "
              f"concurrency {min(args.concurrency, count)}", flush=True)
        start = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(args.concurrency, count)) as pool:
            futures = [pool.submit(send_webhook, target, args.timeout, payload) for payload in payloads]
            results = [future.result() for future in concurrent.futures.as_completed(futures)]
        wall_seconds = time.perf_counter() - start
        summary = summarize(results, wall_seconds, args.threshold_ms)
        samples = [check_status(target, args.timeout, transaction_id)
                   for transaction_id in unique_ids[: args.verify_sample]]
        stage = {"stage": stage_number, "count": count, "unique_transactions": len(unique_ids),
                 "summary": summary, "samples": samples, "requests": results}
        report["stages"].append(stage)
        checkpoint(output, report)
        print(f"  accepted {summary['accepted']}/{count} | p50 {summary['latency_ms']['p50']} ms | "
              f"p95 {summary['latency_ms']['p95']} ms | p99 {summary['latency_ms']['p99']} ms | "
              f"< {args.threshold_ms:g} ms: {summary['accepted_under_threshold']}/{count} | "
              f"server p50 {summary['server_ms']['p50']} ms p95 {summary['server_ms']['p95']} ms | "
              f"throughput {summary['throughput_rps']} req/s", flush=True)
        if args.wait_processing > 0:
            print(f"  waiting up to {args.wait_processing:g}s for {len(unique_ids)} transactions to process...",
                  flush=True)
            processing = wait_for_processing(target, args.timeout, unique_ids, min(args.concurrency, count),
                                             args.wait_processing, args.processing_target_seconds)
            stage["processing"] = processing
            checkpoint(output, report)
            seconds = processing["processing_seconds"]
            print(f"  processed {processing['processed']}/{processing['transactions']} | "
                  f"failed {processing['failed']} | pending {processing['still_pending']} | "
                  f"processing time p50 {seconds['p50']}s p95 {seconds['p95']}s max {seconds['max']}s | "
                  f"<= {args.processing_target_seconds:g}s: {processing['processed_within_target']}"
                  f"/{processing['transactions']}", flush=True)
        if summary["error_examples"]:
            print(f"  errors: {summary['error_examples']}", flush=True)
        if summary["failures"] / count * 100 > args.stop_error_percent or (
            summary["latency_ms"]["p95"] is not None
            and summary["latency_ms"]["p95"] > args.stop_p95_ms
        ):
            report["stopped_early"] = "failure percentage or p95 exceeded stop threshold"
            print(f"Stopping ramp: {report['stopped_early']}", flush=True)
            break

    report["finished_utc"] = datetime.now(timezone.utc).isoformat()
    checkpoint(output, report)
    print(f"Report: {output}", flush=True)
    return 1 if any(stage["summary"]["failures"] for stage in report["stages"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
