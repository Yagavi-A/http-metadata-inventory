"""Per-request context propagated through ``contextvars``.

A ``X-Request-ID`` value is stored here by the request-ID middleware so
that every log record emitted while handling the request — including the
ones produced by the background worker task created within the request —
can be tagged with the same identifier.
"""

from __future__ import annotations

import contextvars

NO_REQUEST_ID = "-"

_request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id",
    default=NO_REQUEST_ID,
)


def get_request_id() -> str:
    """Return the request id bound to the current context (or ``"-"``)."""
    return _request_id_var.get()


def set_request_id(request_id: str) -> contextvars.Token[str]:
    """Bind *request_id* to the current context and return a reset token."""
    return _request_id_var.set(request_id)


def reset_request_id(token: contextvars.Token[str]) -> None:
    """Restore the request id captured by :func:`set_request_id`."""
    _request_id_var.reset(token)
