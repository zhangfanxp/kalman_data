#!/usr/bin/env python3
"""第十二步清洗：保留全部记录，并输出大涨大跌人工审计表。"""

from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = "config.json"
GROUP_COLUMNS = ("publisher_id", "instrument_id", "symbol")
TIMESTAMP_COLUMN = "ts_utc"
CLOSE_COLUMN = "close"
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S%z"


def read_config(config_path: Path) -> dict[str, Any]:
    try:
        with config_path.open("r", encoding="utf-8") as config_file:
            config = json.load(config_file)
    except FileNotFoundError as error:
        raise SystemExit(f"未找到配置文件：{config_path}") from error
    except json.JSONDecodeError as error:
        raise SystemExit(f"配置文件不是有效 JSON：{error}") from error

    clean12_config = config.get("clean12")
    if not isinstance(clean12_config, dict):
        raise SystemExit("配置文件缺少 clean12 配置区段。")
    required_keys = (
        "default_input_path",
        "default_output_dir",
        "large_up_down_output_path",
        "return_threshold_pct",
    )
    missing_keys = [key for key in required_keys if key not in clean12_config]
    if missing_keys:
        raise SystemExit("clean12 配置缺少：" + ", ".join(missing_keys))
    return config


def resolve_config_path(value: str, config_path: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else config_path.parent / path


def resolve_cli_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def default_output_path(input_path: Path, config: dict[str, Any], config_path: Path) -> Path:
    output_dir = resolve_config_path(config["default_output_dir"], config_path)
    return output_dir / f"{input_path.stem}_clean12.csv"


def parse_threshold(value: Any) -> Decimal:
    try:
        threshold = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise SystemExit(f"clean12.return_threshold_pct 不是有效数值：{value}") from error
    if not threshold.is_finite() or threshold <= 0:
        raise SystemExit("clean12.return_threshold_pct 必须是有限正数。")
    return threshold


def parse_close(value: str | None, row_number: int) -> Decimal:
    try:
        close = Decimal(value.strip()) if value is not None else Decimal("NaN")
    except (InvalidOperation, ValueError) as error:
        raise SystemExit(f"第 {row_number:,} 行 close 无效；请先运行 clean07.py。") from error
    if not close.is_finite() or close <= 0:
        raise SystemExit(f"第 {row_number:,} 行 close 无效；请先运行 clean07.py。")
    return close


def parse_timestamp(value: str | None, row_number: int) -> datetime:
    try:
        return datetime.strptime(value or "", TIMESTAMP_FORMAT)
    except ValueError as error:
        raise SystemExit(f"第 {row_number:,} 行 ts_utc 无效；请先运行 clean02.py。") from error


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def clean_csv(
    config_path: Path,
    input_value: str | None,
    output_value: str | None,
    audit_output_value: str | None,
) -> None:
    config = read_config(config_path)
    clean12_config = config["clean12"]
    threshold_pct = parse_threshold(clean12_config["return_threshold_pct"])
    encoding = config.get("encoding", "utf-8")
    input_path = (
        resolve_cli_path(input_value)
        if input_value
        else resolve_config_path(clean12_config["default_input_path"], config_path)
    )
    output_path = (
        resolve_cli_path(output_value)
        if output_value
        else default_output_path(input_path, clean12_config, config_path)
    )
    audit_path = (
        resolve_cli_path(audit_output_value)
        if audit_output_value
        else resolve_config_path(clean12_config["large_up_down_output_path"], config_path)
    )

    if not input_path.is_file():
        raise SystemExit(f"未找到输入文件：{input_path}")
    if input_path.resolve() in {output_path.resolve(), audit_path.resolve()}:
        raise SystemExit("输入文件不能与输出文件或大涨大跌审计文件相同。")
    if output_path.resolve() == audit_path.resolve():
        raise SystemExit("输出文件和大涨大跌审计文件不能相同。")

    with input_path.open("r", encoding=encoding, newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None:
            raise SystemExit("输入 CSV 缺少表头。")
        fieldnames = reader.fieldnames
    required_columns = [*GROUP_COLUMNS, TIMESTAMP_COLUMN, CLOSE_COLUMN]
    missing_columns = [column for column in required_columns if column not in fieldnames]
    if missing_columns:
        raise SystemExit("输入 CSV 缺少字段：" + ", ".join(missing_columns))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = output_path.with_name(f".{output_path.name}.tmp")
    temporary_audit = audit_path.with_name(f".{audit_path.name}.tmp")
    audit_fields = [
        *fieldnames,
        "previous_ts_utc",
        "previous_close",
        "close_return_pct",
        "minutes_since_previous",
        "anomaly_direction",
    ]
    total_rows = 0
    kept_rows = 0
    flagged_rows = 0
    previous_sort_key: tuple[str, str, str, str] | None = None
    current_group: tuple[str, str, str] | None = None
    previous_timestamp: datetime | None = None
    previous_timestamp_text = ""
    previous_close: Decimal | None = None
    previous_close_text = ""

    try:
        with (
            input_path.open("r", encoding=encoding, newline="") as source,
            temporary_output.open("w", encoding=encoding, newline="") as output_file,
            temporary_audit.open("w", encoding=encoding, newline="") as audit_file,
        ):
            reader = csv.DictReader(source)
            output_writer = csv.DictWriter(output_file, fieldnames=fieldnames)
            audit_writer = csv.DictWriter(audit_file, fieldnames=audit_fields)
            output_writer.writeheader()
            audit_writer.writeheader()

            for row_number, row in enumerate(reader, start=2):
                total_rows += 1
                if None in row:
                    raise SystemExit(f"第 {row_number:,} 行的 CSV 列数超过表头定义。")
                group = tuple(row[column] for column in GROUP_COLUMNS)
                if any(value is None or value.strip() == "" for value in group):
                    raise SystemExit(f"第 {row_number:,} 行的合约分组字段为空。")
                timestamp_text = row[TIMESTAMP_COLUMN]
                sort_key = (*group, timestamp_text)
                if previous_sort_key is not None and sort_key < previous_sort_key:
                    raise SystemExit(
                        f"第 {row_number:,} 行未按合约键和 ts_utc 排序；请先运行 clean04.py。"
                    )
                previous_sort_key = sort_key
                timestamp = parse_timestamp(timestamp_text, row_number)
                close = parse_close(row.get(CLOSE_COLUMN), row_number)

                output_writer.writerow(row)
                kept_rows += 1

                if group != current_group:
                    current_group = group
                    previous_timestamp = None
                    previous_close = None
                    previous_timestamp_text = ""
                    previous_close_text = ""

                if previous_close is not None and previous_timestamp is not None:
                    return_pct = (close - previous_close) / previous_close * Decimal("100")
                    if abs(return_pct) >= threshold_pct:
                        minutes_since_previous = int(
                            (timestamp - previous_timestamp).total_seconds() // 60
                        )
                        audit_row = {
                            **row,
                            "previous_ts_utc": previous_timestamp_text,
                            "previous_close": previous_close_text,
                            "close_return_pct": decimal_text(return_pct),
                            "minutes_since_previous": minutes_since_previous,
                            "anomaly_direction": "LARGE_UP"
                            if return_pct > 0
                            else "LARGE_DOWN",
                        }
                        audit_writer.writerow(audit_row)
                        flagged_rows += 1

                previous_timestamp = timestamp
                previous_timestamp_text = timestamp_text
                previous_close = close
                previous_close_text = row[CLOSE_COLUMN]

                if total_rows % 1_000_000 == 0:
                    print(f"已处理 {total_rows:,} 行…")

        os.replace(temporary_output, output_path)
        os.replace(temporary_audit, audit_path)
    except (OSError, UnicodeError, csv.Error) as error:
        temporary_output.unlink(missing_ok=True)
        temporary_audit.unlink(missing_ok=True)
        raise SystemExit(f"清洗失败：{error}") from error
    except BaseException:
        temporary_output.unlink(missing_ok=True)
        temporary_audit.unlink(missing_ok=True)
        raise

    print("第十二步异常波动审计完成")
    print(f"输入文件：{input_path}")
    print(f"完整保留数据文件：{output_path}")
    print(f"大涨大跌审计文件：{audit_path}")
    print(f"涨跌幅阈值：{decimal_text(threshold_pct)}%")
    print(f"数据行总数：{total_rows:,}")
    print(f"保留行数：{kept_rows:,}")
    print(f"大涨大跌标记行数：{flagged_rows:,}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="保留全部记录，并输出供人工审查的大涨大跌记录表。"
    )
    parser.add_argument(
        "-i", "--input", help="输入 CSV 路径；未提供时使用 config.json 的默认路径。"
    )
    parser.add_argument(
        "-o", "--output", help="输出 CSV 路径；未提供时自动生成 xxx_clean12.csv。"
    )
    parser.add_argument(
        "--audit-output", help="Large_up_down.csv 输出路径；未提供时使用 config.json 的默认路径。"
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="配置文件路径。")
    args = parser.parse_args()
    clean_csv(
        Path(args.config).expanduser().resolve(),
        args.input,
        args.output,
        args.audit_output,
    )


if __name__ == "__main__":
    main()
