"""Structured logging + tracing spans for agent execution.

Kept as a thin wrapper (rather than importing OpenTelemetry directly all over the codebase)
so the backend (stdout, Cloud Logging, OTLP collector) can be swapped without touching agents.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from time import perf_counter

from app.config import get_settings

settings = get_settings()

logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


@contextmanager
def span(name: str, **attributes) -> Iterator[dict]:
    """Traces one unit of work. Yields a mutable dict the caller can attach
    result attributes to (e.g. token counts) before the span closes.
    """
    logger = get_logger("quipu.trace")
    start = perf_counter()
    record: dict = {"name": name, **attributes}
    logger.info("span.start name=%s attrs=%s", name, attributes)
    try:
        yield record
    except Exception:
        record["status"] = "error"
        raise
    else:
        record.setdefault("status", "ok")
    finally:
        record["duration_ms"] = (perf_counter() - start) * 1000
        logger.info("span.end name=%s duration_ms=%.2f status=%s", name, record["duration_ms"], record.get("status"))
