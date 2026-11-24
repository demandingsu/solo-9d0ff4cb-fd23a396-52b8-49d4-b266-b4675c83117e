"""Randomized and concurrent consistency checks for the partition model."""

from __future__ import annotations

import random
import threading
import time

import pytest

from inspection.csvio import ParsedRecord
from inspection.db import connect, init_db
from inspection.service import (
    ConflictError,
    SplitError,
    day_overview,
    download_csv,
    split_day,
    upload_records,
)

ROAD = "G7"
DAY = "2026-01-02"


def _tiles(overview) -> None:
    """Active partitions must tile [0, +inf) with no gap or overlap."""

    active = sorted(
        (p for p in overview.partitions if p.status == "active"),
        key=lambda p: p.lo,
    )
    if not active:
        return
    assert active[0].lo == 0
    assert active[-1].hi is None
    for left, right in zip(active, active[1:]):
        assert left.hi == right.lo


def _check(db_path, expected: dict[str, int]) -> list[tuple[int, int | None]]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT record_no, mileage, p.lo, p.hi FROM records r "
            "JOIN partitions p ON p.id = r.partition_id WHERE p.status='active'"
        ).fetchall()
        _tiles(day_overview(conn, ROAD, DAY))
        csv_lines = download_csv(conn, ROAD, DAY, 0, None).decode(
            "utf-8-sig"
        ).splitlines()
    numbered = [(row["record_no"], row["mileage"]) for row in rows]
    assert len(numbered) == len(set(n[0] for n in numbered))
    assert {no for no, _ in numbered} == set(expected)
    for row in rows:
        assert row["lo"] <= row["mileage"] and (
            row["hi"] is None or row["mileage"] < row["hi"]
        )
    assert len(csv_lines) - 1 == len(rows)
    return [(row["lo"], row["hi"]) for row in rows]


def test_random_uploads_and_splits(tmp_path):
    db_path = str(tmp_path / "fuzz.db")
    init_db(db_path)
    random.seed(7)
    written: dict[str, int] = {}

    for round_no in range(30):
        batch = [
            ParsedRecord(
                f"N{random.randint(0, 200)}",
                random.randint(0, 5000),
                random.choice(["L1", "L2", "L3"]),
            )
            for _ in range(random.randint(1, 40))
        ]
        with connect(db_path) as conn:
            try:
                upload_records(conn, ROAD, DAY, batch)
            except ConflictError:
                continue
        for record in batch:
            previous = written.get(record.record_no)
            if previous is not None:
                assert previous == record.mileage
            written[record.record_no] = record.mileage

        with connect(db_path) as conn:
            overview = day_overview(conn, ROAD, DAY)
            _tiles(overview)
            active = [p for p in overview.partitions if p.status == "active"]

        if round_no in (5, 10, 15, 20, 25) and active:
            part = random.choice(active)
            if part.hi is None:
                cut = random.randint(part.lo + 1, part.lo + 2000)
            elif part.hi - part.lo > 1:
                cut = random.randint(part.lo + 1, part.hi - 1)
            else:
                continue
            split_day(db_path, ROAD, DAY, cut, batch_size=3)

    _check(db_path, written)


def test_concurrent_uploads_downloads_and_splits(tmp_path):
    db_path = str(tmp_path / "stress.db")
    init_db(db_path)
    random.seed(1)
    accepted: dict[str, int] = {}
    errors: list[BaseException] = []
    stop = threading.Event()

    def uploader(tid: int) -> None:
        try:
            for i in range(60):
                if stop.is_set():
                    return
                record = ParsedRecord(f"T{tid}-{i}", random.randint(0, 4000), "L1")
                with connect(db_path) as conn:
                    upload_records(conn, ROAD, DAY, [record])
                accepted[record.record_no] = record.mileage
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    def downloader() -> None:
        try:
            while not stop.is_set():
                with connect(db_path) as conn:
                    download_csv(conn, ROAD, DAY, 0, None)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=uploader, args=(i,)) for i in range(3)]
    threads.append(threading.Thread(target=downloader))
    for thread in threads:
        thread.start()
    try:
        for cut in (1000, 2000, 3000):
            time.sleep(0.05)
            try:
                split_day(db_path, ROAD, DAY, cut, batch_size=20)
            except SplitError:
                # A rival split may hold the lock; retrying is covered by
                # other tests.  The file must still be consistent afterwards.
                pass
    finally:
        stop.set()
        for thread in threads:
            thread.join()

    assert errors == []
    _check(db_path, accepted)
    assert len(accepted) > 0
