#!/usr/bin/env python3
"""第二步清洗：验证 Databento 一分钟 OHLCV 的事件时间。"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = "config.json"
TIMESTAMP_PATTERN = re.compile(
    r"^(?P<date_time>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d{1,9}))?Z$"
)
INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
TIMESTAMP_NOT_MINUTE_ALIGNED = "TIMESTAMP_NOT_MINUTE_ALIGNED"
TIMESTAMP_OUT_OF_RANGE = "TIMESTAMP_OUT_OF_RANGE"


def read_config(config_path: Path) -> dict[str, Any]:
    try:
        with config_path.open("r", encoding="utf-8") as config_file:
            config = json.load(config_file)
    except FileNotFoundError as error:
        raise SystemExit(f"未找到配置文件：{config_path}") from error
    except json.JSONDecodeError as error:
        raise SystemExit(f"配置文件不是有效 JSON：{error}") from error

    clean02_config = config.get("clean02")
    if not isinstance(clean02_config, dict):
        raise SystemExit("配置文件缺少 clean02 配置区段。")

    required_keys = (
        "default_input_path",
        "default_output_dir",
        "download_start_utc",
        "download_end_utc",
    )
    missing_keys = [key for key in required_keys if key not in clean02_config]
    if missing_keys:
        raise SystemExit("clean02 配置缺少：" + ", ".join(missing_keys))
    return config


def resolve_config_path(value: str, config_path: Path) -> Path:
    """将配置内的相对路径解析为相对于 config.json 的路径。"""
    path = Path(value).expanduser()
    return path if path.is_absolute() else config_path.parent / path


def resolve_cli_path(value: str) -> Path:
    """将命令行路径解析为相对于当前执行目录的路径。"""
    return Path(value).expanduser().resolve()


def parse_config_timestamp(value: str, name: str) -> datetime:
    try:
        timestamp = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise SystemExit(f"配置项 {name} 不是有效 ISO 8601 时间：{value}") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise SystemExit(f"配置项 {name} 必须包含时区信息：{value}")
    return timestamp.astimezone(timezone.utc)


def parse_databento_timestamp(value: str | None) -> tuple[datetime | None, int]:
    """解析带 Z 后缀、最多九位小数秒的 Databento UTC 时间。"""
    if value is None or value.strip() == "":
        return None, 0

    match = TIMESTAMP_PATTERN.fullmatch(value.strip())
    if match is None:
        return None, 0

    fraction = match.group("fraction") or ""
    nanoseconds = int(fraction.ljust(9, "0")) if fraction else 0
    microseconds = nanoseconds // 1_000
    try:
        timestamp = datetime.fromisoformat(match.group("date_time")).replace(
            microsecond=microseconds, tzinfo=timezone.utc
        )
    except ValueError:
        return None, 0
    return timestamp, nanoseconds


def validate_timestamp(
    value: str | None, start_utc: datetime, end_utc: datetime
) -> tuple[datetime | None, str | None]:
    timestamp, nanoseconds = parse_databento_timestamp(value)
    if timestamp is None:
        return None, INVALID_TIMESTAMP
    if timestamp < start_utc or timestamp > end_utc:
        return None, TIMESTAMP_OUT_OF_RANGE
    if timestamp.second != 0 or timestamp.microsecond != 0 or nanoseconds != 0:
        return None, TIMESTAMP_NOT_MINUTE_ALIGNED
    return timestamp, None


def default_output_path(input_path: Path, clean02_config: dict[str, Any], config_path: Path) -> Path:
    output_dir = resolve_config_path(clean02_config["default_output_dir"], config_path)
    return output_dir / f"{input_path.stem}_clean02.csv"


def reject_output_path(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.stem}_rejects{output_path.suffix}")


def clean_csv(config_path: Path, input_value: str | None, output_value: str | None) -> None:
    config = read_config(config_path)
    clean02_config = config["clean02"]
    encoding = config.get("encoding", "utf-8")
    start_utc = parse_config_timestamp(
        clean02_config["download_start_utc"], "clean02.download_start_utc"
    )
    end_utc = parse_config_timestamp(
        clean02_config["download_end_utc"], "clean02.download_end_utc"
    )
    if start_utc > end_utc:
        raise SystemExit("clean02 下载起始时间不能晚于结束时间。")

    input_path = (
        resolve_cli_path(input_value)
        if input_value
        else resolve_config_path(clean02_config["default_input_path"], config_path)
    )
    output_path = (
        resolve_cli_path(output_value)
        if output_value
        else default_output_path(input_path, clean02_config, config_path)
    )
    rejects_path = reject_output_path(output_path)

    if not input_path.is_file():
        raise SystemExit(f"未找到输入文件：{input_path}")
    if input_path.resolve() in {output_path.resolve(), rejects_path.resolve()}:
        raise SystemExit("输入文件不能与输出文件或拒绝记录文件相同。")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    valid_temporary_path = output_path.with_name(f".{output_path.name}.tmp")
    rejects_temporary_path = rejects_path.with_name(f".{rejects_path.name}.tmp")
    total_rows = 0
    kept_rows = 0
    rejected = Counter()

    try:
        with (
            input_path.open("r", encoding=encoding, newline="") as source,
            valid_temporary_path.open("w", encoding=encoding, newline="") as valid_file,
            rejects_temporary_path.open("w", encoding=encoding, newline="") as rejects_file,
        ):
            reader = csv.DictReader(source)
            if reader.fieldnames is None:
                raise SystemExit("输入 CSV 缺少表头。")
            if "ts_event" not in reader.fieldnames:
                raise SystemExit("输入 CSV 缺少 ts_event 字段。")

            output_fields = [
                "ts_utc" if field == "ts_event" else field for field in reader.fieldnames
            ]
            valid_writer = csv.DictWriter(
                valid_file, fieldnames=output_fields, extrasaction="ignore"
            )
            reject_writer = csv.DictWriter(
                rejects_file,
                fieldnames=[*reader.fieldnames, "reject_reason"],
                extrasaction="ignore",
            )
            valid_writer.writeheader()
            reject_writer.writeheader()

            for row in reader:
                total_rows += 1
                timestamp, reason = validate_timestamp(
                    row.get("ts_event"), start_utc, end_utc
                )
                if reason is not None:
                    row["reject_reason"] = reason
                    reject_writer.writerow(row)
                    rejected[reason] += 1
                    continue

                output_row = {
                    ("ts_utc" if key == "ts_event" else key): value
                    for key, value in row.items()
                    if key is not None
                }
                output_row["ts_utc"] = timestamp.strftime("%Y-%m-%d %H:%M:%S+00:00")
                valid_writer.writerow(output_row)
                kept_rows += 1

        os.replace(valid_temporary_path, output_path)
        os.replace(rejects_temporary_path, rejects_path)
    except (OSError, UnicodeError, csv.Error) as error:
        valid_temporary_path.unlink(missing_ok=True)
        rejects_temporary_path.unlink(missing_ok=True)
        raise SystemExit(f"清洗失败：{error}") from error
    except BaseException:
        valid_temporary_path.unlink(missing_ok=True)
        rejects_temporary_path.unlink(missing_ok=True)
        raise

    print("第二步时间清洗完成")
    print(f"输入文件：{input_path}")
    print(f"有效数据文件：{output_path}")
    print(f"拒绝记录文件：{rejects_path}")
    print(f"数据行总数：{total_rows:,}")
    print(f"保留行数：{kept_rows:,}")
    print(f"拒绝行数：{sum(rejected.values()):,}")
    for reason in (
        INVALID_TIMESTAMP,
        TIMESTAMP_NOT_MINUTE_ALIGNED,
        TIMESTAMP_OUT_OF_RANGE,
    ):
        print(f"{reason}：{rejected[reason]:,}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="验证一分钟 OHLCV 事件时间，并生成 ts_utc 字段。"
    )
    parser.add_argument(
        "-i", "--input", help="输入 CSV 路径；未提供时使用 config.json 的默认路径。"
    )
    parser.add_argument(
        "-o", "--output", help="有效数据输出 CSV 路径；未提供时自动生成 xxx_clean02.csv。"
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="配置文件路径。")
    args = parser.parse_args()
    clean_csv(Path(args.config).expanduser().resolve(), args.input, args.output)


if __name__ == "__main__":
    main()
