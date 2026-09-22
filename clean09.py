#!/usr/bin/env python3
"""第九步清洗：隔离不落在 NQ 0.25 点价格网格上的记录。"""

from __future__ import annotations

import argparse
import csv
import json
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = "config.json"
PRICE_COLUMNS = ("open", "high", "low", "close")
INVALID_TICK_SIZE = "INVALID_TICK_SIZE"


def read_config(config_path: Path) -> dict[str, Any]:
    try:
        with config_path.open("r", encoding="utf-8") as config_file:
            config = json.load(config_file)
    except FileNotFoundError as error:
        raise SystemExit(f"未找到配置文件：{config_path}") from error
    except json.JSONDecodeError as error:
        raise SystemExit(f"配置文件不是有效 JSON：{error}") from error

    clean09_config = config.get("clean09")
    if not isinstance(clean09_config, dict):
        raise SystemExit("配置文件缺少 clean09 配置区段。")
    required_keys = ("default_input_path", "default_output_dir", "tick_size", "tick_tolerance")
    missing_keys = [key for key in required_keys if key not in clean09_config]
    if missing_keys:
        raise SystemExit("clean09 配置缺少：" + ", ".join(missing_keys))
    return config


def resolve_config_path(value: str, config_path: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else config_path.parent / path


def resolve_cli_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def default_output_path(input_path: Path, config: dict[str, Any], config_path: Path) -> Path:
    output_dir = resolve_config_path(config["default_output_dir"], config_path)
    return output_dir / f"{input_path.stem}_clean09.csv"


def rejects_output_path(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.stem}_tick_rejects{output_path.suffix}")


def parse_positive_decimal(value: Any, config_key: str) -> Decimal:
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise SystemExit(f"配置项 {config_key} 不是有效数值：{value}") from error
    if not decimal_value.is_finite() or decimal_value <= 0:
        raise SystemExit(f"配置项 {config_key} 必须是有限正数：{value}")
    return decimal_value


def is_valid_tick(price_text: str | None, tick_size: Decimal, tolerance: Decimal) -> bool:
    """以 Decimal 的 tick 数判断价格是否落在网格上，避免浮点取余误差。"""
    if price_text is None or price_text.strip() == "":
        return False
    try:
        price = Decimal(price_text.strip())
    except (InvalidOperation, ValueError):
        return False
    if not price.is_finite():
        return False

    tick_value = price / tick_size
    nearest_tick = tick_value.to_integral_value()
    return abs(tick_value - nearest_tick) < tolerance


def has_valid_tick_size(
    row: dict[str | None, str | None], tick_size: Decimal, tolerance: Decimal
) -> bool:
    return all(is_valid_tick(row.get(column), tick_size, tolerance) for column in PRICE_COLUMNS)


def clean_csv(config_path: Path, input_value: str | None, output_value: str | None) -> None:
    config = read_config(config_path)
    clean09_config = config["clean09"]
    tick_size = parse_positive_decimal(clean09_config["tick_size"], "clean09.tick_size")
    tolerance = parse_positive_decimal(
        clean09_config["tick_tolerance"], "clean09.tick_tolerance"
    )
    encoding = config.get("encoding", "utf-8")
    input_path = (
        resolve_cli_path(input_value)
        if input_value
        else resolve_config_path(clean09_config["default_input_path"], config_path)
    )
    output_path = (
        resolve_cli_path(output_value)
        if output_value
        else default_output_path(input_path, clean09_config, config_path)
    )
    rejects_path = rejects_output_path(output_path)

    if not input_path.is_file():
        raise SystemExit(f"未找到输入文件：{input_path}")
    if input_path.resolve() in {output_path.resolve(), rejects_path.resolve()}:
        raise SystemExit("输入文件不能与输出文件或 tick 拒绝记录文件相同。")

    with input_path.open("r", encoding=encoding, newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None:
            raise SystemExit("输入 CSV 缺少表头。")
        fieldnames = reader.fieldnames
    missing_columns = [column for column in PRICE_COLUMNS if column not in fieldnames]
    if missing_columns:
        raise SystemExit("输入 CSV 缺少价格字段：" + ", ".join(missing_columns))

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
                if has_valid_tick_size(row, tick_size, tolerance):
                    output_writer.writerow(row)
                    kept_rows += 1
                else:
                    row["reject_reason"] = INVALID_TICK_SIZE
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

    print("第九步价格跳动清洗完成")
    print(f"输入文件：{input_path}")
    print(f"有效数据文件：{output_path}")
    print(f"Tick 拒绝记录文件：{rejects_path}")
    print(f"数据行总数：{total_rows:,}")
    print(f"保留行数：{kept_rows:,}")
    print(f"INVALID_TICK_SIZE：{rejected_rows:,}")


def main() -> None:
    parser = argparse.ArgumentParser(description="隔离不符合 NQ 0.25 点最小跳动的记录。")
    parser.add_argument(
        "-i", "--input", help="输入 CSV 路径；未提供时使用 config.json 的默认路径。"
    )
    parser.add_argument(
        "-o", "--output", help="输出 CSV 路径；未提供时自动生成 xxx_clean09.csv。"
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="配置文件路径。")
    args = parser.parse_args()
    clean_csv(Path(args.config).expanduser().resolve(), args.input, args.output)


if __name__ == "__main__":
    main()
