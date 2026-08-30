"""Structured JSON logging (P1.6).

One rule shapes this module: **log records must never carry raw user query text.**
The PII gate (ARCH §7.1) rejects before logging, but defence in depth matters
here because PS §5.2 forbids *processing* PII, and a log line is processing.
Call sites pass a decision label and metadata, not the question.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per line — greppable in GitHub Actions logs."""

    # NOTE: these go to STDERR (see `configure_logging`). Two JSON streams on
    # one pipe is not a format, it is a bug.

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Anything passed via extra={} rides along as a structured field.
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger. Idempotent.

    **Logs go to stderr, not stdout.** `python -m mf_faq.ingest --json` writes a
    report to stdout for a machine to parse, and a single WARNING interleaved
    into it makes the whole stream unparseable — which is exactly what happened
    the first time a fact was rejected during a `--json` run (P2.9). Actions
    captures both streams, so nothing is lost by the split.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # These are chatty at INFO and drown the ingestion run log.
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
