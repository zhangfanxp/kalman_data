#!/usr/bin/env python3
"""比较两个 CSV 文件中的完整记录（不要求记录顺序相同）。

用法：
    python3 compare.py 路径1/aaa.csv 路径2/bbb.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path


EXPECTED_COLUMNS = [
    "ts_utc", "rtype", "publisher_id", "instrument_id", "open", "high",
    "low", "close", "volume", "symbol", "zero_volume_flag",
]


def row_key(row: list[str]) -> str:
    """生成完整记录的稳定键；CSV 中的字段顺序和值都会参与比较。"""
    return json.dumps(row, ensure_ascii=False, separators=(",", ":"))


def open_csv(path: Path):
    # utf-8-sig 可同时处理普通 UTF-8 和带 BOM 的 UTF-8 文件。
    return path.open("r", encoding="utf-8-sig", newline="")


def read_header(reader: csv.reader, path: Path) -> list[str]:
    try:
        header = next(reader)
    except StopIteration:
        raise ValueError(f"文件为空：{path}")
    return header


def add_first_file(conn: sqlite3.Connection, path: Path) -> tuple[list[str], int]:
    total = 0
    with open_csv(path) as file:
        reader = csv.reader(file)
        header = read_header(reader, path)
        for line_number, row in enumerate(reader, start=2):
            if len(row) != len(header):
                raise ValueError(
                    f"{path} 第 {line_number} 行有 {len(row)} 列，应为 {len(header)} 列"
                )
            conn.execute(
                """
                INSERT INTO records (row_key, remaining) VALUES (?, 1)
                ON CONFLICT(row_key) DO UPDATE SET remaining = remaining + 1
                """,
                (row_key(row),),
            )
            total += 1
            if total % 10_000 == 0:
                conn.commit()
    conn.commit()
    return header, total


def compare_second_file(
    conn: sqlite3.Connection, path: Path, expected_header: list[str]
) -> tuple[int, int]:
    total = common = 0
    with open_csv(path) as file:
        reader = csv.reader(file)
        header = read_header(reader, path)
        if header != expected_header:
            raise ValueError("两个文件的表头不一致，无法按相同字段比较。")

        for line_number, row in enumerate(reader, start=2):
            if len(row) != len(header):
                raise ValueError(
                    f"{path} 第 {line_number} 行有 {len(row)} 列，应为 {len(header)} 列"
                )
            key = row_key(row)
            result = conn.execute(
                "SELECT remaining FROM records WHERE row_key = ?", (key,)
            ).fetchone()
            if result and result[0] > 0:
                conn.execute(
                    "UPDATE records SET remaining = remaining - 1 WHERE row_key = ?",
                    (key,),
                )
                common += 1
            total += 1
            if total % 10_000 == 0:
                conn.commit()
    conn.commit()
    return total, common


def main() -> int:
    parser = argparse.ArgumentParser(
        description="按完整记录比较两个 CSV 文件；记录顺序不会影响结果。"
    )
    parser.add_argument("file1", type=Path, help="第一个 CSV 文件")
    parser.add_argument("file2", type=Path, help="第二个 CSV 文件")
    args = parser.parse_args()

    for path in (args.file1, args.file2):
        if not path.is_file():
            print(f"错误：找不到文件：{path}", file=sys.stderr)
            return 2

    db_fd, db_path = tempfile.mkstemp(prefix="csv_compare_", suffix=".sqlite3")
    os.close(db_fd)
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute("PRAGMA journal_mode = OFF")
            conn.execute("PRAGMA synchronous = OFF")
            conn.execute(
                "CREATE TABLE records (row_key TEXT PRIMARY KEY, remaining INTEGER NOT NULL)"
            )
            header, total1 = add_first_file(conn, args.file1)
            if header != EXPECTED_COLUMNS:
                print("提示：文件表头与预期字段不同，仍将按实际表头和完整记录比较。")
            total2, common = compare_second_file(conn, args.file2, header)
            only_file1 = conn.execute(
                "SELECT COALESCE(SUM(remaining), 0) FROM records"
            ).fetchone()[0]
            only_file2 = total2 - common

        print("比较完成（按完整记录比较，记录顺序不影响结果）")
        print(f"文件 1 总记录数：{total1}")
        print(f"文件 2 总记录数：{total2}")
        print(f"相同记录数：{common}")
        print(f"仅存在于文件 1 的记录数：{only_file1}")
        print(f"仅存在于文件 2 的记录数：{only_file2}")
        print(f"不一致记录数（两侧合计）：{only_file1 + only_file2}")
        return 0
    except (OSError, UnicodeError, csv.Error, ValueError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1
    finally:
        try:
            os.remove(db_path)
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
