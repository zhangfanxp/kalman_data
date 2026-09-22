#!/usr/bin/env python3
"""第六步清洗：隔离同一合约分钟内 OHLCV 不一致的冲突记录。"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = "config.json"
CONFLICT_DUPLICATE = "CONFLICT_DUPLICATE"
KEY_COLUMNS = ("publisher_id", "instrument_id")
OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")


def read_config(config_path: Path) -> dict[str, Any]:
    try:
        with config_path.open("r", encoding="utf-8") as config_file:
            config = json.load(config_file)
    except FileNotFoundError as error:
        raise SystemExit(f"未找到配置文件：{config_path}") from error
    except json.JSONDecodeError as error:
        raise SystemExit(f"配置文件不是有效 JSON：{error}") from error

    clean06_config = config.get("clean06")
    if not isinstance(clean06_config, dict):
        raise SystemExit("配置文件缺少 clean06 配置区段。")
    required_keys = ("default_input_path", "default_output_dir", "timestamp_column")
    missing_keys = [key for key in required_keys if key not in clean06_config]
    if missing_keys:
        raise SystemExit("clean06 配置缺少：" + ", ".join(missing_keys))
    return config


def resolve_config_path(value: str, config_path: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else config_path.parent / path


def resolve_cli_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def default_output_path(input_path: Path, config: dict[str, Any], config_path: Path) -> Path:
    output_dir = resolve_config_path(config["default_output_dir"], config_path)
    return output_dir / f"{input_path.stem}_clean06.csv"


def conflicts_output_path(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.stem}_conflicts{output_path.suffix}")


def create_database(
    database_path: Path,
    input_path: Path,
    encoding: str,
    fieldnames: list[str],
) -> int:
    quoted_columns = ", ".join(quote_identifier(column) for column in fieldnames)
    placeholders = ", ".join("?" for _ in range(len(fieldnames) + 1))
    insert_sql = (
        f"INSERT INTO records ({quoted_columns}, _source_row) VALUES ({placeholders})"
    )
    total_rows = 0

    with sqlite3.connect(database_path) as connection:
        column_definitions = ", ".join(
            f"{quote_identifier(column)} TEXT" for column in fieldnames
        )
        connection.execute(
            f"CREATE TABLE records ({column_definitions}, _source_row INTEGER PRIMARY KEY)"
        )
        batch: list[list[str | None | int]] = []
        with input_path.open("r", encoding=encoding, newline="") as source:
            reader = csv.DictReader(source)
            for row_number, row in enumerate(reader, start=2):
                total_rows += 1
                if None in row:
                    raise SystemExit(f"第 {row_number:,} 行的 CSV 列数超过表头定义。")
                batch.append([*(row.get(column) for column in fieldnames), row_number])
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


def create_conflict_keys(
    connection: sqlite3.Connection, timestamp_column: str
) -> int:
    group_columns = [*KEY_COLUMNS, timestamp_column]
    quoted_group_columns = ", ".join(quote_identifier(column) for column in group_columns)
    ohlcv_difference = " OR ".join(
        f"MIN({quote_identifier(column)}) <> MAX({quote_identifier(column)})"
        for column in OHLCV_COLUMNS
    )
    connection.execute(
        f"CREATE TABLE conflict_keys AS "
        f"SELECT {quoted_group_columns} FROM records "
        f"GROUP BY {quoted_group_columns} "
        f"HAVING COUNT(*) > 1 AND ({ohlcv_difference})"
    )
    connection.execute(
        f"CREATE INDEX conflict_keys_lookup ON conflict_keys ({quoted_group_columns})"
    )
    return connection.execute("SELECT COUNT(*) FROM conflict_keys").fetchone()[0]


def write_outputs(
    database_path: Path,
    fieldnames: list[str],
    timestamp_column: str,
    output_path: Path,
    conflicts_path: Path,
    encoding: str,
) -> tuple[int, int]:
    temporary_output = output_path.with_name(f".{output_path.name}.tmp")
    temporary_conflicts = conflicts_path.with_name(f".{conflicts_path.name}.tmp")
    quoted_columns = ", ".join(f"r.{quote_identifier(column)}" for column in fieldnames)
    join_conditions = " AND ".join(
        f"r.{quote_identifier(column)} = c.{quote_identifier(column)}"
        for column in (*KEY_COLUMNS, timestamp_column)
    )
    query = (
        f"SELECT {quoted_columns}, "
        f"CASE WHEN c.{quote_identifier(timestamp_column)} IS NULL THEN 0 ELSE 1 END "
        f"FROM records r LEFT JOIN conflict_keys c ON {join_conditions} "
        f"ORDER BY r._source_row"
    )
    kept_rows = 0
    conflict_rows = 0

    try:
        with (
            sqlite3.connect(database_path) as connection,
            temporary_output.open("w", encoding=encoding, newline="") as output_file,
            temporary_conflicts.open("w", encoding=encoding, newline="") as conflicts_file,
        ):
            output_writer = csv.DictWriter(output_file, fieldnames=fieldnames)
            conflicts_writer = csv.DictWriter(
                conflicts_file,
                fieldnames=[*fieldnames, "reject_reason"],
                extrasaction="ignore",
            )
            output_writer.writeheader()
            conflicts_writer.writeheader()

            for record in connection.execute(query):
                values, is_conflict = record[:-1], record[-1]
                row = dict(zip(fieldnames, values, strict=True))
                if is_conflict:
                    row["reject_reason"] = CONFLICT_DUPLICATE
                    conflicts_writer.writerow(row)
                    conflict_rows += 1
                else:
                    output_writer.writerow(row)
                    kept_rows += 1

        os.replace(temporary_output, output_path)
        os.replace(temporary_conflicts, conflicts_path)
    except BaseException:
        temporary_output.unlink(missing_ok=True)
        temporary_conflicts.unlink(missing_ok=True)
        raise

    return kept_rows, conflict_rows


def clean_csv(config_path: Path, input_value: str | None, output_value: str | None) -> None:
    config = read_config(config_path)
    clean06_config = config["clean06"]
    timestamp_column = clean06_config["timestamp_column"]
    encoding = config.get("encoding", "utf-8")
    input_path = (
        resolve_cli_path(input_value)
        if input_value
        else resolve_config_path(clean06_config["default_input_path"], config_path)
    )
    output_path = (
        resolve_cli_path(output_value)
        if output_value
        else default_output_path(input_path, clean06_config, config_path)
    )
    conflicts_path = conflicts_output_path(output_path)

    if not input_path.is_file():
        raise SystemExit(f"未找到输入文件：{input_path}")
    if input_path.resolve() in {output_path.resolve(), conflicts_path.resolve()}:
        raise SystemExit("输入文件不能与输出文件或冲突记录文件相同。")

    with input_path.open("r", encoding=encoding, newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None:
            raise SystemExit("输入 CSV 缺少表头。")
        fieldnames = reader.fieldnames
    required_columns = [*KEY_COLUMNS, timestamp_column, *OHLCV_COLUMNS]
    missing_columns = [column for column in required_columns if column not in fieldnames]
    if missing_columns:
        raise SystemExit("输入 CSV 缺少字段：" + ", ".join(missing_columns))
    if "_source_row" in fieldnames or len(set(fieldnames)) != len(fieldnames):
        raise SystemExit("输入 CSV 表头含保留字段或重复字段，无法安全处理。")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_database = tempfile.NamedTemporaryFile(
        prefix=".clean06_", suffix=".sqlite", dir=output_path.parent, delete=False
    )
    database_path = Path(temporary_database.name)
    temporary_database.close()

    try:
        total_rows = create_database(database_path, input_path, encoding, fieldnames)
        with sqlite3.connect(database_path) as connection:
            index_columns = ", ".join(
                quote_identifier(column) for column in (*KEY_COLUMNS, timestamp_column)
            )
            connection.execute(f"CREATE INDEX records_key ON records ({index_columns})")
            conflict_keys = create_conflict_keys(connection, timestamp_column)
            connection.commit()
        kept_rows, conflict_rows = write_outputs(
            database_path,
            fieldnames,
            timestamp_column,
            output_path,
            conflicts_path,
            encoding,
        )
    except (OSError, UnicodeError, csv.Error, sqlite3.Error) as error:
        raise SystemExit(f"清洗失败：{error}") from error
    finally:
        database_path.unlink(missing_ok=True)

    print("第六步冲突重复清洗完成")
    print(f"输入文件：{input_path}")
    print(f"有效数据文件：{output_path}")
    print(f"冲突记录文件：{conflicts_path}")
    print(f"数据行总数：{total_rows:,}")
    print(f"保留行数：{kept_rows:,}")
    print(f"冲突键数量：{conflict_keys:,}")
    print(f"冲突隔离行数：{conflict_rows:,}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="隔离同一合约分钟内 OHLCV 不一致的冲突重复记录。"
    )
    parser.add_argument(
        "-i", "--input", help="输入 CSV 路径；未提供时使用 config.json 的默认路径。"
    )
    parser.add_argument(
        "-o", "--output", help="输出 CSV 路径；未提供时自动生成 xxx_clean06.csv。"
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="配置文件路径。")
    args = parser.parse_args()
    clean_csv(Path(args.config).expanduser().resolve(), args.input, args.output)


if __name__ == "__main__":
    main()
