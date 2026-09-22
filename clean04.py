#!/usr/bin/env python3
"""第四步清洗：按完整合约键排序，并在合约组内审计时间序列。"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import tempfile
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = "config.json"
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S%z"


def read_config(config_path: Path) -> dict[str, Any]:
    try:
        with config_path.open("r", encoding="utf-8") as config_file:
            config = json.load(config_file)
    except FileNotFoundError as error:
        raise SystemExit(f"未找到配置文件：{config_path}") from error
    except json.JSONDecodeError as error:
        raise SystemExit(f"配置文件不是有效 JSON：{error}") from error

    clean04_config = config.get("clean04")
    if not isinstance(clean04_config, dict):
        raise SystemExit("配置文件缺少 clean04 配置区段。")
    required_keys = ("default_input_path", "default_output_dir", "group_columns")
    missing_keys = [key for key in required_keys if key not in clean04_config]
    if missing_keys:
        raise SystemExit("clean04 配置缺少：" + ", ".join(missing_keys))
    if not isinstance(clean04_config["group_columns"], list):
        raise SystemExit("clean04.group_columns 必须是列表。")
    return config


def resolve_config_path(value: str, config_path: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else config_path.parent / path


def resolve_cli_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def quote_identifier(identifier: str) -> str:
    return f'"{identifier.replace("\"", "\"\"")}"'


def is_empty(value: str | None) -> bool:
    return value is None or value.strip() == ""


def parse_ts_utc(value: str | None, row_number: int) -> datetime:
    if is_empty(value):
        raise SystemExit(f"第 {row_number:,} 行的 ts_utc 为空。")
    try:
        timestamp = datetime.strptime(value, TIMESTAMP_FORMAT)
    except ValueError as error:
        raise SystemExit(f"第 {row_number:,} 行的 ts_utc 格式无效：{value}") from error
    if timestamp.utcoffset() is None or timestamp.utcoffset().total_seconds() != 0:
        raise SystemExit(f"第 {row_number:,} 行的 ts_utc 不是 UTC：{value}")
    return timestamp


def default_output_path(input_path: Path, config: dict[str, Any], config_path: Path) -> Path:
    output_dir = resolve_config_path(config["default_output_dir"], config_path)
    return output_dir / f"{input_path.stem}_clean04.csv"


def audit_output_path(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.stem}_group_audit{output_path.suffix}")


def insert_rows(
    connection: sqlite3.Connection,
    input_path: Path,
    encoding: str,
    fieldnames: list[str],
    group_columns: list[str],
) -> int:
    quoted_columns = ", ".join(quote_identifier(field) for field in fieldnames)
    placeholders = ", ".join("?" for _ in range(len(fieldnames) + 1))
    insert_sql = (
        f"INSERT INTO records ({quoted_columns}, _source_row) "
        f"VALUES ({placeholders})"
    )
    batch: list[list[str | None | int]] = []
    total_rows = 0

    with input_path.open("r", encoding=encoding, newline="") as source:
        reader = csv.DictReader(source)
        for row_number, row in enumerate(reader, start=2):
            total_rows += 1
            if None in row:
                raise SystemExit(f"第 {row_number:,} 行的 CSV 列数超过表头定义。")
            missing_group_fields = [
                field for field in group_columns if is_empty(row.get(field))
            ]
            if missing_group_fields:
                raise SystemExit(
                    f"第 {row_number:,} 行的合约键为空：{', '.join(missing_group_fields)}"
                )
            parse_ts_utc(row.get("ts_utc"), row_number)
            batch.append([*(row.get(field) for field in fieldnames), row_number])

            if len(batch) >= 10_000:
                connection.executemany(insert_sql, batch)
                connection.commit()
                batch.clear()
            if total_rows % 1_000_000 == 0:
                print(f"已读取 {total_rows:,} 行…")

    if batch:
        connection.executemany(insert_sql, batch)
        connection.commit()
    return total_rows


def write_sorted_data_and_audit(
    connection: sqlite3.Connection,
    fieldnames: list[str],
    group_columns: list[str],
    output_path: Path,
    audit_path: Path,
    encoding: str,
) -> tuple[int, int, int]:
    quoted_columns = ", ".join(quote_identifier(field) for field in fieldnames)
    order_columns = ", ".join(
        [*(quote_identifier(field) for field in group_columns), quote_identifier("ts_utc"), "_source_row"]
    )
    query = f"SELECT {quoted_columns} FROM records ORDER BY {order_columns}"
    temporary_output = output_path.with_name(f".{output_path.name}.tmp")
    temporary_audit = audit_path.with_name(f".{audit_path.name}.tmp")
    output_rows = 0
    group_count = 0
    duplicate_timestamp_rows = 0
    missing_minutes = 0

    audit_fields = [
        *group_columns,
        "row_count",
        "first_ts_utc",
        "last_ts_utc",
        "duplicate_timestamp_rows",
        "gap_count",
        "missing_minutes",
    ]
    current_group: tuple[str, ...] | None = None
    current_count = 0
    current_first_timestamp = ""
    current_last_timestamp = ""
    current_previous_datetime: datetime | None = None
    current_duplicate_rows = 0
    current_gap_count = 0
    current_missing_minutes = 0

    def write_audit_row(writer: csv.DictWriter) -> None:
        nonlocal group_count, duplicate_timestamp_rows, missing_minutes
        if current_group is None:
            return
        writer.writerow(
            {
                **dict(zip(group_columns, current_group, strict=True)),
                "row_count": current_count,
                "first_ts_utc": current_first_timestamp,
                "last_ts_utc": current_last_timestamp,
                "duplicate_timestamp_rows": current_duplicate_rows,
                "gap_count": current_gap_count,
                "missing_minutes": current_missing_minutes,
            }
        )
        group_count += 1
        duplicate_timestamp_rows += current_duplicate_rows
        missing_minutes += current_missing_minutes

    try:
        with (
            temporary_output.open("w", encoding=encoding, newline="") as output_file,
            temporary_audit.open("w", encoding=encoding, newline="") as audit_file,
        ):
            output_writer = csv.DictWriter(output_file, fieldnames=fieldnames)
            audit_writer = csv.DictWriter(audit_file, fieldnames=audit_fields)
            output_writer.writeheader()
            audit_writer.writeheader()

            for record in connection.execute(query):
                row = dict(zip(fieldnames, record, strict=True))
                group = tuple(row[field] for field in group_columns)
                timestamp_text = row["ts_utc"]
                timestamp = datetime.strptime(timestamp_text, TIMESTAMP_FORMAT)

                if group != current_group:
                    write_audit_row(audit_writer)
                    current_group = group
                    current_count = 0
                    current_first_timestamp = timestamp_text
                    current_duplicate_rows = 0
                    current_gap_count = 0
                    current_missing_minutes = 0
                    current_previous_datetime = None

                if current_previous_datetime is not None:
                    seconds_between = (timestamp - current_previous_datetime).total_seconds()
                    if seconds_between == 0:
                        current_duplicate_rows += 1
                    elif seconds_between > 60:
                        current_gap_count += 1
                        current_missing_minutes += int(seconds_between // 60) - 1

                current_count += 1
                current_last_timestamp = timestamp_text
                current_previous_datetime = timestamp
                output_writer.writerow(row)
                output_rows += 1

            write_audit_row(audit_writer)

        os.replace(temporary_output, output_path)
        os.replace(temporary_audit, audit_path)
    except BaseException:
        temporary_output.unlink(missing_ok=True)
        temporary_audit.unlink(missing_ok=True)
        raise

    return output_rows, group_count, duplicate_timestamp_rows, missing_minutes


def clean_csv(config_path: Path, input_value: str | None, output_value: str | None) -> None:
    config = read_config(config_path)
    clean04_config = config["clean04"]
    group_columns = clean04_config["group_columns"]
    encoding = config.get("encoding", "utf-8")
    input_path = (
        resolve_cli_path(input_value)
        if input_value
        else resolve_config_path(clean04_config["default_input_path"], config_path)
    )
    output_path = (
        resolve_cli_path(output_value)
        if output_value
        else default_output_path(input_path, clean04_config, config_path)
    )
    audit_path = audit_output_path(output_path)

    if not input_path.is_file():
        raise SystemExit(f"未找到输入文件：{input_path}")
    if input_path.resolve() in {output_path.resolve(), audit_path.resolve()}:
        raise SystemExit("输入文件不能与输出文件或审计文件相同。")

    with input_path.open("r", encoding=encoding, newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None:
            raise SystemExit("输入 CSV 缺少表头。")
        fieldnames = reader.fieldnames
    required_columns = ["ts_utc", *group_columns]
    missing_columns = [column for column in required_columns if column not in fieldnames]
    if missing_columns:
        raise SystemExit("输入 CSV 缺少字段：" + ", ".join(missing_columns))
    if len(set(fieldnames)) != len(fieldnames):
        raise SystemExit("输入 CSV 表头包含重复字段名，无法安全排序。")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_database = tempfile.NamedTemporaryFile(
        prefix=".clean04_", suffix=".sqlite", dir=output_path.parent, delete=False
    )
    database_path = Path(temporary_database.name)
    temporary_database.close()

    try:
        with sqlite3.connect(database_path) as connection:
            column_definitions = ", ".join(
                f"{quote_identifier(field)} TEXT" for field in fieldnames
            )
            connection.execute(
                f"CREATE TABLE records ({column_definitions}, _source_row INTEGER NOT NULL)"
            )
            total_rows = insert_rows(
                connection, input_path, encoding, fieldnames, group_columns
            )
            index_columns = ", ".join(
                [*(quote_identifier(field) for field in group_columns), quote_identifier("ts_utc"), "_source_row"]
            )
            connection.execute(f"CREATE INDEX records_group_time ON records ({index_columns})")
            output_rows, group_count, duplicates, missing_minutes = write_sorted_data_and_audit(
                connection,
                fieldnames,
                group_columns,
                output_path,
                audit_path,
                encoding,
            )
    except (OSError, UnicodeError, csv.Error, sqlite3.Error) as error:
        raise SystemExit(f"清洗失败：{error}") from error
    finally:
        database_path.unlink(missing_ok=True)

    print("第四步合约分组清洗完成")
    print(f"输入文件：{input_path}")
    print(f"有效数据文件：{output_path}")
    print(f"分组合约审计文件：{audit_path}")
    print(f"数据行总数：{total_rows:,}")
    print(f"输出行数：{output_rows:,}")
    print(f"合约组数：{group_count:,}")
    print(f"组内重复分钟记录数：{duplicates:,}")
    print(f"组内分钟缺口数：{missing_minutes:,}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="按 publisher_id、instrument_id、symbol 对合约分别排序和审计。"
    )
    parser.add_argument(
        "-i", "--input", help="输入 CSV 路径；未提供时使用 config.json 的默认路径。"
    )
    parser.add_argument(
        "-o", "--output", help="输出 CSV 路径；未提供时自动生成 xxx_clean04.csv。"
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="配置文件路径。")
    args = parser.parse_args()
    clean_csv(Path(args.config).expanduser().resolve(), args.input, args.output)


if __name__ == "__main__":
    main()
