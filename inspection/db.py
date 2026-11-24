"""SQLite schema, connections, and crash recovery for the inspection store."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

try:  # POSIX only; the container runs Linux.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None


class SplitLockBusy(Exception):
    """Another process/thread currently owns the split lock."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS partitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    road TEXT NOT NULL,
    date TEXT NOT NULL,
    lo INTEGER NOT NULL CHECK (lo >= 0),
    hi INTEGER,
    status TEXT NOT NULL CHECK (status IN ('active', 'shadow', 'retired')),
    parent_id INTEGER REFERENCES partitions(id),
    split_state TEXT CHECK (split_state IS NULL OR split_state = 'copying'),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK (hi IS NULL OR hi > lo)
);

CREATE INDEX IF NOT EXISTS idx_partitions_day_status
    ON partitions (road, date, status);
CREATE INDEX IF NOT EXISTS idx_partitions_parent
    ON partitions (parent_id);

-- At most one split may be in progress for a road/day.  A split only moves
-- records within one day, so serializing split attempts per day keeps the
-- switch transaction small and deterministic.
CREATE UNIQUE INDEX IF NOT EXISTS idx_partitions_one_copying
    ON partitions (road, date) WHERE split_state = 'copying';

CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    road TEXT NOT NULL,
    date TEXT NOT NULL,
    partition_id INTEGER NOT NULL REFERENCES partitions(id) ON DELETE CASCADE,
    record_no TEXT NOT NULL,
    mileage INTEGER NOT NULL CHECK (mileage >= 0),
    grade TEXT NOT NULL,
    UNIQUE (road, date, record_no)
);

CREATE INDEX IF NOT EXISTS idx_records_partition_mileage
    ON records (partition_id, mileage, record_no);
CREATE INDEX IF NOT EXISTS idx_records_day_mileage
    ON records (road, date, mileage, record_no);

-- Enforce that a stored record belongs to a partition of the same road/day
-- and that its mileage lies in the partition's [lo, hi) range.
CREATE TRIGGER IF NOT EXISTS trg_records_partition_scope
BEFORE INSERT ON records
FOR EACH ROW
BEGIN
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM partitions p
            WHERE p.id = NEW.partition_id
              AND p.road = NEW.road
              AND p.date = NEW.date
              AND NEW.mileage >= p.lo
              AND (p.hi IS NULL OR NEW.mileage < p.hi)
        )
        THEN RAISE(ABORT, 'record does not fit partition range')
    END;
END;

CREATE TABLE IF NOT EXISTS split_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_partition_id INTEGER NOT NULL REFERENCES partitions(id) ON DELETE CASCADE,
    cut INTEGER NOT NULL,
    left_partition_id INTEGER REFERENCES partitions(id) ON DELETE SET NULL,
    right_partition_id INTEGER REFERENCES partitions(id) ON DELETE SET NULL,
    status TEXT NOT NULL CHECK (status IN ('copying', 'switched', 'failed')),
    error TEXT,
    copied_records INTEGER NOT NULL DEFAULT 0,
    -- Fixed at split start: highest records.id of the source partition that
    -- belongs to the split snapshot.  The bounded copy only reads rows with
    -- id <= copy_upto_id, so continuously arriving supplements cannot make
    -- the copy chase an ever-moving tail.  Late rows are staged separately
    -- before the switch transaction.
    copy_upto_id INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_split_jobs_status
    ON split_jobs (status);
CREATE INDEX IF NOT EXISTS idx_split_jobs_source
    ON split_jobs (source_partition_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_split_jobs_one_copying
    ON split_jobs (source_partition_id) WHERE status = 'copying';

-- Rows copied out of the source partition while the split is in progress.
-- The source partition stays authoritative and visible until the atomic
-- switch, so a failure before the switch can always be rolled back.
CREATE TABLE IF NOT EXISTS split_staging (
    job_id INTEGER NOT NULL REFERENCES split_jobs(id) ON DELETE CASCADE,
    record_id INTEGER NOT NULL,
    record_no TEXT NOT NULL,
    mileage INTEGER NOT NULL,
    grade TEXT NOT NULL,
    PRIMARY KEY (job_id, record_id)
);
"""


def configure_connection(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")


@contextmanager
def connect(db_path: str) -> Iterator[sqlite3.Connection]:
    """Open a short-lived connection.

    One connection per operation/request keeps threads independent.  WAL mode
    (enabled once at database creation) lets readers hold a stable snapshot
    while a writer commits the split switch.
    """

    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    configure_connection(conn)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """A serialized write transaction.  Must not be nested."""

    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def read_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """An explicit read transaction with a stable WAL snapshot.

    The first SELECT fixes the snapshot.  A download therefore keeps using the
    partition layout and record placement that existed when it started, even
    if another connection commits a split switch while it is reading.
    """

    conn.execute("BEGIN")
    try:
        yield
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def split_lock(db_path: str) -> Iterator[None]:
    """A process-wide exclusive lock for split copy phases.

    Only one split may copy at a time; more importantly, startup recovery must
    not mistake a live process's in-progress copy for an interrupted one.  The
    lock is an ``flock`` on a sidecar file, released automatically if the
    process dies, so no stale lock can block recovery.
    """

    if fcntl is None:  # pragma: no cover
        yield
        return

    lock_path = os.path.abspath(db_path) + ".split.lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, "a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SplitLockBusy("已有拆分任务在进行") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _recover_interrupted_splits(conn: sqlite3.Connection) -> None:
    """Discard an unfinished pre-switch split and keep the source complete.

    Shadow partitions are never read by the application and the switch is one
    transaction.  If the process died during the copy phase, the source
    partition still holds every accepted record; dropping the partial copy
    makes the split retryable without data loss.
    """

    with write_transaction(conn):
        jobs = conn.execute(
            "SELECT id, source_partition_id, left_partition_id, right_partition_id "
            "FROM split_jobs WHERE status = 'copying'"
        ).fetchall()

        for job in jobs:
            child_ids = [
                pid
                for pid in (job["left_partition_id"], job["right_partition_id"])
                if pid is not None
            ]
            if child_ids:
                placeholders = ",".join("?" for _ in child_ids)
                # Defensive only: the switch inserts into children in the same
                # transaction that flips the job away from 'copying'.
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
                "DELETE FROM split_staging WHERE job_id = ?", (job["id"],)
            )
            conn.execute(
                "UPDATE partitions SET split_state = NULL "
                "WHERE id = ? AND split_state = 'copying'",
                (job["source_partition_id"],),
            )
            conn.execute(
                "UPDATE split_jobs SET status = 'failed', "
                "error = 'interrupted before switch', "
                "finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                "WHERE id = ?",
                (job["id"],),
            )

        # Defensive cleanup: a marker without a live job must not block future
        # splits, and terminal jobs must not leave staged rows behind.
        conn.execute(
            "UPDATE partitions SET split_state = NULL "
            "WHERE split_state = 'copying' AND id NOT IN "
            "(SELECT source_partition_id FROM split_jobs WHERE status = 'copying')"
        )
        conn.execute(
            "DELETE FROM split_staging WHERE job_id IN "
            "(SELECT id FROM split_jobs WHERE status != 'copying')"
        )


def recover_interrupted_splits(db_path: str) -> None:
    """Public wrapper used by the split path.

    The caller must hold :func:`split_lock`: then no live process is copying,
    so every ``copying`` job is stale and can be rolled back safely.
    """

    with connect(db_path) as conn:
        _recover_interrupted_splits(conn)


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the first release to pre-existing DBs.

    ``CREATE TABLE IF NOT EXISTS`` does not alter an old ``split_jobs`` table,
    so the split high-water-mark column is added explicitly when missing.
    """

    job_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(split_jobs)")
    }
    if "copy_upto_id" not in job_columns:
        # A constant DEFAULT lets SQLite add the NOT NULL column without a
        # table rebuild; existing jobs simply read as bound 0.
        conn.execute(
            "ALTER TABLE split_jobs "
            "ADD COLUMN copy_upto_id INTEGER NOT NULL DEFAULT 0"
        )


def init_db(db_path: str) -> None:
    directory = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(directory, exist_ok=True)

    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    try:
        configure_connection(conn)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(SCHEMA)
        _migrate_schema(conn)
        # Only recover when no live process is mid-copy.  While it holds the
        # split lock, its in-progress job is not "interrupted".
        try:
            with split_lock(db_path):
                _recover_interrupted_splits(conn)
        except SplitLockBusy:
            pass
    finally:
        conn.close()
