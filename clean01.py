#!/usr/bin/env python3
"""第一步清洗：删除必填字段为空的 CSV 记录。"""

from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = "config.json"


def read_config(config_path: Path) -> dict[str, Any]:
    """读取并验证 JSON 配置。"""
    try:
        with config_path.open("r", encoding="utf-8") as config_file:
            config = json.load(config_file)
    except FileNotFoundError as error:
        raise SystemExit(f"未找到配置文件：{config_path}") from error
    except json.JSONDecodeError as error:
        raise SystemExit(f"配置文件不是有效 JSON：{error}") from error

    for key in ("input_path", "output_path", "required_columns"):
        if key not in config:
            raise SystemExit(f"配置文件缺少必需项：{key}")

    if not isinstance(config["required_columns"], list) or not config["required_columns"]:
        raise SystemExit("required_columns 必须是非空列表。")

    return config


def resolve_path(value: str, config_path: Path) -> Path:
    """相对路径以 config.json 所在目录为基准。"""
    path = Path(value).expanduser()
    return path if path.is_absolute() else config_path.parent / path


def is_empty(value: str | None) -> bool:
    """空字符串、纯空白字符串和缺失列值都判定为空。"""
    return value is None or value.strip() == ""


def clean_csv(config_path: Path) -> None:
    config = read_config(config_path)
    input_path = resolve_path(config["input_path"], config_path)
    output_path = resolve_path(config["output_path"], config_path)
    required_columns = config["required_columns"]
    encoding = config.get("encoding", "utf-8")

    if input_path.resolve() == output_path.resolve():
        raise SystemExit("input_path 和 output_path 不能指向同一个文件。")
    if not input_path.is_file():
        raise SystemExit(f"未找到输入文件：{input_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f".{output_path.name}.tmp")
    total_rows = 0
    kept_rows = 0
    removed_rows = 0

    try:
        with input_path.open("r", encoding=encoding, newline="") as source, temporary_path.open(
            "w", encoding=encoding, newline=""
        ) as destination:
            reader = csv.DictReader(source)
            if reader.fieldnames is None:
                raise SystemExit("输入 CSV 缺少表头。")

            missing_headers = [
                column for column in required_columns if column not in reader.fieldnames
            ]
            if missing_headers:
                raise SystemExit(
                    "输入 CSV 缺少必填字段：" + ", ".join(missing_headers)
                )

            writer = csv.DictWriter(
                destination, fieldnames=reader.fieldnames, extrasaction="ignore"
            )
            writer.writeheader()

            for row in reader:
                total_rows += 1

                if any(is_empty(row.get(column)) for column in required_columns):
                    removed_rows += 1
                    continue

                writer.writerow(row)
                kept_rows += 1

        os.replace(temporary_path, output_path)
    except (OSError, UnicodeError, csv.Error) as error:
        temporary_path.unlink(missing_ok=True)
        raise SystemExit(f"清洗失败：{error}") from error
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise

    print("清洗完成")
    print(f"输入文件：{input_path}")
    print(f"输出文件：{output_path}")
    print(f"数据行总数：{total_rows:,}")
    print(f"保留行数：{kept_rows:,}")
    print(f"删除行数：{removed_rows:,}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="删除指定必填字段为空的 CSV 记录。"
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        help="配置文件路径（默认：config.json）。",
    )
    args = parser.parse_args()
    clean_csv(Path(args.config).expanduser().resolve())


if __name__ == "__main__":
    main()
