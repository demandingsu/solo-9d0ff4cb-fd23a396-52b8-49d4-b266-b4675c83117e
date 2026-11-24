"""CSV parsing and rendering for inspection records."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from typing import Iterable, Sequence


class CsvFormatError(ValueError):
    """Raised when the uploaded CSV is structurally unusable."""

    def __init__(self, messages: Sequence[str]):
        self.messages = list(messages)
        super().__init__("; ".join(self.messages))


@dataclass(frozen=True)
class ParsedRecord:
    record_no: str
    mileage: int
    grade: str
    line: int = 0


RECORD_NO_ALIASES = {
    "记录号",
    "记录编号",
    "编号",
    "record_no",
    "recordno",
    "record",
    "record_id",
    "id",
}
MILEAGE_ALIASES = {
    "整米里程",
    "里程",
    "桩号",
    "mileage",
    "meterage",
    "meters",
    "m",
}
GRADE_ALIASES = {
    "病害等级",
    "等级",
    "病害级别",
    "grade",
    "level",
    "severity",
}

FIELD_NAMES = ("record_no", "mileage", "grade")

OUTPUT_HEADER = ["记录号", "整米里程", "病害等级"]

# Digits only: int() would also accept "+5", "1_000" and surrounding
# whitespace variants, which are not valid mileages in these files.
MILEAGE_PATTERN = re.compile(r"[0-9]+")
MAX_MILEAGE = 9_223_372_036_854_775_807


def _decode(content: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise CsvFormatError(["无法识别文件编码，请使用 UTF-8 或 GB18030 编码的 CSV"])


def _detect_delimiter(text: str) -> str:
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        return dialect.delimiter
    except csv.Error:
        return ","


def _header_map(first_row: Sequence[str]) -> dict[str, int] | None:
    normalized = [cell.strip().lower() for cell in first_row]
    mapping: dict[str, int] = {}
    for index, cell in enumerate(normalized):
        if cell in RECORD_NO_ALIASES and "record_no" not in mapping:
            mapping["record_no"] = index
        elif cell in MILEAGE_ALIASES and "mileage" not in mapping:
            mapping["mileage"] = index
        elif cell in GRADE_ALIASES and "grade" not in mapping:
            mapping["grade"] = index
    if not mapping:
        return None
    missing = [name for name in FIELD_NAMES if name not in mapping]
    if missing:
        readable = {"record_no": "记录号", "mileage": "整米里程", "grade": "病害等级"}
        raise CsvFormatError(
            ["表头缺少列: " + "、".join(readable[name] for name in missing)]
        )
    return mapping


def parse_records(content: bytes) -> list[ParsedRecord]:
    """Parse a single-road inspection CSV.

    Accepted header names are Chinese or English.  A headerless file is also
    accepted with the three columns in the order 记录号, 整米里程, 病害等级.
    Structural problems reject the whole file; duplicate/conflict handling is
    done later inside the database transaction.
    """

    text = _decode(content)
    if not text.strip():
        raise CsvFormatError(["CSV 文件为空"])

    delimiter = _detect_delimiter(text)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    # Keep the physical record number (1-based) so error messages match the
    # line a user sees when opening the file in a spreadsheet.
    numbered_rows: list[tuple[int, list[str]]] = []
    for row in reader:
        if any(cell.strip() for cell in row):
            numbered_rows.append((reader.line_num, row))
    if not numbered_rows:
        raise CsvFormatError(["CSV 文件没有数据行"])

    mapping = _header_map(numbered_rows[0][1])
    if mapping is not None:
        data_start = 1  # index in numbered_rows
    else:
        mapping = {"record_no": 0, "mileage": 1, "grade": 2}
        data_start = 0
        first = numbered_rows[0][1]
        if len(first) > 1 and not MILEAGE_PATTERN.fullmatch(first[1].strip()):
            raise CsvFormatError(
                [
                    "无法识别表头：第一行不是数据行，但表头缺少"
                    "「记录号、整米里程、病害等级」中的列",
                    "请检查列名，或去掉表头后按 记录号,整米里程,病害等级 顺序提供",
                ]
            )

    if len(numbered_rows) <= data_start:
        raise CsvFormatError(["CSV 只有表头，没有数据行"])

    parsed: list[ParsedRecord] = []
    errors: list[str] = []
    needed = max(mapping.values()) + 1

    for row_index in range(data_start, len(numbered_rows)):
        display_line, row = numbered_rows[row_index]
        if len(row) < needed:
            errors.append(f"第 {display_line} 行: 列数不足（需要至少 {needed} 列）")
            continue
        record_no = row[mapping["record_no"]].strip()
        mileage_text = row[mapping["mileage"]].strip()
        grade = row[mapping["grade"]].strip()
        if not record_no:
            errors.append(f"第 {display_line} 行: 记录号为空")
            continue
        if not mileage_text:
            errors.append(f"第 {display_line} 行: 整米里程为空")
            continue
        if not MILEAGE_PATTERN.fullmatch(mileage_text):
            errors.append(
                f"第 {display_line} 行: 整米里程必须是非负整数"
                f"（当前为 {mileage_text!r}）"
            )
            continue
        mileage = int(mileage_text, 10)
        if mileage > MAX_MILEAGE:
            errors.append(f"第 {display_line} 行: 整米里程超出可存储范围")
            continue
        if not grade:
            errors.append(f"第 {display_line} 行: 病害等级为空")
            continue
        parsed.append(
            ParsedRecord(
                record_no=record_no,
                mileage=mileage,
                grade=grade,
                line=display_line,
            )
        )

    if errors:
        # Cap the number of messages returned to keep the page usable.
        if len(errors) > 20:
            extra = len(errors) - 20
            errors = errors[:20] + [f"……另有 {extra} 行错误未显示"]
        raise CsvFormatError(errors)
    if not parsed:
        raise CsvFormatError(["CSV 没有有效数据行"])

    return parsed


def render_records_csv(rows: Iterable[Sequence[object]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(OUTPUT_HEADER)
    for row in rows:
        writer.writerow([row[0], row[1], row[2]])
    # UTF-8 BOM makes Excel open Chinese CSVs correctly.
    return buffer.getvalue().encode("utf-8-sig")
