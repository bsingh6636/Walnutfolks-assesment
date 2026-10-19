"""Monotonic timings shared by the API, database pool, and worker."""

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter

log = logging.getLogger(__name__)
request_id: ContextVar[str] = ContextVar("request_id", default="-")


@contextmanager
def measure(operation: str, *, level: int = logging.INFO, **fields):
    started = perf_counter()
    outcome = "ok"
    try:
        yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        log.log(
            level,
            "timing operation=%s elapsed_ms=%.2f outcome=%s request_id=%s %s",
            operation, (perf_counter() - started) * 1000, outcome,
            request_id.get(), " ".join(f"{key}={value}" for key, value in fields.items()),
        )
