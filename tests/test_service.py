from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Callable

import pytest

from inspection.csvio import ParsedRecord, CsvFormatError, parse_records
from inspection.db import connect, init_db
from inspection.service import (
    ConflictError,
    SplitError,
    ValidationError,
    begin_split,
    day_overview,
    download_csv,
    split_day,
    upload_records,
)

ROAD = "G101"
DAY = "2026-11-24"


def rec(no: str, mileage: int, grade: str = "L1", line: int = 0) -> ParsedRecord:
    return ParsedRecord(record_no=no, mileage=mileage, grade=grade, line=line)


def parse_csv(text: str) -> list[ParsedRecord]:
    return parse_records(text.encode("utf-8"))


def all_records(db_path: str) -> list[sqlite3.Row]:
    with connect(db_path) as conn:
        return conn.execute(
            "SELECT r.record_no, r.mileage, r.grade, r.partition_id, "
            "p.status, p.lo, p.hi "
            "FROM records r JOIN partitions p ON p.id = r.partition_id "
            "ORDER BY r.mileage, r.record_no"
        ).fetchall()


def active_rows(db_path: str) -> list[sqlite3.Row]:
    with connect(db_path) as conn:
        return conn.execute(
            "SELECT record_no, mileage, grade, partition_id, p.lo, p.hi "
            "FROM records r JOIN partitions p ON p.id = r.partition_id "
            "WHERE p.status = 'active' ORDER BY record_no"
        ).fetchall()


def download(db_path: str, start: int = 0, end: int | None = None) -> list[list[str]]:
    with connect(db_path) as conn:
        data = download_csv(conn, ROAD, DAY, start, end)
    import csv as csv_module
    import io

    return list(csv_module.reader(io.StringIO(data.decode("utf-8-sig"))))


@pytest.fixture()
def db_path(tmp_path) -> str:
    path = str(tmp_path / "inspection.db")
    init_db(path)
    return path


def upload(db_path: str, *records: ParsedRecord):
    with connect(db_path) as conn:
        return upload_records(conn, ROAD, DAY, list(records))


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------


def test_parse_chinese_header_and_gb18030():
    parsed = parse_csv("记录号,整米里程,病害等级\nA-1,100,轻\nA-2,200,重\n")
    assert [(p.record_no, p.mileage, p.grade) for p in parsed] == [
        ("A-1", 100, "轻"),
        ("A-2", 200, "重"),
    ]

    parsed = parse_records("记录号,整米里程,病害等级\nA-1,100,轻\n".encode("gb18030"))
    assert parsed[0].record_no == "A-1"


def test_parse_english_and_headerless_rows():
    assert parse_csv("record_no,mileage,grade\nX,5,L2\n")[0].mileage == 5
    assert parse_csv("X,5,L2\nY,6,L3\n")[1].record_no == "Y"


def test_parse_rejects_bad_rows_with_line_numbers():
    with pytest.raises(CsvFormatError) as info:
        parse_csv("记录号,整米里程,病害等级\nA,10,L1\nB,x,L2\nC,,L3\n")
    messages = "\n".join(info.value.messages)
    assert "第 3 行" in messages
    assert "第 4 行" in messages


def test_parse_rejects_missing_column():
    with pytest.raises(CsvFormatError) as info:
        parse_csv("记录号,整米里程\nA,10\n")
    assert "病害等级" in str(info.value)


# ---------------------------------------------------------------------------
# Upload semantics
# ---------------------------------------------------------------------------


def test_upload_is_idempotent_for_identical_reports(db_path):
    first = upload(db_path, rec("A-1", 100), rec("A-2", 200))
    assert (first.inserted, first.unchanged) == (2, 0)

    second = upload(db_path, rec("A-1", 100), rec("A-2", 200))
    assert (second.inserted, second.unchanged) == (0, 2)
    assert len(all_records(db_path)) == 2


def test_duplicate_identical_rows_within_one_file_counted_once(db_path):
    result = upload(db_path, rec("A-1", 100), rec("A-1", 100, line=3))
    assert (result.inserted, result.unchanged) == (1, 1)
    assert len(all_records(db_path)) == 1


def test_conflicting_report_rejects_entire_file(db_path):
    upload(db_path, rec("A-1", 100, "L1"))
    with pytest.raises(ConflictError) as info:
        # A-2 is new but must not be written because A-1 conflicts.
        upload(db_path, rec("A-2", 200, "L2"), rec("A-1", 100, "L9"))
    assert any("A-1" in message for message in info.value.conflicts)
    rows = all_records(db_path)
    assert [row["record_no"] for row in rows] == ["A-1"]
    assert rows[0]["grade"] == "L1"


def test_conflict_within_file_rejects_file(db_path):
    with pytest.raises(ConflictError):
        upload(db_path, rec("A-1", 100, "L1"), rec("A-1", 101, "L1"))
    assert all_records(db_path) == []


def test_same_record_number_on_other_day_is_independent(db_path):
    upload(db_path, rec("A-1", 100))
    with connect(db_path) as conn:
        upload_records(
            conn, ROAD, "2026-11-25", [rec("A-1", 100, "L2")]
        )
    with connect(db_path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    assert total == 2


# ---------------------------------------------------------------------------
# Splitting
# ---------------------------------------------------------------------------


def test_split_cut_belongs_to_right_partition(db_path):
    upload(
        db_path,
        rec("A", 100),
        rec("B", 149),
        rec("C", 150),  # exactly the cut
        rec("D", 200),
    )
    moved = split_day(db_path, ROAD, DAY, 150)
    assert moved == 4

    rows = {row["record_no"]: row for row in all_records(db_path)}
    assert rows["A"]["lo"] == 0 and rows["A"]["hi"] == 150
    assert rows["B"]["lo"] == 0 and rows["B"]["hi"] == 150
    assert rows["C"]["lo"] == 150 and rows["C"]["hi"] is None
    assert rows["D"]["lo"] == 150 and rows["D"]["hi"] is None
    assert all(row["status"] == "active" for row in rows.values())
    assert len(rows) == 4  # no loss, no duplication


def test_split_second_level_is_possible(db_path):
    upload(db_path, rec("A", 100), rec("B", 200), rec("C", 300))
    split_day(db_path, ROAD, DAY, 150)
    split_day(db_path, ROAD, DAY, 250)
    rows = {row["record_no"]: row for row in all_records(db_path)}
    assert (rows["A"]["lo"], rows["A"]["hi"]) == (0, 150)
    assert (rows["B"]["lo"], rows["B"]["hi"]) == (150, 250)
    assert (rows["C"]["lo"], rows["C"]["hi"]) == (250, None)


@pytest.mark.parametrize("cut", [0, -5])
def test_invalid_cut_is_rejected_and_layout_unchanged(db_path, cut):
    upload(db_path, rec("A", 100), rec("B", 200))
    with pytest.raises((SplitError, ValidationError)):
        split_day(db_path, ROAD, DAY, cut)
    rows = all_records(db_path)
    assert len(rows) == 2
    assert {row["status"] for row in rows} == {"active"}
    assert all(row["lo"] == 0 and row["hi"] is None for row in rows)


def test_cut_above_all_records_is_still_inside_open_partition(db_path):
    upload(db_path, rec("A", 100), rec("B", 200))
    split_day(db_path, ROAD, DAY, 10_000)
    rows = {row["record_no"]: row for row in all_records(db_path)}
    assert (rows["A"]["lo"], rows["A"]["hi"]) == (0, 10_000)
    assert (rows["B"]["lo"], rows["B"]["hi"]) == (0, 10_000)
    with connect(db_path) as conn:
        active = conn.execute(
            "SELECT COUNT(*) FROM partitions WHERE status='active'"
        ).fetchone()[0]
    assert active == 2


def test_split_on_existing_boundary_rejected(db_path):
    upload(db_path, rec("A", 100), rec("B", 200))
    split_day(db_path, ROAD, DAY, 150)
    before = all_records(db_path)
    with pytest.raises(SplitError):
        split_day(db_path, ROAD, DAY, 150)
    after = all_records(db_path)
    assert [(r["record_no"], r["partition_id"]) for r in before] == [
        (r["record_no"], r["partition_id"]) for r in after
    ]


def test_reports_during_copy_are_kept_and_archived_after_switch(db_path):
    upload(db_path, *[rec(f"R{i:03d}", i * 10) for i in range(1, 61)])

    uploaded = {}

    def during_copy(job_id: int, copied: int) -> None:
        # Fires after each commit, i.e. while the source is still active and
        # the shadow partitions exist but are invisible.
        if copied and "late-left" not in uploaded:
            result = upload(
                db_path,
                rec("late-left", 30, "L7"),
                rec("late-right", 900, "L7"),
            )
            assert result.inserted == 2
            uploaded["late-left"] = True
            with connect(db_path) as conn:
                overview = day_overview(conn, ROAD, DAY)
            active = [p for p in overview.partitions if p.status == "active"]
            assert len(active) == 1
            assert overview.record_count == 62

    moved = split_day(db_path, ROAD, DAY, 300, batch_size=7, on_batch=during_copy)
    assert moved == 62

    rows = {row["record_no"]: row for row in all_records(db_path)}
    assert len(rows) == 62
    assert rows["late-left"]["lo"] == 0 and rows["late-left"]["hi"] == 300
    assert rows["late-right"]["lo"] == 300
    assert len({row["partition_id"] for row in rows.values()}) == 2

    # A later report lands directly in the correct new partition.
    upload(db_path, rec("after-left", 299), rec("after-right", 300))
    rows = {row["record_no"]: row for row in all_records(db_path)}
    assert rows["after-left"]["hi"] == 300
    assert rows["after-right"]["lo"] == 300

    assert len(active_rows(db_path)) == 64
    assert len({r["record_no"] for r in active_rows(db_path)}) == 64


def test_download_during_split_copy_sees_complete_day(db_path):
    upload(db_path, *[rec(f"R{i:03d}", i * 10) for i in range(1, 21)])
    observed = {}

    def during_copy(job_id: int, copied: int) -> None:
        if "seen" not in observed:
            observed["seen"] = download(db_path)
            assert len(observed["seen"]) == 21  # header + 20 records

    split_day(db_path, ROAD, DAY, 150, batch_size=5, on_batch=during_copy)
    final = download(db_path)
    assert final[1:] == observed["seen"][1:]


def test_failure_mid_copy_keeps_source_and_allows_retry(db_path):
    upload(db_path, *[rec(f"R{i:03d}", i * 10) for i in range(1, 21)])
    before = download(db_path)

    def explode(job_id: int, copied: int) -> None:
        if copied >= 10:
            raise RuntimeError("simulated copy failure")

    with pytest.raises(RuntimeError):
        split_day(db_path, ROAD, DAY, 150, batch_size=5, on_batch=explode)

    # Only the original partition remains, complete and queryable.
    with connect(db_path) as conn:
        parts = conn.execute(
            "SELECT id, status, split_state, lo, hi FROM partitions ORDER BY id"
        ).fetchall()
        jobs = conn.execute(
            "SELECT status, error FROM split_jobs ORDER BY id"
        ).fetchall()
    assert [(p["status"], p["split_state"]) for p in parts] == [("active", None)]
    assert jobs[-1]["status"] == "failed"
    assert "simulated" in jobs[-1]["error"]
    assert download(db_path) == before

    # Retry succeeds and the day is still exactly-once.
    moved = split_day(db_path, ROAD, DAY, 150)
    assert moved == 20
    rows = all_records(db_path)
    assert len(rows) == 20
    assert len({row["record_no"] for row in rows}) == 20
    assert download(db_path) == before


def test_interrupted_copy_is_recovered_on_startup(db_path):
    upload(db_path, *[rec(f"R{i:03d}", i * 10) for i in range(1, 11)])
    before = download(db_path)

    # Simulate a process that dies after creating shadows and staging one
    # batch, without running any cleanup.
    with connect(db_path) as conn:
        job = begin_split(conn, ROAD, DAY, 100)
    with connect(db_path) as conn:
        with conn:
            conn.execute(
                "INSERT INTO split_staging "
                "(job_id, record_id, record_no, mileage, grade) "
                "SELECT ?, id, record_no, mileage, grade FROM records "
                "WHERE partition_id = ? AND mileage < 50",
                (job["job_id"], job["source_id"]),
            )
        conn.execute(
            "UPDATE split_jobs SET copied_records = 5 WHERE id = ?",
            (job["job_id"],),
        )
    assert download(db_path) == before

    # A restart discards the partial shadows; the original remains complete.
    init_db(db_path)
    with connect(db_path) as conn:
        parts = conn.execute(
            "SELECT status, split_state, parent_id FROM partitions ORDER BY id"
        ).fetchall()
        jobs = conn.execute("SELECT status, error FROM split_jobs").fetchall()
        staging = conn.execute("SELECT COUNT(*) FROM split_staging").fetchone()[0]
    assert [(p["status"], p["split_state"]) for p in parts] == [("active", None)]
    assert jobs[-1]["status"] == "failed"
    assert "interrupted" in jobs[-1]["error"]
    assert staging == 0
    assert download(db_path) == before

    # And the split can be retried.
    split_day(db_path, ROAD, DAY, 100)
    assert len(all_records(db_path)) == 10
    assert download(db_path) == before


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------


def test_download_reads_only_intersecting_partitions_and_snapshot_layout(db_path):
    upload(db_path, *[rec(f"R{mileage}", mileage) for mileage in range(0, 1000, 100)])
    split_day(db_path, ROAD, DAY, 300)
    split_day(db_path, ROAD, DAY, 700)

    with connect(db_path) as conn:
        expected_layout = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM partitions WHERE road=? AND date=? "
                "AND status='active' AND lo <= 750 "
                "AND (hi IS NULL OR hi > 150) ORDER BY lo",
                (ROAD, DAY),
            )
        ]
    # [0,300), [300,700), [700,+inf) all intersect [150, 750].
    assert len(expected_layout) == 3

    observed: dict = {}

    def hook(partition_ids: list[int]) -> None:
        observed["layout"] = list(partition_ids)
        # A split switch commits after the layout was chosen but before any
        # record row is read.  WAL keeps this download on its snapshot.
        split_day(db_path, ROAD, DAY, 500)

    with connect(db_path) as conn:
        data = download_csv(
            conn, ROAD, DAY, 150, 750, after_layout=hook
        )
    import csv as csv_module
    import io

    rows = list(csv_module.reader(io.StringIO(data.decode("utf-8-sig"))))
    mileages = [int(row[1]) for row in rows[1:]]
    assert mileages == [200, 300, 400, 500, 600, 700]

    assert observed["layout"] == expected_layout
    with connect(db_path) as conn:
        active_now = conn.execute(
            "SELECT COUNT(*) FROM partitions WHERE status='active'"
        ).fetchone()[0]
    assert active_now == 4  # the mid-download switch added one partition

    # A fresh download still returns the same rows exactly once from the new
    # layout, with nothing lost or duplicated.
    fresh = download(db_path, 150, 750)
    assert fresh[1:] == rows[1:]


def test_download_open_ended_and_empty_ranges(db_path):
    upload(db_path, rec("A", 0), rec("B", 100), rec("C", 300))
    rows = download(db_path, 100, None)
    assert [row[0] for row in rows[1:]] == ["B", "C"]
    assert len(download(db_path, 500, None)) == 1  # header only
    assert [row[0] for row in download(db_path, 100, 100)[1:]] == ["B"]


def test_download_after_split_is_complete_and_ordered(db_path):
    upload(db_path, rec("B", 200), rec("A", 100), rec("D", 400), rec("C", 300))
    split_day(db_path, ROAD, DAY, 250)
    rows = download(db_path)
    assert [row[0] for row in rows[1:]] == ["A", "B", "C", "D"]
    assert [int(row[1]) for row in rows[1:]] == [100, 200, 300, 400]


# ---------------------------------------------------------------------------
# Concurrent uploads during the switch window
# ---------------------------------------------------------------------------


def test_concurrent_uploads_never_lost_or_duplicated(db_path):
    upload(db_path, *[rec(f"R{i:04d}", i) for i in range(0, 200)])
    stop = threading.Event()
    errors: list[BaseException] = []
    inserted: list[str] = []

    def writer_loop(prefix: str, miles: range) -> None:
        try:
            for mileage in miles:
                if stop.is_set():
                    return
                upload(db_path, rec(f"{prefix}-{mileage}", mileage, "L5"))
                inserted.append(f"{prefix}-{mileage}")
                time.sleep(0.001)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [
        threading.Thread(target=writer_loop, args=("L", range(0, 200, 6))),
        threading.Thread(target=writer_loop, args=("R", range(200, 400, 6))),
    ]
    for thread in threads:
        thread.start()
    try:
        split_day(db_path, ROAD, DAY, 200, batch_size=13)
    finally:
        stop.set()
        for thread in threads:
            thread.join()

    assert errors == []

    active = active_rows(db_path)
    numbers = [row["record_no"] for row in active]
    assert len(numbers) == len(set(numbers))
    assert set(inserted) <= set(numbers)
    # Every record is stored in a partition whose half-open range contains it.
    for row in active:
        assert row["lo"] <= row["mileage"] and (
            row["hi"] is None or row["mileage"] < row["hi"]
        )

    assert len(download(db_path)) - 1 == len(active)


# ---------------------------------------------------------------------------
# Multi-process safety
# ---------------------------------------------------------------------------


def test_startup_does_not_recover_a_live_process_split(db_path):
    from inspection.db import split_lock

    upload(db_path, rec("A", 100), rec("B", 200))
    with split_lock(db_path):
        with connect(db_path) as conn:
            job = begin_split(conn, ROAD, DAY, 150)
        # Another process starts while the first is still copying.
        init_db(db_path)
        with connect(db_path) as conn:
            live = conn.execute(
                "SELECT status FROM split_jobs WHERE id=?", (job["job_id"],)
            ).fetchone()[0]
            parts = conn.execute(
                "SELECT status FROM partitions ORDER BY id"
            ).fetchall()
        assert live == "copying"
        assert [p["status"] for p in parts] == ["active", "shadow", "shadow"]

    # After the live owner exits, a restart can recover the now-stale job.
    init_db(db_path)
    with connect(db_path) as conn:
        assert conn.execute(
            "SELECT status FROM split_jobs WHERE id=?", (job["job_id"],)
        ).fetchone()[0] == "failed"
        assert conn.execute(
            "SELECT COUNT(*) FROM partitions WHERE status='active'"
        ).fetchone()[0] == 1


def test_second_split_is_rejected_while_lock_held(db_path):
    from inspection.db import split_lock

    upload(db_path, rec("A", 100), rec("B", 200))
    with split_lock(db_path):
        with pytest.raises(SplitError, match="已有拆分任务"):
            split_day(db_path, ROAD, DAY, 150)
    # Nothing was created by the rejected attempt.
    with connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM split_jobs").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM partitions"
        ).fetchone()[0] == 1
    split_day(db_path, ROAD, DAY, 150)
    assert len(all_records(db_path)) == 2


def test_shadow_partitions_are_not_downloaded_or_uploaded_into(db_path):
    upload(db_path, rec("A", 100), rec("B", 200))
    observed = {}

    def during_copy(job_id: int, copied: int) -> None:
        if observed:
            return
        observed["download"] = download(db_path)
        observed["upload"] = upload(db_path, rec("C", 100, "L9"))
        with connect(db_path) as conn:
            observed["source"] = conn.execute(
                "SELECT source_partition_id FROM split_jobs WHERE id=?",
                (job_id,),
            ).fetchone()[0]
            observed["c_partition"] = conn.execute(
                "SELECT partition_id FROM records WHERE record_no='C'"
            ).fetchone()[0]

    moved = split_day(db_path, ROAD, DAY, 150, batch_size=1, on_batch=during_copy)
    assert [row[0] for row in observed["download"][1:]] == ["A", "B"]
    assert observed["upload"].inserted == 1
    assert observed["c_partition"] == observed["source"]
    assert moved == 3
    rows = {row["record_no"]: row for row in all_records(db_path)}
    assert rows["C"]["partition_id"] != observed["source"]
    assert rows["C"]["hi"] == 150


def test_retry_without_restart_recovers_stale_copying_job(db_path):
    upload(db_path, rec("A", 100), rec("B", 300))
    observed = {}

    def die(job_id: int, copied: int) -> None:
        if copied:
            observed["job"] = job_id
            raise RuntimeError("process would die here")

    with pytest.raises(RuntimeError):
        split_day(db_path, ROAD, DAY, 200, batch_size=1, on_batch=die)

    # Simulate the crashed process never cleaning up and no restart: put the
    # job back into 'copying' with a shadow partition still present.
    with connect(db_path) as conn:
        with conn:
            conn.execute(
                "UPDATE split_jobs SET status='copying', finished_at=NULL "
                "WHERE id=?", (observed["job"],),
            )
            conn.execute(
                "UPDATE partitions SET split_state='copying' WHERE id="
                "(SELECT source_partition_id FROM split_jobs WHERE id=?)",
                (observed["job"],),
            )
    # A retry that wins the lock performs recovery first, then splits; the
    # stale marker must not block it even without a process restart.
    moved = split_day(db_path, ROAD, DAY, 200)
    assert moved == 2
    with connect(db_path) as conn:
        jobs = conn.execute(
            "SELECT id, status FROM split_jobs ORDER BY id"
        ).fetchall()
    assert [(j["id"], j["status"]) for j in jobs] == [
        (observed["job"], "failed"),
        (jobs[-1]["id"], "switched"),
    ]
    assert len(all_records(db_path)) == 2


def test_idempotent_and_conflicting_reports_during_copy(db_path):
    upload(db_path, rec("A", 100), rec("B", 300))
    observed = {}

    def during_copy(job_id: int, copied: int) -> None:
        if observed:
            return
        # Identical re-report is a no-op; a conflicting one is rejected as a
        # whole and must not disturb the in-progress copy.
        observed["same"] = upload(db_path, rec("A", 100))
        with pytest.raises(ConflictError):
            upload(db_path, rec("C", 200), rec("B", 300, "L9"))
        with connect(db_path) as conn:
            observed["c_exists"] = conn.execute(
                "SELECT COUNT(*) FROM records WHERE record_no='C'"
            ).fetchone()[0]

    moved = split_day(db_path, ROAD, DAY, 200, batch_size=1, on_batch=during_copy)
    assert observed["same"].inserted == 0
    assert observed["same"].unchanged == 1
    assert observed["c_exists"] == 0
    assert moved == 2
    rows = {row["record_no"]: row for row in all_records(db_path)}
    assert rows["A"]["hi"] == 200 and rows["B"]["lo"] == 200


def test_unrecognized_header_is_reported_clearly():
    with pytest.raises(CsvFormatError) as info:
        parse_csv("编号X,里程X,等级X\nA,100,L1\n")
    assert "无法识别表头" in str(info.value)
