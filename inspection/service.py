"""Inspection business logic: idempotent uploads, splits, and downloads.

Consistency model
-----------------
* Every day (road + date) starts as one active partition ``[0, +inf)``.
* A split creates two shadow partitions and stages a copy of the source rows.
  The source stays active and readable for the whole copy phase, so uploads
  keep working and a download keeps seeing a complete day.
* The switch is a single ``BEGIN IMMEDIATE`` transaction: any rows that
  arrived during the copy are folded in, the source rows are removed and the
  same rows are inserted into the two children, then statuses are flipped.
  Readers therefore see either the source or the two children, never a mix.
* If anything fails before the switch, cleanup (or startup recovery) drops
  the shadows/staging and leaves the unchanged source partition in place.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from .csvio import ParsedRecord, render_records_csv
from .db import (
    SplitLockBusy,
    connect,
    read_transaction,
    recover_interrupted_splits,
    split_lock,
    write_transaction,
)

logger = logging.getLogger("inspection.service")

MAX_MILEAGE = 9_223_372_036_854_775_807
MAX_ERROR_CHARS = 1000
EXISTING_LOOKUP_CHUNK = 400


class ServiceError(Exception):
    """Base class for errors that are safe to show to the user."""


class ValidationError(ServiceError):
    pass


class ConflictError(ServiceError):
    def __init__(self, conflicts: Sequence[str]):
        self.conflicts = list(conflicts)
        super().__init__("存在内容冲突，整份文件未写入")


class SplitError(ServiceError):
    pass


@dataclass(frozen=True)
class UploadResult:
    total_rows: int
    inserted: int
    unchanged: int
    partition_count: int


@dataclass(frozen=True)
class PartitionInfo:
    id: int
    road: str
    date: str
    lo: int
    hi: int | None
    status: str
    split_state: str | None
    record_count: int


@dataclass(frozen=True)
class SplitJobInfo:
    id: int
    source_partition_id: int
    cut: int
    status: str
    error: str | None
    copied_records: int
    created_at: str
    finished_at: str | None


@dataclass(frozen=True)
class DayOverview:
    road: str
    date: str
    partitions: list[PartitionInfo]
    record_count: int
    split_jobs: list[SplitJobInfo]


def _active_partitions(
    conn: sqlite3.Connection, road: str, date: str
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, lo, hi FROM partitions "
        "WHERE road = ? AND date = ? AND status = 'active' "
        "ORDER BY lo, id",
        (road, date),
    ).fetchall()


def _target_partition(parts: Sequence[sqlite3.Row], mileage: int) -> sqlite3.Row:
    matches = [
        part
        for part in parts
        if part["lo"] <= mileage and (part["hi"] is None or mileage < part["hi"])
    ]
    if len(matches) != 1:
        raise ServiceError(
            f"里程 {mileage} 未能唯一对应到有效分区（匹配 {len(matches)} 个），"
            "分区数据可能损坏，请联系管理员"
        )
    return matches[0]


def _fetch_existing(
    conn: sqlite3.Connection, road: str, date: str, record_nos: Iterable[str]
) -> dict[str, tuple[int, str]]:
    unique = list(dict.fromkeys(record_nos))
    existing: dict[str, tuple[int, str]] = {}
    for offset in range(0, len(unique), EXISTING_LOOKUP_CHUNK):
        chunk = unique[offset : offset + EXISTING_LOOKUP_CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            "SELECT record_no, mileage, grade FROM records "
            f"WHERE road = ? AND date = ? AND record_no IN ({placeholders})",
            (road, date, *chunk),
        ).fetchall()
        for row in rows:
            existing[row["record_no"]] = (row["mileage"], row["grade"])
    return existing


def upload_records(
    conn: sqlite3.Connection,
    road: str,
    date: str,
    records: Sequence[ParsedRecord],
) -> UploadResult:
    """Insert a day's CSV rows idempotently.

    * Same record number with identical mileage+grade: accepted silently once.
    * Same record number with different content: the whole file is rejected.
    * Duplicate rows inside the file are treated with the same rule.
    """

    if not records:
        raise ValidationError("没有可写入的记录")

    with write_transaction(conn):
        parts = _active_partitions(conn, road, date)
        if not parts:
            conn.execute(
                "INSERT INTO partitions (road, date, lo, hi, status) "
                "VALUES (?, ?, 0, NULL, 'active')",
                (road, date),
            )
            parts = _active_partitions(conn, road, date)
        if not parts:
            raise ServiceError("无法创建当日分区")

        existing = _fetch_existing(
            conn, road, date, (record.record_no for record in records)
        )

        seen: dict[str, ParsedRecord] = {}
        conflicts: list[str] = []
        conflict_nos: set[str] = set()
        to_insert: list[ParsedRecord] = []
        unchanged = 0

        for record in records:
            first = seen.get(record.record_no)
            if first is not None:
                if (first.mileage, first.grade) == (
                    record.mileage,
                    record.grade,
                ):
                    unchanged += 1
                elif record.record_no not in conflict_nos:
                    conflict_nos.add(record.record_no)
                    where = f"第 {record.line} 行" if record.line else "文件中"
                    conflicts.append(
                        f"{where}记录号 {record.record_no!r} "
                        f"与文件第 {first.line} 行重复但内容不同"
                        f"（里程 {first.mileage}→{record.mileage}，"
                        f"等级 {first.grade!r}→{record.grade!r}）"
                    )
                continue

            seen[record.record_no] = record
            stored = existing.get(record.record_no)
            if stored is not None:
                if stored == (record.mileage, record.grade):
                    unchanged += 1
                elif record.record_no not in conflict_nos:
                    conflict_nos.add(record.record_no)
                    where = f"第 {record.line} 行" if record.line else "文件中"
                    conflicts.append(
                        f"{where}记录号 {record.record_no!r} 已存在但内容不同"
                        f"（库中 里程 {stored[0]}、等级 {stored[1]!r}；"
                        f"文件中 里程 {record.mileage}、等级 {record.grade!r}）"
                    )
                continue

            to_insert.append(record)

        if conflicts:
            # Raising inside write_transaction rolls the entire file back,
            # including the possibly-created day partition.
            raise ConflictError(conflicts)

        for record in to_insert:
            target = _target_partition(parts, record.mileage)
            conn.execute(
                "INSERT INTO records "
                "(road, date, partition_id, record_no, mileage, grade) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    road,
                    date,
                    target["id"],
                    record.record_no,
                    record.mileage,
                    record.grade,
                ),
            )

        partition_count = len(parts)
        logger.info(
            "upload road=%s date=%s rows=%d inserted=%d unchanged=%d partitions=%d",
            road,
            date,
            len(records),
            len(to_insert),
            unchanged,
            partition_count,
        )
        return UploadResult(
            total_rows=len(records),
            inserted=len(to_insert),
            unchanged=unchanged,
            partition_count=partition_count,
        )


def _split_source(
    conn: sqlite3.Connection, road: str, date: str, cut: int
) -> sqlite3.Row:
    rows = conn.execute(
        "SELECT id, lo, hi, status, split_state FROM partitions "
        "WHERE road = ? AND date = ? AND status = 'active' "
        "AND lo < ? AND (hi IS NULL OR ? < hi) "
        "ORDER BY lo, id",
        (road, date, cut, cut),
    ).fetchall()
    if not rows:
        raise SplitError(
            "找不到包含该切点的未拆分分区；切点必须大于分区左端且小于右端"
        )
    if len(rows) > 1:
        raise ServiceError("切点同时落入多个分区，分区数据可能损坏")
    source = rows[0]
    if source["split_state"] is not None:
        raise SplitError("该分区已有拆分任务在进行")
    copying = conn.execute(
        "SELECT 1 FROM split_jobs WHERE status = 'copying' "
        "AND source_partition_id IN "
        "(SELECT id FROM partitions WHERE road = ? AND date = ?)",
        (road, date),
    ).fetchone()
    if copying:
        raise SplitError("该公路当天已有拆分任务在进行，请等待完成后再试")
    return source


def begin_split(conn: sqlite3.Connection, road: str, date: str, cut: int) -> dict:
    """Create the two shadow partitions and mark the copy phase as started."""

    if cut < 0 or cut > MAX_MILEAGE:
        raise ValidationError("切点里程必须是有效的非负整数")

    with write_transaction(conn):
        source = _split_source(conn, road, date, cut)
        left_id = conn.execute(
            "INSERT INTO partitions (road, date, lo, hi, status, parent_id) "
            "VALUES (?, ?, ?, ?, 'shadow', ?)",
            (road, date, source["lo"], cut, source["id"]),
        ).lastrowid
        right_id = conn.execute(
            "INSERT INTO partitions (road, date, lo, hi, status, parent_id) "
            "VALUES (?, ?, ?, ?, 'shadow', ?)",
            (road, date, cut, source["hi"], source["id"]),
        ).lastrowid
        job_id = conn.execute(
            "INSERT INTO split_jobs "
            "(source_partition_id, cut, left_partition_id, right_partition_id, "
            "status) VALUES (?, ?, ?, ?, 'copying')",
            (source["id"], cut, left_id, right_id),
        ).lastrowid
        updated = conn.execute(
            "UPDATE partitions SET split_state = 'copying' "
            "WHERE id = ? AND status = 'active' AND split_state IS NULL",
            (source["id"],),
        ).rowcount
        if updated != 1:
            raise SplitError("该分区已有拆分任务在进行")
        logger.info(
            "split started job=%s road=%s date=%s source=%s cut=%s",
            job_id,
            road,
            date,
            source["id"],
            cut,
        )
        return {
            "job_id": job_id,
            "source_id": source["id"],
            "left_id": left_id,
            "right_id": right_id,
            "lo": source["lo"],
            "hi": source["hi"],
            "cut": cut,
        }


def _job_row(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM split_jobs WHERE id = ?", (job_id,)
    ).fetchone()


def _copy_records(
    db_path: str,
    job: dict,
    batch_size: int,
    on_batch: Callable[[int, int], None] | None,
) -> None:
    """Stage a copy in short transactions, leaving the source writable.

    Rows inserted by concurrent uploads receive larger row ids and are picked
    up by a later batch, or by the final delta copy inside the switch.
    """

    last_id = 0
    copied = 0
    while True:
        with connect(db_path) as reader:
            rows = reader.execute(
                "SELECT id, record_no, mileage, grade FROM records "
                "WHERE partition_id = ? AND id > ? ORDER BY id LIMIT ?",
                (job["source_id"], last_id, batch_size),
            ).fetchall()
        if not rows:
            break

        with connect(db_path) as writer:
            with write_transaction(writer):
                current = _job_row(writer, job["job_id"])
                if current is None or current["status"] != "copying":
                    raise SplitError("拆分任务已不可用")
                writer.executemany(
                    "INSERT OR IGNORE INTO split_staging "
                    "(job_id, record_id, record_no, mileage, grade) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [
                        (
                            job["job_id"],
                            row["id"],
                            row["record_no"],
                            row["mileage"],
                            row["grade"],
                        )
                        for row in rows
                    ],
                )
                copied = writer.execute(
                    "SELECT COUNT(*) FROM split_staging WHERE job_id = ?",
                    (job["job_id"],),
                ).fetchone()[0]
                writer.execute(
                    "UPDATE split_jobs SET copied_records = ? WHERE id = ?",
                    (copied, job["job_id"]),
                )

        last_id = rows[-1]["id"]
        if on_batch is not None:
            on_batch(job["job_id"], copied)


def _switch_split(db_path: str, job_id: int) -> int:
    """Atomically move every source row into its child and flip the layout."""

    with connect(db_path) as conn:
        with write_transaction(conn):
            job = _job_row(conn, job_id)
            if job is None or job["status"] != "copying":
                raise SplitError("拆分任务不在复制阶段，无法切换")
            source = conn.execute(
                "SELECT * FROM partitions WHERE id = ?",
                (job["source_partition_id"],),
            ).fetchone()
            if (
                source is None
                or source["status"] != "active"
                or source["split_state"] != "copying"
            ):
                raise SplitError("原分区状态已变化，无法切换")
            left = conn.execute(
                "SELECT * FROM partitions WHERE id = ?",
                (job["left_partition_id"],),
            ).fetchone()
            right = conn.execute(
                "SELECT * FROM partitions WHERE id = ?",
                (job["right_partition_id"],),
            ).fetchone()
            if (
                left is None
                or right is None
                or left["status"] != "shadow"
                or right["status"] != "shadow"
            ):
                raise SplitError("影子分区状态已变化，无法切换")
            if left["hi"] != job["cut"] or right["lo"] != job["cut"]:
                raise SplitError("影子分区边界与切点不一致")

            # Fold in rows uploaded after the last staged batch.  The write
            # transaction prevents uploads from committing between this delta
            # copy, the source delete, and the child insert.
            conn.execute(
                "INSERT OR IGNORE INTO split_staging "
                "(job_id, record_id, record_no, mileage, grade) "
                "SELECT ?, id, record_no, mileage, grade FROM records "
                "WHERE partition_id = ?",
                (job_id, source["id"]),
            )
            expected = conn.execute(
                "SELECT COUNT(*) FROM records WHERE partition_id = ?",
                (source["id"],),
            ).fetchone()[0]
            staged = conn.execute(
                "SELECT COUNT(*) FROM split_staging WHERE job_id = ?",
                (job_id,),
            ).fetchone()[0]
            if expected != staged:
                raise SplitError(
                    f"搬移校验失败：原分区 {expected} 条，暂存 {staged} 条"
                )

            conn.execute(
                "DELETE FROM records WHERE partition_id = ?", (source["id"],)
            )
            conn.execute(
                "INSERT INTO records "
                "(road, date, partition_id, record_no, mileage, grade) "
                "SELECT ?, ?, CASE WHEN mileage < ? THEN ? ELSE ? END, "
                "record_no, mileage, grade "
                "FROM split_staging WHERE job_id = ? ORDER BY record_id",
                (
                    source["road"],
                    source["date"],
                    job["cut"],
                    job["left_partition_id"],
                    job["right_partition_id"],
                    job_id,
                ),
            )
            inserted = conn.execute("SELECT changes()").fetchone()[0]
            if inserted != expected:
                raise SplitError(
                    f"切换写入校验失败：应写 {expected} 条，实际 {inserted} 条"
                )

            conn.execute(
                "DELETE FROM split_staging WHERE job_id = ?", (job_id,)
            )
            conn.execute(
                "UPDATE split_jobs SET status = 'switched', "
                "copied_records = ?, finished_at = "
                "strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), error = NULL "
                "WHERE id = ?",
                (expected, job_id),
            )
            conn.execute(
                "UPDATE partitions SET status = 'retired', split_state = NULL "
                "WHERE id = ?",
                (source["id"],),
            )
            conn.execute(
                "UPDATE partitions SET status = 'active' WHERE id IN (?, ?)",
                (job["left_partition_id"], job["right_partition_id"]),
            )
            logger.info(
                "split switched job=%s source=%s records=%d cut=%s",
                job_id,
                source["id"],
                expected,
                job["cut"],
            )
            return expected


def fail_split(db_path: str, job_id: int, error: str) -> bool:
    """Roll back a pre-switch split, keeping the source complete and retryable.

    Safe to call more than once and safe if the switch already committed (in
    which case it does nothing).
    """

    message = (error or "未知错误")[:MAX_ERROR_CHARS]
    try:
        with connect(db_path) as conn:
            with write_transaction(conn):
                job = _job_row(conn, job_id)
                if job is None or job["status"] != "copying":
                    return False
                child_ids = [
                    pid
                    for pid in (
                        job["left_partition_id"],
                        job["right_partition_id"],
                    )
                    if pid is not None
                ]
                if child_ids:
                    placeholders = ",".join("?" for _ in child_ids)
                    conn.execute(
                        f"DELETE FROM records WHERE partition_id IN ({placeholders})",
                        child_ids,
                    )
                    conn.execute(
                        f"DELETE FROM partitions WHERE id IN ({placeholders}) "
                        "AND status = 'shadow'",
                        child_ids,
                    )
                conn.execute(
                    "DELETE FROM split_staging WHERE job_id = ?", (job_id,)
                )
                conn.execute(
                    "UPDATE partitions SET split_state = NULL "
                    "WHERE id = ? AND split_state = 'copying'",
                    (job["source_partition_id"],),
                )
                conn.execute(
                    "UPDATE split_jobs SET status = 'failed', error = ?, "
                    "finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                    "WHERE id = ?",
                    (message, job_id),
                )
                logger.warning(
                    "split failed job=%s source=%s error=%s",
                    job_id,
                    job["source_partition_id"],
                    message,
                )
                return True
    except sqlite3.Error:
        # Startup recovery in init_db() performs the same cleanup next time.
        logger.exception("failed to clean up split job %s", job_id)
        return False


def split_day(
    db_path: str,
    road: str,
    date: str,
    cut: int,
    *,
    batch_size: int = 500,
    on_batch: Callable[[int, int], None] | None = None,
) -> int:
    """Split a day leaf at ``cut``.  Mileage equal to the cut goes right.

    ``on_batch`` is an observability/test hook invoked after each copy batch
    has committed, while no write lock is held.
    """

    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    try:
        with split_lock(db_path):
            # Owning the lock proves no live copy exists, so a stale 'copying'
            # job (e.g. a killed process with no restart) can be rolled back
            # now.  This makes retrying the split work without a restart.
            recover_interrupted_splits(db_path)
            try:
                with connect(db_path) as conn:
                    job = begin_split(conn, road, date, cut)
            except sqlite3.IntegrityError as exc:
                # The per-day "only one split copying" unique indexes fire
                # here while a rival request holds the day's split.  The
                # failed transaction already rolled back its shadows.
                raise SplitError("该公路当天已有拆分任务在进行，请稍后重试") from exc
            except sqlite3.OperationalError as exc:
                raise SplitError(f"无法开始拆分：{exc}") from exc

            try:
                _copy_records(db_path, job, batch_size, on_batch)
                return _switch_split(db_path, job["job_id"])
            except BaseException as exc:
                fail_split(db_path, job["job_id"], str(exc))
                if isinstance(exc, ServiceError):
                    raise
                if isinstance(exc, sqlite3.Error):
                    raise SplitError(f"拆分失败，原分区已保留：{exc}") from exc
                raise
    except SplitLockBusy as exc:
        raise SplitError("已有拆分任务在进行，请稍后重试") from exc


def download_csv(
    conn: sqlite3.Connection,
    road: str,
    date: str,
    start: int,
    end: int | None,
    *,
    after_layout: Callable[[list[int]], None] | None = None,
) -> bytes:
    """Render a day/range CSV from one WAL snapshot.

    Only active partitions intersecting the requested mileage range are read.
    ``after_layout`` runs after the partition layout is queried but before the
    records are read; tests use it to switch a split mid-download.
    """

    if start < 0 or start > MAX_MILEAGE:
        raise ValidationError("起始里程必须是有效的非负整数")
    if end is not None and (end < start or end > MAX_MILEAGE):
        raise ValidationError("结束里程必须不小于起始里程")

    with read_transaction(conn):
        if end is None:
            rows = conn.execute(
                "SELECT id FROM partitions WHERE road = ? AND date = ? "
                "AND status = 'active' AND (hi IS NULL OR hi > ?) "
                "ORDER BY lo, id",
                (road, date, start),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id FROM partitions WHERE road = ? AND date = ? "
                "AND status = 'active' AND lo <= ? AND (hi IS NULL OR hi > ?) "
                "ORDER BY lo, id",
                (road, date, end, start),
            ).fetchall()

        partition_ids = [row["id"] for row in rows]
        if after_layout is not None:
            after_layout(partition_ids)

        records: list[sqlite3.Row] = []
        if partition_ids:
            placeholders = ",".join("?" for _ in partition_ids)
            if end is None:
                records = conn.execute(
                    "SELECT record_no, mileage, grade FROM records "
                    f"WHERE partition_id IN ({placeholders}) AND mileage >= ? "
                    "ORDER BY mileage, record_no, id",
                    (*partition_ids, start),
                ).fetchall()
            else:
                records = conn.execute(
                    "SELECT record_no, mileage, grade FROM records "
                    f"WHERE partition_id IN ({placeholders}) "
                    "AND mileage >= ? AND mileage <= ? "
                    "ORDER BY mileage, record_no, id",
                    (*partition_ids, start, end),
                ).fetchall()

        return render_records_csv(
            (row["record_no"], row["mileage"], row["grade"]) for row in records
        )


def day_overview(conn: sqlite3.Connection, road: str, date: str) -> DayOverview:
    with read_transaction(conn):
        partitions = conn.execute(
            "SELECT p.id, p.road, p.date, p.lo, p.hi, p.status, p.split_state, "
            "COUNT(r.id) AS record_count "
            "FROM partitions p LEFT JOIN records r ON r.partition_id = p.id "
            "WHERE p.road = ? AND p.date = ? "
            "GROUP BY p.id ORDER BY p.status, p.lo, p.id",
            (road, date),
        ).fetchall()
        jobs = conn.execute(
            "SELECT j.id, j.source_partition_id, j.cut, j.status, j.error, "
            "j.copied_records, j.created_at, j.finished_at "
            "FROM split_jobs j JOIN partitions p ON p.id = j.source_partition_id "
            "WHERE p.road = ? AND p.date = ? "
            "ORDER BY j.id DESC LIMIT 10",
            (road, date),
        ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) FROM records WHERE road = ? AND date = ?",
            (road, date),
        ).fetchone()[0]

    return DayOverview(
        road=road,
        date=date,
        partitions=[
            PartitionInfo(
                id=row["id"],
                road=row["road"],
                date=row["date"],
                lo=row["lo"],
                hi=row["hi"],
                status=row["status"],
                split_state=row["split_state"],
                record_count=row["record_count"],
            )
            for row in partitions
        ],
        record_count=total,
        split_jobs=[
            SplitJobInfo(
                id=row["id"],
                source_partition_id=row["source_partition_id"],
                cut=row["cut"],
                status=row["status"],
                error=row["error"],
                copied_records=row["copied_records"],
                created_at=row["created_at"],
                finished_at=row["finished_at"],
            )
            for row in jobs
        ],
    )


def list_roads(conn: sqlite3.Connection, limit: int = 50) -> list[str]:
    with read_transaction(conn):
        rows = conn.execute(
            "SELECT road, MAX(created_at) AS latest FROM partitions "
            "GROUP BY road ORDER BY latest DESC, road LIMIT ?",
            (limit,),
        ).fetchall()
    return [row["road"] for row in rows]


def range_label(lo: int, hi: int | None) -> str:
    if hi is None:
        return f"[{lo}, +∞)"
    return f"[{lo}, {hi})"
