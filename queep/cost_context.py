"""Request-local attribution; never mutate the shared AI client's context."""
from contextlib import contextmanager
from contextvars import ContextVar

current = ContextVar("ai_cost_context", default=None)


@contextmanager
def attribution(**metadata):
    token = current.set(metadata)
    try:
        yield
    finally:
        current.reset(token)
