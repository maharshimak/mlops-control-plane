"""Append-only, hash-chained governance audit log.

Every event stores the SHA-256 of its predecessor (``prev_hash``) and its own
``hash`` computed over a canonical JSON encoding of all of its fields. Editing,
reordering, inserting or deleting an event in the middle of the chain is
detected by :meth:`AuditLog.verify`. Truncating events from the *end* of the
chain cannot be detected from the log alone; callers that need that guarantee
must keep the head hash somewhere else and pass it as ``expected_head``.

This is tamper evidence, not tamper prevention: there are no signatures, and an
actor able to rewrite the whole database can recompute every hash. With SQLite
storage, ``UPDATE`` and ``DELETE`` on the table are additionally rejected by
triggers so accidental in-place edits fail loudly.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Mapping
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from mlops_cp.models import utc_now

GENESIS_HASH = "0" * 64


def canonical_json(value: Any) -> str:
    """Canonical JSON used for every hash in this package (sorted keys, compact)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AuditEvent:
    seq: int
    event_type: str
    subject: str
    payload: dict[str, Any]
    created_at: str
    prev_hash: str
    hash: str

    def body(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "event_type": self.event_type,
            "subject": self.subject,
            "payload": self.payload,
            "created_at": self.created_at,
            "prev_hash": self.prev_hash,
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ChainVerification:
    valid: bool
    length: int
    head_hash: str
    first_invalid_seq: int | None
    reason: str | None


def _event_hash(body: Mapping[str, Any]) -> str:
    return sha256_json(dict(body))


class AuditLog:
    """Hash-chained event log, in memory (``database_path=None``) or in SQLite."""

    def __init__(self, database_path: str | None = None) -> None:
        if database_path == ":memory:":
            # Each sqlite3 connection to ":memory:" is a separate empty database.
            raise ValueError("use database_path=None for an in-memory audit log")
        self.database_path = database_path
        self._events: list[AuditEvent] = []
        self._lock = threading.Lock()
        if database_path is not None:
            self._initialize_storage()

    def _connect(self) -> sqlite3.Connection:
        assert self.database_path is not None
        Path(self.database_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, isolation_level=None, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize_storage(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS audit_events (
                    seq INTEGER PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    prev_hash TEXT NOT NULL,
                    hash TEXT NOT NULL UNIQUE
                );
                CREATE INDEX IF NOT EXISTS idx_audit_subject ON audit_events(subject, seq);
                CREATE TRIGGER IF NOT EXISTS audit_events_no_update
                BEFORE UPDATE ON audit_events
                BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
                BEFORE DELETE ON audit_events
                BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
                """
            )

    def append(
        self,
        event_type: str,
        subject: str,
        payload: Mapping[str, Any] | None = None,
    ) -> AuditEvent:
        if not event_type or not subject:
            raise ValueError("event_type and subject are required")
        clean_payload = json.loads(canonical_json(dict(payload or {})))
        if self.database_path is None:
            with self._lock:
                previous = self._events[-1] if self._events else None
                event = self._build(previous, event_type, subject, clean_payload)
                self._events.append(event)
                return event

        with closing(self._connect()) as connection:
            # BEGIN IMMEDIATE takes the write lock before reading the head, so two
            # writers can never chain onto the same predecessor.
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM audit_events ORDER BY seq DESC LIMIT 1"
                ).fetchone()
                previous = self._row_to_event(row) if row is not None else None
                event = self._build(previous, event_type, subject, clean_payload)
                connection.execute(
                    """
                    INSERT INTO audit_events(
                        seq, event_type, subject, payload_json, created_at, prev_hash, hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.seq,
                        event.event_type,
                        event.subject,
                        canonical_json(event.payload),
                        event.created_at,
                        event.prev_hash,
                        event.hash,
                    ),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return event

    @staticmethod
    def _build(
        previous: AuditEvent | None,
        event_type: str,
        subject: str,
        payload: dict[str, Any],
    ) -> AuditEvent:
        body = {
            "seq": 1 if previous is None else previous.seq + 1,
            "event_type": event_type,
            "subject": subject,
            "payload": payload,
            "created_at": utc_now(),
            "prev_hash": GENESIS_HASH if previous is None else previous.hash,
        }
        return AuditEvent(**body, hash=_event_hash(body))

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> AuditEvent:
        return AuditEvent(
            seq=row["seq"],
            event_type=row["event_type"],
            subject=row["subject"],
            payload=json.loads(row["payload_json"]),
            created_at=row["created_at"],
            prev_hash=row["prev_hash"],
            hash=row["hash"],
        )

    def events(self, subject: str | None = None) -> list[AuditEvent]:
        if self.database_path is None:
            with self._lock:
                items = list(self._events)
            # Hand out copies so callers cannot mutate the stored payloads.
            return [
                AuditEvent(**{**e.to_dict(), "payload": json.loads(canonical_json(e.payload))})
                for e in items
                if subject is None or e.subject == subject
            ]
        query = "SELECT * FROM audit_events"
        params: tuple[str, ...] = ()
        if subject is not None:
            query += " WHERE subject = ?"
            params = (subject,)
        with closing(self._connect()) as connection:
            rows = connection.execute(query + " ORDER BY seq ASC", params).fetchall()
        return [self._row_to_event(row) for row in rows]

    def verify(self, expected_head: str | None = None) -> ChainVerification:
        return verify_chain(self.events(), expected_head=expected_head)


def verify_chain(
    events: list[AuditEvent],
    *,
    expected_head: str | None = None,
) -> ChainVerification:
    """Recompute every hash and link; report the first event that does not match."""
    previous_hash = GENESIS_HASH
    for index, event in enumerate(events, start=1):
        if event.seq != index:
            return ChainVerification(False, len(events), previous_hash, event.seq,
                                     f"sequence gap: expected {index}, found {event.seq}")
        if event.prev_hash != previous_hash:
            return ChainVerification(False, len(events), previous_hash, event.seq,
                                     "prev_hash does not match the preceding event")
        if _event_hash(event.body()) != event.hash:
            return ChainVerification(False, len(events), previous_hash, event.seq,
                                     "event content does not match its hash")
        previous_hash = event.hash
    if expected_head is not None and expected_head != previous_hash:
        return ChainVerification(False, len(events), previous_hash, None,
                                 "head hash does not match the expected anchor")
    return ChainVerification(True, len(events), previous_hash, None, None)
