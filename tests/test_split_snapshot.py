"""Regression tests for bounded split copy under continuous supplements.

The web-reported bug: when new reports catch up with copy progress, the split
keeps chasing the moving tail and never reaches the switch.  These tests pin
the required contract:

* copy only stages the split-start snapshot (fixed high-water mark);
* reports committed during the copy still succeed and stay downloadable from
  the original active partition before the switch;
* supplements are prepared before the switch transaction and end up, together
  with old rows, in exactly one new partition, cut point going right;
* an interruption before the switch leaves every confirmed row in the source
  and the split is retryable.
"""

from __future__ import annotations

import csv as csv_module
import io
import threading
import time

import pytest

from inspection.csvio import ParsedRecord
from inspection.db import connect, init_db
from inspection.service import (
    SplitError,
    download_csv,
    split_day,
    upload_records,
)

ROAD = "G-CHASE"
DAY = "2026-11-24"


def rec(no: str, mileage: int, grade: str = "L1") -> ParsedRecord:
    return ParsedRecord(record_no=no, mileage=mileage, grade=grade, line=0)


@pytest.fixture()
def db_path(tmp_path) -> str:
    path = str(tmp_path / "chase.db")
    init_db(path)
    return path


def upload(db_path: str, *records: ParsedRecord):
    with connect(db_path) as conn:
        return upload_records(conn, ROAD, DAY, list(records))


def rows_by_no(db_path: str):
    with connect(db_path) as conn:
        return {
            row["record_no"]: row
            for row in conn.execute(
                "SELECT r.record_no, r.mileage, r.partition_id, p.lo, p.hi, "
                "p.status FROM records r JOIN partitions p ON p.id = r.partition_id"
            ).fetchall()
        }


def download_numbers(db_path: str, start: int = 0, end: int | None = None):
    with connect(db_path) as conn:
        data = download_csv(conn, ROAD, DAY, start, end)
    lines = list(csv_module.reader(io.StringIO(data.decode("utf-8-sig"))))
    return [line[0] for line in lines[1:]]


def test_copy_only_processes_split_start_snapshot_even_when_reports_catch_up(
    db_path,
):
    initial = 40
    upload(db_path, *[rec(f"R{i:04d}", i) for i in range(1, initial + 1)])

    batch = 5
    state = {"calls": 0, "max_copied_in_hook": 0}

    def every_batch_adds_a_full_batch(job_id: int, copied: int) -> None:
        # Adversarial: exactly as many new rows as the batch size land after
        # every copy batch, so an open-ended id>last_id scan never drains.
        state["calls"] += 1
        state["max_copied_in_hook"] = max(state["max_copied_in_hook"], copied)
        k = state["calls"]
        upload(
            db_path,
            *[
                rec(f"NEW{k}-{j}", 100_000 + k * batch + j)
                for j in range(batch)
            ],
        )

    moved = split_day(
        db_path,
        ROAD,
        DAY,
        50_000,
        batch_size=batch,
        on_batch=every_batch_adds_a_full_batch,
    )

    # Exactly ceil(40/5) bounded snapshot batches, never a chasing loop.
    assert state["calls"] == initial // batch
    # Progress observed from batch hooks never exceeds the start snapshot.
    assert state["max_copied_in_hook"] == initial
    # 40 initial + 8 batches * 5 supplements.
    assert moved == initial + state["calls"] * batch

    rows = rows_by_no(db_path)
    assert len(rows) == moved
    assert len(set(rows)) == moved
    # Cut belongs to the right: initial mileages 1..40 are all left of 50000,
    # every supplement (>= 100005) is right.
    assert all(row["hi"] == 50_000 for no, row in rows.items() if no.startswith("R"))
    assert all(row["lo"] == 50_000 for no, row in rows.items() if no.startswith("NEW"))
    assert all(row["status"] == "active" for row in rows.values())


def test_confirmed_supplements_are_downloadable_from_source_before_switch(
    db_path,
):
    upload(db_path, *[rec(f"R{i:03d}", i * 10) for i in range(1, 21)])
    seen = {}

    def hook(job_id: int, copied: int) -> None:
        if copied == 10 and "checked" not in seen:
            upload(db_path, rec("late-left", 30), rec("late-at-cut", 150))
            with connect(db_path) as conn:
                parts = conn.execute(
                    "SELECT id, status FROM partitions WHERE road=? AND date=?",
                    (ROAD, DAY),
                ).fetchall()
                source_id = conn.execute(
                    "SELECT source_partition_id FROM split_jobs WHERE id=?",
                    (job_id,),
                ).fetchone()[0]
                placed = conn.execute(
                    "SELECT COUNT(*) FROM records WHERE partition_id=?",
                    (source_id,),
                ).fetchone()[0]
            active = [p for p in parts if p["status"] == "active"]
            assert len(active) == 1  # shadows invisible
            # Every confirmed row (20 + 2 supplements) is still in the source.
            assert placed == 22
            all_rows = download_numbers(db_path)
            assert len(all_rows) == 22
            assert {"late-left", "late-at-cut"} <= set(all_rows)
            seen["checked"] = True

    moved = split_day(db_path, ROAD, DAY, 150, batch_size=5, on_batch=hook)
    assert seen.get("checked") is True
    assert moved == 22

    # Cut point 150 goes right together with higher-mileage rows.
    left = download_numbers(db_path, 0, 149)
    right = download_numbers(db_path, 150, None)
    assert "late-left" in left
    assert "late-at-cut" in right
    assert "late-at-cut" not in left
    assert len(left) + len(right) == 22
    assert set(left).isdisjoint(right)


def test_continuous_supplements_throughout_split_all_arrive_exactly_once(db_path):
    upload(db_path, *[rec(f"R{i:04d}", i) for i in range(0, 300)])
    stop = threading.Event()
    errors: list[BaseException] = []
    accepted: set[str] = set()
    lock = threading.Lock()

    def writer() -> None:
        i = 0
        try:
            while not stop.is_set():
                # Alternate sides of the cut at 150.
                mileage = 10 + (i % 2) * 200 + (i // 2) % 120
                no = f"LIVE-{i:05d}"
                upload(db_path, rec(no, mileage, "L9"))
                with lock:
                    accepted.add(no)
                i += 1
                time.sleep(0.0005)
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    try:
        # Tiny batches stretch the copy so the writer overlaps it heavily.
        moved = split_day(db_path, ROAD, DAY, 150, batch_size=7)
    finally:
        stop.set()
        thread.join(timeout=10)

    assert errors == []
    rows = rows_by_no(db_path)
    total = 300 + len(accepted)
    # Every acknowledged upload is stored exactly once ...
    assert len(rows) == total
    assert len(set(rows)) == total
    assert accepted <= set(rows)
    # ... and split_day moved the whole pre-switch source (some writer
    # commits may legitimately land just after the switch and not be in the
    # returned moved count).
    assert 300 <= moved <= total

    # Mileage downloads reconstruct the whole set exactly once.
    left = download_numbers(db_path, 0, 149)
    right = download_numbers(db_path, 150, None)
    assert len(left) == len(set(left))
    assert len(right) == len(set(right))
    assert set(left).isdisjoint(right)
    assert len(left) + len(right) == total
    # Every stored row sits inside the active partition that owns its mileage.
    for row in rows.values():
        assert row["status"] == "active"
        assert row["lo"] <= row["mileage"]
        assert row["hi"] is None or row["mileage"] < row["hi"]


def test_interrupt_after_supplements_prepared_keeps_source_and_retry_works(
    db_path,
):
    upload(db_path, *[rec(f"R{i:03d}", i * 10) for i in range(1, 16)])
    before = download_numbers(db_path)
    state = {"failed_once": False}

    def abort_right_before_switch(job_id: int, staged: int) -> None:
        # Preparation has staged the snapshot plus all supplements so far.
        assert staged >= 15
        # A last-moment confirmed report still commits independently.
        upload(db_path, rec("last-instant", 999))
        state["failed_once"] = True
        raise RuntimeError("simulated kill right before switch")

    with pytest.raises(RuntimeError):
        split_day(
            db_path,
            ROAD,
            DAY,
            500,
            batch_size=4,
            on_before_switch=abort_right_before_switch,
        )
    assert state["failed_once"] is True

    # Nothing switched: one active partition, all 16 confirmed rows present
    # and downloadable from the original partition.
    with connect(db_path) as conn:
        parts = conn.execute(
            "SELECT id, status, split_state, lo, hi FROM partitions ORDER BY id"
        ).fetchall()
        job = conn.execute(
            "SELECT status, error FROM split_jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert [(p["status"], p["split_state"]) for p in parts] == [("active", None)]
    assert job["status"] == "failed"
    assert "simulated kill" in job["error"]

    after_fail = download_numbers(db_path)
    assert len(after_fail) == len(before) + 1
    assert "last-instant" in after_fail

    # Retry succeeds, moving every confirmed row exactly once.
    moved = split_day(db_path, ROAD, DAY, 500, batch_size=4)
    assert moved == 16
    rows = rows_by_no(db_path)
    assert len(rows) == 16
    assert len({r["partition_id"] for r in rows.values()}) == 2
    final_all = download_numbers(db_path)
    assert len(final_all) == 16
    assert set(final_all) == set(after_fail)

    with pytest.raises(SplitError):
        # Same boundary is no longer inside one leaf.
        split_day(db_path, ROAD, DAY, 500)


def test_old_database_without_high_water_column_is_migrated(tmp_path):
    """A pre-existing split_jobs table gains copy_upto_id transparently."""

    path = str(tmp_path / "old.db")
    init_db(path)
    with connect(path) as conn:
        conn.execute("DROP TABLE split_jobs")
        conn.execute(
            "CREATE TABLE split_jobs ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "source_partition_id INTEGER NOT NULL,"
            "cut INTEGER NOT NULL,"
            "left_partition_id INTEGER,"
            "right_partition_id INTEGER,"
            "status TEXT NOT NULL,"
            "error TEXT,"
            "copied_records INTEGER NOT NULL DEFAULT 0,"
            "created_at TEXT,"
            "finished_at TEXT)"
        )
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(split_jobs)")}
        assert "copy_upto_id" not in cols

    # Re-running startup performs the ALTER and still works end to end.
    init_db(path)
    upload(path, rec("A", 1), rec("B", 200))
    assert split_day(path, ROAD, DAY, 100) == 2
    assert len(rows_by_no(path)) == 2
