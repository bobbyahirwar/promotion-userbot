from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator


_account_id: ContextVar[int | None] = ContextVar("account_id", default=None)


def current_account_id() -> int | None:
    return _account_id.get()


@contextmanager
def account_context(account_id: int) -> Iterator[None]:
    token = _account_id.set(int(account_id))
    try:
        yield
    finally:
        _account_id.reset(token)
