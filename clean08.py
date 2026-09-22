#!/usr/bin/env python3
"""第八步清洗：隔离不满足 OHLC 边界关系的记录。"""

from __future__ import annotations

import argparse
import csv
import json
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = "config.json"
OHLC_COLUMNS = ("open", "high", "low", "close")
INVALID_OHLC = "INVALID_OHLC"


def read_config(config_path: Path) -> dict[str, Any]:
    try:
        with config_path.open("r", encoding="utf-8") as config_file:
            config = json.load(config_file)
    except FileNotFoundError as error:
        raise SystemExit(f"未找到配置文件：{config_path}") from error
    except json.JSONDecodeError as error:
        raise SystemExit(f"配置文件不是有效 JSON：{error}") from error

    clean08_config = config.get("clean08")
    if not isinstance(clean08_config, dict):
        raise SystemExit("配置文件缺少 clean08 配置区段。")
    required_keys = ("default_input_path", "default_output_dir")
    missing_keys = [key for key in required_keys if key not in clean08_config]
    if missing_keys:
        raise SystemExit("clean08 配置缺少：" + ", ".join(missing_keys))
    return config


def resolve_config_path(value: str, config_path: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else config_path.parent / path


def resolve_cli_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def default_output_path(input_path: Path, config: dict[str, Any], config_path: Path) -> Path:
    output_dir = resolve_config_path(config["default_output_dir"], config_path)
    return output_dir / f"{input_path.stem}_clean08.csv"


def rejects_output_path(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.stem}_ohlc_rejects{output_path.suffix}")


def is_valid_ohlc(row: dict[str | None, str | None]) -> bool:
    """判断 low <= min(open, close) <= max(open, close) <= high。"""
    try:
        values = {
            column: Decimal(row[column].strip())
            for column in OHLC_COLUMNS
            if row.get(column) is not None
        }
    except (InvalidOperation, AttributeError):
        return False
    if len(values) != len(OHLC_COLUMNS) or not all(
        value.is_finite() for value in values.values()
    ):
        return False

    open_price = values["open"]
    high_price = values["high"]
    low_price = values["low"]
    close_price = values["close"]
    return low_price <= min(open_price, close_price) and max(
        open_price, close_price
    ) <= high_price


def clean_csv(config_path: Path, input_value: str | None, output_value: str | None) -> None:
    config = read_config(config_path)
    clean08_config = config["clean08"]
    encoding = config.get("encoding", "utf-8")
    input_path = (
        resolve_cli_path(input_value)
        if input_value
        else resolve_config_path(clean08_config["default_input_path"], config_path)
    )
    output_path = (
        resolve_cli_path(output_value)
        if output_value
        else default_output_path(input_path, clean08_config, config_path)
    )
    rejects_path = rejects_output_path(output_path)

    if not input_path.is_file():
        raise SystemExit(f"未找到输入文件：{input_path}")
    if input_path.resolve() in {output_path.resolve(), rejects_path.resolve()}:
        raise SystemExit("输入文件不能与输出文件或 OHLC 拒绝记录文件相同。")

    with input_path.open("r", encoding=encoding, newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None:
            raise SystemExit("输入 CSV 缺少表头。")
        fieldnames = reader.fieldnames
    missing_columns = [column for column in OHLC_COLUMNS if column not in fieldnames]
    if missing_columns:
        raise SystemExit("输入 CSV 缺少 OHLC 字段：" + ", ".join(missing_columns))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = output_path.with_name(f".{output_path.name}.tmp")
    temporary_rejects = rejects_path.with_name(f".{rejects_path.name}.tmp")
    total_rows = 0
    kept_rows = 0
    rejected_rows = 0

    try:
        with (
            input_path.open("r", encoding=encoding, newline="") as source,
            temporary_output.open("w", encoding=encoding, newline="") as output_file,
            temporary_rejects.open("w", encoding=encoding, newline="") as rejects_file,
        ):
            reader = csv.DictReader(source)
            output_writer = csv.DictWriter(output_file, fieldnames=fieldnames)
            rejects_writer = csv.DictWriter(
                rejects_file,
                fieldnames=[*fieldnames, "reject_reason"],
                extrasaction="ignore",
            )
            output_writer.writeheader()
            rejects_writer.writeheader()

            for row in reader:
                total_rows += 1
                if None in row:
                    raise SystemExit(f"第 {total_rows + 1:,} 行的 CSV 列数超过表头定义。")
                if is_valid_ohlc(row):
                    output_writer.writerow(row)
                    kept_rows += 1
                else:
                    row["reject_reason"] = INVALID_OHLC
                    rejects_writer.writerow(row)
                    rejected_rows += 1

                if total_rows % 1_000_000 == 0:
                    print(f"已处理 {total_rows:,} 行…")

        os.replace(temporary_output, output_path)
        os.replace(temporary_rejects, rejects_path)
    except (OSError, UnicodeError, csv.Error) as error:
        temporary_output.unlink(missing_ok=True)
        temporary_rejects.unlink(missing_ok=True)
        raise SystemExit(f"清洗失败：{error}") from error
    except BaseException:
        temporary_output.unlink(missing_ok=True)
        temporary_rejects.unlink(missing_ok=True)
        raise

    print("第八步 OHLC 逻辑清洗完成")
    print(f"输入文件：{input_path}")
    print(f"有效数据文件：{output_path}")
    print(f"OHLC 拒绝记录文件：{rejects_path}")
    print(f"数据行总数：{total_rows:,}")
    print(f"保留行数：{kept_rows:,}")
    print(f"INVALID_OHLC：{rejected_rows:,}")


def main() -> None:
    parser = argparse.ArgumentParser(description="隔离不满足 OHLC 逻辑的记录。")
    parser.add_argument(
        "-i", "--input", help="输入 CSV 路径；未提供时使用 config.json 的默认路径。"
    )
    parser.add_argument(
        "-o", "--output", help="输出 CSV 路径；未提供时自动生成 xxx_clean08.csv。"
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="配置文件路径。")
    args = parser.parse_args()
    clean_csv(Path(args.config).expanduser().resolve(), args.input, args.output)


if __name__ == "__main__":
    main()
