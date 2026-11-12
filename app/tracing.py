"""Correlation ids for request logs and the mail pipeline."""
from __future__ import annotations

import contextvars
import logging
import re
import uuid
from typing import Optional

_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")
_entity_id: contextvars.ContextVar[str] = contextvars.ContextVar("entity_id", default="")

_SAFE_ID = re.compile(r"^[\x21-\x7e]{1,96}$")
_LOG_FORMAT = "%(asctime)s %(levelname)s [trace=%(trace_id)s entity=%(entity_id)s] %(name)s: %(message)s"

_factory_installed = False


def new_trace_id() -> str:
    """Return a short id for one request or one polled message."""
    return uuid.uuid4().hex[:12]


def set_trace_id(value: str) -> contextvars.Token:
    return _trace_id.set(value)


def reset_trace_id(token: contextvars.Token) -> None:
    _trace_id.reset(token)


def get_trace_id() -> str:
    return _trace_id.get() or "-"


def set_entity_id(value: str) -> contextvars.Token:
    return _entity_id.set(value)


def reset_entity_id(token: contextvars.Token) -> None:
    _entity_id.reset(token)


def get_entity_id() -> str:
    return _entity_id.get() or "-"


def clean_id(value: Optional[str]) -> str:
    """Reject empty, oversized or non-printable ids before they reach a log."""
    if value and _SAFE_ID.match(value):
        return value
    return ""


def configure_logging(level: int = logging.INFO) -> None:
    """Add the ids to every LogRecord, including handlers owned by uvicorn."""
    global _factory_installed
    if not _factory_installed:
        base_factory = logging.getLogRecordFactory()

        def factory(*args, **kwargs):
            record = base_factory(*args, **kwargs)
            record.trace_id = get_trace_id()
            record.entity_id = get_entity_id()
            return record

        logging.setLogRecordFactory(factory)
        _factory_installed = True
    logging.basicConfig(level=level, format=_LOG_FORMAT)
