#!/usr/bin/env python3
"""为 5 分钟 OHLCV 数据补充 HH、HL、LL、LH 市场结构字段。

用法：
    python run_HHLL.py -i 路径/xxx.csv -o 路径/yyy.csv

规则：
* 当前 high 严格高于前后各 5 根 K 线的 high，才是摆动高点；
  当前 low 严格低于前后各 5 根 K 线的 low，才是摆动低点。
* 连续的同类摆动点只保留更极端的一个，形成交替的 ZigZag 摆动点序列。
* 摆动高点与上一个保留的摆动高点比较：更高为 Higher_High，
  更低为 Lower_High；摆动低点与上一个保留的摆动低点比较：更高为
  Higher_Low，更低为 Lower_Low。
* 每类摆动点的第一个没有可比较的前值、或与前一同类摆动点价格相等时，
  四个字段均为 0。

同一根 K 线若同时满足摆动高、摆动低的严格条件，按摆动高处理，确保
每条记录的四个新增字段最多仅有一个 1。
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Sequence


SWING_WINDOW = 5
REMOVE_COLUMNS = {"symbol", "zero_volume_flag", "rtype", "publisher_id", "instrument_id"}
GROUP_COLUMNS = ("publisher_id", "instrument_id", "symbol")
HIGHER_LOW = "Higher_Low"
HIGHER_HIGH = "Higher_High"
LOWER_LOW = "Lower_Low"
LOWER_HIGH = "Lower_High"
NEW_COLUMNS = (HIGHER_LOW, HIGHER_HIGH, LOWER_LOW, LOWER_HIGH)


@dataclass(frozen=True)
class Candidate:
    """一个局部摆动点；kind 仅可能为 high 或 low。"""

    index: int
    kind: str
    price: Decimal


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="为 5 分钟 K 线生成 HH/HL/LL/LH 字段。")
    parser.add_argument("-i", "--input", required=True, help="输入 CSV 路径")
    parser.add_argument("-o", "--output", required=True, help="输出 CSV 路径")
    return parser.parse_args()


def normalized_headers(fieldnames: Sequence[str]) -> dict[str, str]:
    """生成不区分大小写的表头映射，同时拒绝歧义列名。"""
    result: dict[str, str] = {}
    for name in fieldnames:
        key = name.lstrip("\ufeff").strip().lower()
        if key in result:
            raise ValueError(f"CSV 表头在忽略大小写后重复：{name!r}。")
        result[key] = name
    return result


def require_column(headers: dict[str, str], name: str) -> str:
    try:
        return headers[name]
    except KeyError as error:
        raise ValueError(f"输入 CSV 缺少必要字段：{name}。") from error


def decimal_value(value: str | None, line_number: int, column: str) -> Decimal:
    try:
        number = Decimal((value or "").strip())
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"第 {line_number:,} 行的 {column} 不是有效数值：{value!r}。") from error
    if not number.is_finite():
        raise ValueError(f"第 {line_number:,} 行的 {column} 必须是有限数值：{value!r}。")
    return number


def group_key(row: dict[str, str], group_columns: Sequence[str]) -> tuple[str, ...]:
    return tuple((row.get(column) or "").strip() for column in group_columns)


def raw_pivots(
    rows: Sequence[dict[str, str]], high_column: str, low_column: str, line_numbers: Sequence[int]
) -> list[Candidate]:
    """找出严格的局部高低点；同一根同时满足时优先记为高点。"""
    highs = [decimal_value(row.get(high_column), line, high_column) for row, line in zip(rows, line_numbers)]
    lows = [decimal_value(row.get(low_column), line, low_column) for row, line in zip(rows, line_numbers)]
    found: list[Candidate] = []

    for index in range(SWING_WINDOW, len(rows) - SWING_WINDOW):
        high = highs[index]
        low = lows[index]
        surrounding_highs = highs[index - SWING_WINDOW : index] + highs[index + 1 : index + SWING_WINDOW + 1]
        surrounding_lows = lows[index - SWING_WINDOW : index] + lows[index + 1 : index + SWING_WINDOW + 1]
        if high > max(surrounding_highs):
            found.append(Candidate(index, "high", high))
        elif low < min(surrounding_lows):
            found.append(Candidate(index, "low", low))
    return found


def alternating_pivots(candidates: Sequence[Candidate]) -> list[Candidate]:
    """将连续同类局部点压缩为更极端的一个，得到交替摆动点。"""
    result: list[Candidate] = []
    for candidate in candidates:
        if not result:
            result.append(candidate)
            continue

        previous = result[-1]
        if candidate.kind != previous.kind:
            result.append(candidate)
            continue

        is_more_extreme = (
            candidate.price > previous.price
            if candidate.kind == "high"
            else candidate.price < previous.price
        )
        if is_more_extreme:
            result[-1] = candidate
    return result


def labels_for_group(
    rows: Sequence[dict[str, str]], high_column: str, low_column: str, line_numbers: Sequence[int]
) -> list[str]:
    """为单个合约序列返回每行唯一的市场结构标签，未标记项为空字符串。"""
    labels = ["" for _ in rows]
    previous_high: Decimal | None = None
    previous_low: Decimal | None = None

    for pivot in alternating_pivots(raw_pivots(rows, high_column, low_column, line_numbers)):
        if pivot.kind == "high":
            if previous_high is not None:
                if pivot.price > previous_high:
                    labels[pivot.index] = HIGHER_HIGH
                elif pivot.price < previous_high:
                    labels[pivot.index] = LOWER_HIGH
            previous_high = pivot.price
        else:
            if previous_low is not None:
                if pivot.price > previous_low:
                    labels[pivot.index] = HIGHER_LOW
                elif pivot.price < previous_low:
                    labels[pivot.index] = LOWER_LOW
            previous_low = pivot.price
    return labels


def output_fields(fieldnames: Sequence[str]) -> list[str]:
    """保留原列顺序，去掉指定字段，并以固定顺序追加四个结构字段。"""
    remaining = [name for name in fieldnames if name.strip().lower() not in REMOVE_COLUMNS]
    duplicated_new_columns = [name for name in NEW_COLUMNS if name in remaining]
    if duplicated_new_columns:
        raise ValueError("输入 CSV 已含同名目标字段：" + ", ".join(duplicated_new_columns))
    return [*remaining, *NEW_COLUMNS]


def process(input_path: Path, output_path: Path) -> tuple[int, dict[str, int]]:
    with input_path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if not reader.fieldnames:
            raise ValueError("输入 CSV 为空或没有表头。")
        fieldnames = reader.fieldnames
        headers = normalized_headers(fieldnames)
        high_column = require_column(headers, "high")
        low_column = require_column(headers, "low")
        group_columns = tuple(headers[column] for column in GROUP_COLUMNS if column in headers)
        destination_fields = output_fields(fieldnames)
        temporary_path = output_path.with_name(f".{output_path.name}.tmp")
        counts = {name: 0 for name in NEW_COLUMNS}
        row_count = 0

        def write_group(
            writer: csv.DictWriter,
            group_rows: Sequence[dict[str, str]],
            group_lines: Sequence[int],
        ) -> None:
            """计算一个连续合约分组的标签后立即写出，以控制内存占用。"""
            group_labels = labels_for_group(group_rows, high_column, low_column, group_lines)
            for row, label in zip(group_rows, group_labels):
                output_row = {name: row.get(name, "") for name in destination_fields}
                for name in NEW_COLUMNS:
                    output_row[name] = "1" if name == label else "0"
                if label:
                    counts[label] += 1
                writer.writerow(output_row)

        try:
            with temporary_path.open("w", encoding="utf-8", newline="") as destination:
                writer = csv.DictWriter(destination, fieldnames=destination_fields, extrasaction="ignore")
                writer.writeheader()
                current_group: tuple[str, ...] | None = None
                current_rows: list[dict[str, str]] = []
                current_lines: list[int] = []
                completed_groups: set[tuple[str, ...]] = set()

                for line_number, row in enumerate(reader, start=2):
                    if None in row:
                        raise ValueError(f"第 {line_number:,} 行的列数超过表头定义。")
                    row_count += 1
                    row_group = group_key(row, group_columns)
                    if current_group is None:
                        current_group = row_group
                    elif row_group != current_group:
                        write_group(writer, current_rows, current_lines)
                        completed_groups.add(current_group)
                        if row_group in completed_groups:
                            raise ValueError(
                                "输入数据的同一合约分组不是连续排列；请先按 "
                                "publisher_id、instrument_id、symbol 和时间排序。"
                            )
                        current_group = row_group
                        current_rows = []
                        current_lines = []
                    current_rows.append(row)
                    current_lines.append(line_number)

                if current_group is None:
                    raise ValueError("输入 CSV 只有表头，没有数据记录。")
                write_group(writer, current_rows, current_lines)
            os.replace(temporary_path, output_path)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise

    return row_count, counts


def main() -> int:
    args = parse_arguments()
    input_path = Path(args.input).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    if not input_path.is_file():
        print(f"错误：找不到输入文件：{input_path}", file=sys.stderr)
        return 2
    if input_path == output_path:
        print("错误：输入文件和输出文件不能相同。", file=sys.stderr)
        return 2

    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        row_count, counts = process(input_path, output_path)
    except (OSError, UnicodeError, csv.Error, ValueError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2

    print("处理完成")
    print(f"输入：{input_path}")
    print(f"输出：{output_path}")
    print(f"处理记录数：{row_count:,}")
    print("标记数量：" + "，".join(f"{name}={counts[name]:,}" for name in NEW_COLUMNS))
    print("摆动窗口：前后各 5 根 K 线；价格相等不标记。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
