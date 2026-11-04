"""Share one SQLite writer transaction across the registry and governance stores."""

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from contextvars import ContextVar
from pathlib import Path

_active: ContextVar[tuple[str, sqlite3.Connection] | None] = ContextVar(
    "mlops_sqlite_transaction", default=None
)


@contextmanager
def connect(path: str) -> Iterator[sqlite3.Connection]:
    active = _active.get()
    if active is not None and active[0] == path:
        yield active[1]
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=30, isolation_level=None)) as connection:
        connection.row_factory = sqlite3.Row
        yield connection


@contextmanager
def transaction(path: str) -> Iterator[sqlite3.Connection]:
    active = _active.get()
    if active is not None:
        if active[0] != path:
            raise RuntimeError("A transaction cannot span separate SQLite databases.")
        yield active[1]
        return
    with connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        token = _active.set((path, connection))
        try:
            with connection:
                yield connection
        finally:
            _active.reset(token)
