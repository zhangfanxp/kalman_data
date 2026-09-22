#!/usr/bin/env python3
"""第十步清洗：隔离无效、负数或非整数成交量。"""

from __future__ import annotations

import argparse
import csv
import json
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = "config.json"
VOLUME_COLUMN = "volume"
NEGATIVE_VOLUME = "NEGATIVE_VOLUME"
INVALID_VOLUME = "INVALID_VOLUME"
NON_INTEGER_VOLUME = "NON_INTEGER_VOLUME"


def read_config(config_path: Path) -> dict[str, Any]:
    try:
        with config_path.open("r", encoding="utf-8") as config_file:
            config = json.load(config_file)
    except FileNotFoundError as error:
        raise SystemExit(f"未找到配置文件：{config_path}") from error
    except json.JSONDecodeError as error:
        raise SystemExit(f"配置文件不是有效 JSON：{error}") from error

    clean10_config = config.get("clean10")
    if not isinstance(clean10_config, dict):
        raise SystemExit("配置文件缺少 clean10 配置区段。")
    required_keys = ("default_input_path", "default_output_dir")
    missing_keys = [key for key in required_keys if key not in clean10_config]
    if missing_keys:
        raise SystemExit("clean10 配置缺少：" + ", ".join(missing_keys))
    return config


def resolve_config_path(value: str, config_path: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else config_path.parent / path


def resolve_cli_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def default_output_path(input_path: Path, config: dict[str, Any], config_path: Path) -> Path:
    output_dir = resolve_config_path(config["default_output_dir"], config_path)
    return output_dir / f"{input_path.stem}_clean10.csv"


def rejects_output_path(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.stem}_volume_rejects{output_path.suffix}")


def classify_volume(value: str | None) -> str | None:
    """返回拒绝原因；None 表示成交量是非负整数。"""
    if value is None or value.strip() == "":
        return INVALID_VOLUME
    try:
        volume = Decimal(value.strip())
    except (InvalidOperation, ValueError):
        return INVALID_VOLUME
    if not volume.is_finite():
        return INVALID_VOLUME
    if volume < 0:
        return NEGATIVE_VOLUME
    if volume != volume.to_integral_value():
        return NON_INTEGER_VOLUME
    return None


def clean_csv(config_path: Path, input_value: str | None, output_value: str | None) -> None:
    config = read_config(config_path)
    clean10_config = config["clean10"]
    encoding = config.get("encoding", "utf-8")
    input_path = (
        resolve_cli_path(input_value)
        if input_value
        else resolve_config_path(clean10_config["default_input_path"], config_path)
    )
    output_path = (
        resolve_cli_path(output_value)
        if output_value
        else default_output_path(input_path, clean10_config, config_path)
    )
    rejects_path = rejects_output_path(output_path)

    if not input_path.is_file():
        raise SystemExit(f"未找到输入文件：{input_path}")
    if input_path.resolve() in {output_path.resolve(), rejects_path.resolve()}:
        raise SystemExit("输入文件不能与输出文件或成交量拒绝记录文件相同。")

    with input_path.open("r", encoding=encoding, newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None:
            raise SystemExit("输入 CSV 缺少表头。")
        fieldnames = reader.fieldnames
    if VOLUME_COLUMN not in fieldnames:
        raise SystemExit("输入 CSV 缺少 volume 字段。")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = output_path.with_name(f".{output_path.name}.tmp")
    temporary_rejects = rejects_path.with_name(f".{rejects_path.name}.tmp")
    total_rows = 0
    kept_rows = 0
    rejected_counts = {
        NEGATIVE_VOLUME: 0,
        INVALID_VOLUME: 0,
        NON_INTEGER_VOLUME: 0,
    }

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
                reason = classify_volume(row.get(VOLUME_COLUMN))
                if reason is None:
                    output_writer.writerow(row)
                    kept_rows += 1
                else:
                    row["reject_reason"] = reason
                    rejects_writer.writerow(row)
                    rejected_counts[reason] += 1

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

    print("第十步成交量清洗完成")
    print(f"输入文件：{input_path}")
    print(f"有效数据文件：{output_path}")
    print(f"成交量拒绝记录文件：{rejects_path}")
    print(f"数据行总数：{total_rows:,}")
    print(f"保留行数：{kept_rows:,}")
    print(f"拒绝行数：{sum(rejected_counts.values()):,}")
    for reason in (NEGATIVE_VOLUME, INVALID_VOLUME, NON_INTEGER_VOLUME):
        print(f"{reason}：{rejected_counts[reason]:,}")


def main() -> None:
    parser = argparse.ArgumentParser(description="隔离无效、负数或非整数成交量。")
    parser.add_argument(
        "-i", "--input", help="输入 CSV 路径；未提供时使用 config.json 的默认路径。"
    )
    parser.add_argument(
        "-o", "--output", help="输出 CSV 路径；未提供时自动生成 xxx_clean10.csv。"
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="配置文件路径。")
    args = parser.parse_args()
    clean_csv(Path(args.config).expanduser().resolve(), args.input, args.output)


if __name__ == "__main__":
    main()
