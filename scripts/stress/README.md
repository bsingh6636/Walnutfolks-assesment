# Webhook stress test

`load_test.py` sends a configurable number of concurrent webhook POSTs to the
live service or a local instance. It measures HTTP acknowledgment latency and
throughput, and reads the `Server-Timing` header so time spent inside the app
is reported separately from network time. With `--wait-processing SECONDS`
it also polls every unique transaction after each stage and reports how long
processing took (`processed_at - created_at`, both set by the database). The
worker processes every due row as one batch, so a stage of 1,000 unique
transactions should finish about 31 seconds after the POSTs.

From the repository root:

```powershell
# One stage: exactly 50 webhook POSTs, all unique.
python scripts/stress/load_test.py --count 50 --concurrency 10

# Increase the POST count and concurrent clients.
python scripts/stress/load_test.py --count 100 --concurrency 15
python scripts/stress/load_test.py --count 200 --concurrency 20

# Higher request volume with fewer new background jobs.
# 90% of POSTs reuse IDs from the same stage.
python scripts/stress/load_test.py --count 500 --concurrency 25 --duplicates 90
python scripts/stress/load_test.py --count 1000 --concurrency 30 --duplicates 90

# Or run the full default ramp with all unique IDs. Use only when the longer
# worker backlog is acceptable.
python scripts/stress/load_test.py

# Also wait for processing and report processing time per transaction.
python scripts/stress/load_test.py --count 1000 --concurrency 30 --wait-processing 120

# Local instance and custom report location.
python scripts/stress/load_test.py --url http://127.0.0.1:8000 --count 50 --output local-stress.json
```

`--count` runs one stage. `--counts` accepts a comma-separated ramp, such as
`--counts 50,100,200,500,1000`. `--concurrency` caps the number of simultaneous
POSTs. `--duplicates 90` means approximately 10% unique transaction IDs in
that stage; the total number of POSTs remains exactly `--count`. The script
stops a ramp after a stage with more than 2% failed acknowledgments or p95
latency over 10 seconds. These limits can be changed with
`--stop-error-percent` and `--stop-p95-ms`. `--wait-processing 120` waits up to
120 seconds per stage for every transaction to reach `PROCESSED`;
`--processing-target-seconds` (default 35) is the limit counted as on time.

The script makes one health GET before sending POSTs and up to three status
GETs after each stage. Those GETs are outside the reported request count.
It measures POST latency on an established HTTP connection, excluding TLS
connection setup; wall time and throughput include connection setup. `202`
with the matching acknowledgment body counts as success. Latency includes
the network round trip, so it does not isolate server processing time.

Each run writes a checkpointed JSON report in `scripts/stress/results/`.
The report includes every request's status, latency, and transaction ID. The
IDs begin with `txn_stress_` so test data is easy to identify later. The
script does not delete transactions from the database.
