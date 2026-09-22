#!/usr/bin/env python3
"""第三步清洗：只保留 NQ 期货单腿季度合约。"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = "config.json"
NQ_SINGLE_LEG_PATTERN = re.compile(r"^NQ[HMUZ][0-9]{1,2}$")
NQ_SPREAD_PATTERN = re.compile(r"^NQ[HMUZ][0-9]{1,2}-NQ[HMUZ][0-9]{1,2}$")
OTHER_FUTURES_PATTERN = re.compile(r"^[A-Z]{1,6}[HMUZ][0-9]{1,2}$")
NQ_OPTION_PATTERNS = (
    re.compile(r"^NQ[HMUZ][0-9]{1,2}\s*[CP]\s*[0-9]+(?:\.[0-9]+)?$"),
    re.compile(r"^NQ\s*[CP]\s*[0-9]+(?:\.[0-9]+)?$"),
)

INVALID_PRODUCT = "INVALID_PRODUCT"
FUTURES_SPREAD = "FUTURES_SPREAD"
OPTION_INSTRUMENT = "OPTION_INSTRUMENT"
INVALID_SYMBOL = "INVALID_SYMBOL"
DEFINITION_FIELDS = ("instrument_class", "security_type")


def read_config(config_path: Path) -> dict[str, Any]:
    try:
        with config_path.open("r", encoding="utf-8") as config_file:
            config = json.load(config_file)
    except FileNotFoundError as error:
        raise SystemExit(f"未找到配置文件：{config_path}") from error
    except json.JSONDecodeError as error:
        raise SystemExit(f"配置文件不是有效 JSON：{error}") from error

    clean03_config = config.get("clean03")
    if not isinstance(clean03_config, dict):
        raise SystemExit("配置文件缺少 clean03 配置区段。")
    required_keys = ("default_input_path", "default_output_dir")
    missing_keys = [key for key in required_keys if key not in clean03_config]
    if missing_keys:
        raise SystemExit("clean03 配置缺少：" + ", ".join(missing_keys))
    return config


def resolve_config_path(value: str, config_path: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else config_path.parent / path


def resolve_cli_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def definition_kind(row: dict[str | None, str | None], config: dict[str, Any]) -> str | None:
    """优先依据合约定义字段识别期货或期权；无此字段时返回 None。"""
    future_values = {value.upper() for value in config.get("future_definition_values", [])}
    option_values = {value.upper() for value in config.get("option_definition_values", [])}
    values = [
        value.strip().upper()
        for field in DEFINITION_FIELDS
        if (value := row.get(field)) and value.strip()
    ]
    if not values:
        return None
    if any(value in option_values for value in values):
        return "option"
    if any(value in future_values for value in values):
        return "future"
    return "other"


def is_option_symbol(symbol: str) -> bool:
    return any(pattern.fullmatch(symbol) for pattern in NQ_OPTION_PATTERNS)


def classify_symbol(row: dict[str | None, str | None], config: dict[str, Any]) -> str | None:
    """返回拒绝原因；返回 None 表示该记录应保留。"""
    raw_symbol = row.get("symbol")
    if raw_symbol is None or raw_symbol.strip() == "":
        return INVALID_SYMBOL

    symbol = raw_symbol.strip().upper()
    kind = definition_kind(row, config)
    if kind == "option":
        return OPTION_INSTRUMENT
    if kind == "other":
        return INVALID_PRODUCT

    if NQ_SPREAD_PATTERN.fullmatch(symbol):
        return FUTURES_SPREAD
    if is_option_symbol(symbol):
        return OPTION_INSTRUMENT
    if NQ_SINGLE_LEG_PATTERN.fullmatch(symbol):
        return None
    if OTHER_FUTURES_PATTERN.fullmatch(symbol):
        return INVALID_PRODUCT
    return INVALID_SYMBOL


def default_output_path(input_path: Path, clean03_config: dict[str, Any], config_path: Path) -> Path:
    output_dir = resolve_config_path(clean03_config["default_output_dir"], config_path)
    return output_dir / f"{input_path.stem}_clean03.csv"


def reject_output_path(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.stem}_rejects{output_path.suffix}")


def clean_csv(config_path: Path, input_value: str | None, output_value: str | None) -> None:
    config = read_config(config_path)
    clean03_config = config["clean03"]
    encoding = config.get("encoding", "utf-8")
    input_path = (
        resolve_cli_path(input_value)
        if input_value
        else resolve_config_path(clean03_config["default_input_path"], config_path)
    )
    output_path = (
        resolve_cli_path(output_value)
        if output_value
        else default_output_path(input_path, clean03_config, config_path)
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
            if "symbol" not in reader.fieldnames:
                raise SystemExit("输入 CSV 缺少 symbol 字段。")

            valid_writer = csv.DictWriter(
                valid_file, fieldnames=reader.fieldnames, extrasaction="ignore"
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
                reason = classify_symbol(row, clean03_config)
                if reason is not None:
                    row["reject_reason"] = reason
                    reject_writer.writerow(row)
                    rejected[reason] += 1
                    continue
                valid_writer.writerow(row)
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

    print("第三步合约清洗完成")
    print(f"输入文件：{input_path}")
    print(f"有效数据文件：{output_path}")
    print(f"拒绝记录文件：{rejects_path}")
    print(f"数据行总数：{total_rows:,}")
    print(f"保留行数：{kept_rows:,}")
    print(f"拒绝行数：{sum(rejected.values()):,}")
    for reason in (
        INVALID_PRODUCT,
        FUTURES_SPREAD,
        OPTION_INSTRUMENT,
        INVALID_SYMBOL,
    ):
        print(f"{reason}：{rejected[reason]:,}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="只保留 NQ 期货单腿季度合约。"
    )
    parser.add_argument(
        "-i", "--input", help="输入 CSV 路径；未提供时使用 config.json 的默认路径。"
    )
    parser.add_argument(
        "-o", "--output", help="有效数据输出路径；未提供时自动生成 xxx_clean03.csv。"
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="配置文件路径。")
    args = parser.parse_args()
    clean_csv(Path(args.config).expanduser().resolve(), args.input, args.output)


if __name__ == "__main__":
    main()
