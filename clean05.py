#!/usr/bin/env python3
"""第五步清洗：隔离完全重复的 NQ 一分钟 OHLCV 记录。"""

from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = "config.json"
EXACT_DUPLICATE = "EXACT_DUPLICATE"
REQUIRED_VALUE_COLUMNS = ("open", "high", "low", "close", "volume")
GROUP_KEY_COLUMNS = ("publisher_id", "instrument_id", "symbol")


def read_config(config_path: Path) -> dict[str, Any]:
    try:
        with config_path.open("r", encoding="utf-8") as config_file:
            config = json.load(config_file)
    except FileNotFoundError as error:
        raise SystemExit(f"未找到配置文件：{config_path}") from error
    except json.JSONDecodeError as error:
        raise SystemExit(f"配置文件不是有效 JSON：{error}") from error

    clean05_config = config.get("clean05")
    if not isinstance(clean05_config, dict):
        raise SystemExit("配置文件缺少 clean05 配置区段。")
    required_keys = ("default_input_path", "default_output_dir", "timestamp_column")
    missing_keys = [key for key in required_keys if key not in clean05_config]
    if missing_keys:
        raise SystemExit("clean05 配置缺少：" + ", ".join(missing_keys))
    return config


def resolve_config_path(value: str, config_path: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else config_path.parent / path


def resolve_cli_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def default_output_path(input_path: Path, config: dict[str, Any], config_path: Path) -> Path:
    output_dir = resolve_config_path(config["default_output_dir"], config_path)
    return output_dir / f"{input_path.stem}_clean05.csv"


def duplicates_output_path(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.stem}_duplicates{output_path.suffix}")


def symbol_conflicts_output_path(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.stem}_symbol_conflicts{output_path.suffix}")


def is_empty(value: str | None) -> bool:
    return value is None or value.strip() == ""


def clean_csv(config_path: Path, input_value: str | None, output_value: str | None) -> None:
    config = read_config(config_path)
    clean05_config = config["clean05"]
    timestamp_column = clean05_config["timestamp_column"]
    encoding = config.get("encoding", "utf-8")
    input_path = (
        resolve_cli_path(input_value)
        if input_value
        else resolve_config_path(clean05_config["default_input_path"], config_path)
    )
    output_path = (
        resolve_cli_path(output_value)
        if output_value
        else default_output_path(input_path, clean05_config, config_path)
    )
    duplicates_path = duplicates_output_path(output_path)
    symbol_conflicts_path = symbol_conflicts_output_path(output_path)

    if not input_path.is_file():
        raise SystemExit(f"未找到输入文件：{input_path}")
    if input_path.resolve() in {
        output_path.resolve(),
        duplicates_path.resolve(),
        symbol_conflicts_path.resolve(),
    }:
        raise SystemExit("输入文件不能与输出文件、重复记录文件或一致性审计文件相同。")

    with input_path.open("r", encoding=encoding, newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None:
            raise SystemExit("输入 CSV 缺少表头。")
        fieldnames = reader.fieldnames
    required_columns = [*GROUP_KEY_COLUMNS, timestamp_column, *REQUIRED_VALUE_COLUMNS]
    missing_columns = [column for column in required_columns if column not in fieldnames]
    if missing_columns:
        raise SystemExit("输入 CSV 缺少字段：" + ", ".join(missing_columns))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = output_path.with_name(f".{output_path.name}.tmp")
    temporary_duplicates = duplicates_path.with_name(f".{duplicates_path.name}.tmp")
    temporary_conflicts = symbol_conflicts_path.with_name(
        f".{symbol_conflicts_path.name}.tmp"
    )
    total_rows = 0
    kept_rows = 0
    duplicate_rows = 0
    symbol_conflict_rows = 0
    previous_sort_key: tuple[str, str, str, str] | None = None
    current_bucket: tuple[str, str, str, str] | None = None
    seen_records: set[tuple[str, ...]] = set()
    instrument_symbols: dict[tuple[str, str], tuple[str, int]] = {}
    reported_conflicts: set[tuple[str, str, str, str]] = set()

    try:
        with (
            input_path.open("r", encoding=encoding, newline="") as source,
            temporary_output.open("w", encoding=encoding, newline="") as output_file,
            temporary_duplicates.open("w", encoding=encoding, newline="") as duplicates_file,
            temporary_conflicts.open("w", encoding=encoding, newline="") as conflicts_file,
        ):
            reader = csv.DictReader(source)
            output_writer = csv.DictWriter(output_file, fieldnames=fieldnames)
            duplicates_writer = csv.DictWriter(
                duplicates_file,
                fieldnames=[*fieldnames, "reject_reason"],
                extrasaction="ignore",
            )
            conflicts_writer = csv.DictWriter(
                conflicts_file,
                fieldnames=[
                    "publisher_id",
                    "instrument_id",
                    "first_symbol",
                    "conflicting_symbol",
                    "first_seen_row",
                    "conflicting_row",
                ],
            )
            output_writer.writeheader()
            duplicates_writer.writeheader()
            conflicts_writer.writeheader()

            for row_number, row in enumerate(reader, start=2):
                total_rows += 1
                if None in row:
                    raise SystemExit(f"第 {row_number:,} 行的 CSV 列数超过表头定义。")
                if any(is_empty(row.get(column)) for column in required_columns):
                    raise SystemExit(f"第 {row_number:,} 行包含空的去重字段。")

                publisher_id = row["publisher_id"]
                instrument_id = row["instrument_id"]
                symbol = row["symbol"]
                timestamp = row[timestamp_column]
                sort_key = (publisher_id, instrument_id, symbol, timestamp)
                if previous_sort_key is not None and sort_key < previous_sort_key:
                    raise SystemExit(
                        f"第 {row_number:,} 行未按合约键和 {timestamp_column} 排序；"
                        "请先运行 clean04.py。"
                    )
                previous_sort_key = sort_key

                instrument_key = (publisher_id, instrument_id)
                first_symbol = instrument_symbols.get(instrument_key)
                if first_symbol is None:
                    instrument_symbols[instrument_key] = (symbol, row_number)
                elif first_symbol[0] != symbol:
                    conflict = (publisher_id, instrument_id, first_symbol[0], symbol)
                    if conflict not in reported_conflicts:
                        conflicts_writer.writerow(
                            {
                                "publisher_id": publisher_id,
                                "instrument_id": instrument_id,
                                "first_symbol": first_symbol[0],
                                "conflicting_symbol": symbol,
                                "first_seen_row": first_symbol[1],
                                "conflicting_row": row_number,
                            }
                        )
                        reported_conflicts.add(conflict)
                    symbol_conflict_rows += 1

                bucket = (publisher_id, instrument_id, symbol, timestamp)
                if bucket != current_bucket:
                    current_bucket = bucket
                    seen_records.clear()
                exact_record = tuple(row[column] for column in required_columns)
                if exact_record in seen_records:
                    row["reject_reason"] = EXACT_DUPLICATE
                    duplicates_writer.writerow(row)
                    duplicate_rows += 1
                    continue

                seen_records.add(exact_record)
                output_writer.writerow(row)
                kept_rows += 1

                if total_rows % 1_000_000 == 0:
                    print(f"已处理 {total_rows:,} 行…")

        os.replace(temporary_output, output_path)
        os.replace(temporary_duplicates, duplicates_path)
        os.replace(temporary_conflicts, symbol_conflicts_path)
    except (OSError, UnicodeError, csv.Error) as error:
        temporary_output.unlink(missing_ok=True)
        temporary_duplicates.unlink(missing_ok=True)
        temporary_conflicts.unlink(missing_ok=True)
        raise SystemExit(f"清洗失败：{error}") from error
    except BaseException:
        temporary_output.unlink(missing_ok=True)
        temporary_duplicates.unlink(missing_ok=True)
        temporary_conflicts.unlink(missing_ok=True)
        raise

    print("第五步完全重复清洗完成")
    print(f"输入文件：{input_path}")
    print(f"有效数据文件：{output_path}")
    print(f"重复记录文件：{duplicates_path}")
    print(f"合约代码一致性审计文件：{symbol_conflicts_path}")
    print(f"数据行总数：{total_rows:,}")
    print(f"保留行数：{kept_rows:,}")
    print(f"完全重复行数：{duplicate_rows:,}")
    print(f"代码不一致记录数：{symbol_conflict_rows:,}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="隔离完全重复的 NQ 一分钟 OHLCV 记录。"
    )
    parser.add_argument(
        "-i", "--input", help="输入 CSV 路径；未提供时使用 config.json 的默认路径。"
    )
    parser.add_argument(
        "-o", "--output", help="输出 CSV 路径；未提供时自动生成 xxx_clean05.csv。"
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="配置文件路径。")
    args = parser.parse_args()
    clean_csv(Path(args.config).expanduser().resolve(), args.input, args.output)


if __name__ == "__main__":
    main()
