import itertools
import random
import sqlite3
import threading
from contextlib import closing
from dataclasses import replace

import pytest

from mlops_cp.audit import GENESIS_HASH, AuditLog, verify_chain


def _filled(log: AuditLog, count: int = 5) -> AuditLog:
    for index in range(count):
        log.append("event", f"model:{index % 2}", {"index": index, "values": [index, 0.5]})
    return log


@pytest.fixture(params=["memory", "sqlite"])
def log(request, tmp_path) -> AuditLog:
    return AuditLog(None if request.param == "memory" else str(tmp_path / "audit.db"))


def test_chain_links_every_event_to_its_predecessor(log) -> None:
    _filled(log)
    events = log.events()
    assert [e.seq for e in events] == [1, 2, 3, 4, 5]
    assert events[0].prev_hash == GENESIS_HASH
    assert all(b.prev_hash == a.hash for a, b in itertools.pairwise(events))
    result = log.verify()
    assert result.valid and result.length == 5 and result.head_hash == events[-1].hash


def test_subject_filter_returns_only_matching_events(log) -> None:
    _filled(log)
    assert [e.payload["index"] for e in log.events("model:1")] == [1, 3]


def test_returned_events_cannot_mutate_the_in_memory_log() -> None:
    log = _filled(AuditLog())
    log.events()[0].payload["index"] = 999
    assert log.verify().valid


def test_property_any_single_field_tamper_is_detected() -> None:
    rng = random.Random(20261103)
    events = _filled(AuditLog(), 12).events()
    for _ in range(300):
        position = rng.randrange(len(events))
        field = rng.choice(["event_type", "subject", "payload", "created_at", "prev_hash",
                            "hash", "seq"])
        original = events[position]
        tampered_value = {
            "event_type": original.event_type + "x",
            "subject": original.subject + "x",
            "payload": {**original.payload, "index": rng.randrange(1000, 2000)},
            "created_at": "1970-01-01T00:00:00+00:00",
            "prev_hash": "f" * 64,
            "hash": "e" * 64,
            "seq": original.seq + rng.randrange(1, 5),
        }[field]
        mutated = list(events)
        mutated[position] = replace(original, **{field: tampered_value})
        result = verify_chain(mutated)
        assert not result.valid, (position, field)
        assert result.first_invalid_seq is not None


def test_deleting_or_reordering_events_is_detected() -> None:
    events = _filled(AuditLog(), 6).events()
    assert not verify_chain(events[:2] + events[3:]).valid
    swapped = list(events)
    swapped[1], swapped[2] = swapped[2], swapped[1]
    assert not verify_chain(swapped).valid


def test_tail_truncation_needs_an_external_anchor() -> None:
    events = _filled(AuditLog(), 4).events()
    head = events[-1].hash
    truncated = events[:-1]
    # Documented limitation: the log alone cannot see a dropped tail ...
    assert verify_chain(truncated).valid
    # ... but an anchored head hash kept elsewhere does.
    anchored = verify_chain(truncated, expected_head=head)
    assert not anchored.valid and "anchor" in anchored.reason


def test_sqlite_rejects_update_and_delete(tmp_path) -> None:
    path = str(tmp_path / "audit.db")
    _filled(AuditLog(path), 3)
    with closing(sqlite3.connect(path)) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE audit_events SET subject = 'x' WHERE seq = 2")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM audit_events WHERE seq = 2")


def test_sqlite_tampering_behind_the_triggers_is_detected(tmp_path) -> None:
    path = str(tmp_path / "audit.db")
    log = _filled(AuditLog(path), 4)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TRIGGER audit_events_no_update")
        connection.execute(
            "UPDATE audit_events SET payload_json = '{\"index\":42}' WHERE seq = 3"
        )
    result = AuditLog(path).verify()
    assert not result.valid
    assert result.first_invalid_seq == 3
    assert result.reason == "event content does not match its hash"
    assert log.verify().first_invalid_seq == 3


def test_concurrent_writers_never_fork_the_chain(tmp_path) -> None:
    path = str(tmp_path / "audit.db")
    AuditLog(path)  # create schema once
    errors: list[BaseException] = []

    def worker(worker_id: int) -> None:
        local = AuditLog(path)  # separate connection per append, like separate processes
        try:
            for index in range(10):
                local.append("event", f"worker:{worker_id}", {"index": index})
        except (sqlite3.Error, ValueError) as error:  # pragma: no cover
            errors.append(error)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    result = AuditLog(path).verify()
    assert result.valid and result.length == 60


def test_memory_path_is_rejected_because_each_connection_is_a_new_database() -> None:
    with pytest.raises(ValueError):
        AuditLog(":memory:")


def test_non_finite_payload_values_are_rejected() -> None:
    with pytest.raises(ValueError):
        AuditLog().append("event", "s", {"value": float("nan")})
