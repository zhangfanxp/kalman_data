#!/usr/bin/env python3
"""将一分钟 OHLCV CSV 聚合为五分钟 K 线。

用法：
    python run_1To5.py -i 路径/xxx.csv -o 路径2/yyy.csv

输入必须包含 ts_utc、open、high、low、close、volume。若存在
publisher_id、instrument_id、symbol，则它们会共同作为合约键，避免不同合约
落入同一根 K 线。其余列保留为每个五分钟区间第一条真实记录的值。
"""

from __future__ import annotations

import argparse
import csv
import heapq
import os
import tempfile
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterator, Sequence


TIMESTAMP_COLUMN = "ts_utc"
OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")
POSSIBLE_GROUP_COLUMNS = ("publisher_id", "instrument_id", "symbol")
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S%z"
SORT_CHUNK_ROWS = 100_000


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="将一分钟 CSV 聚合为五分钟 K 线。")
    parser.add_argument("-i", "--input", required=True, help="输入的一分钟 CSV 路径")
    parser.add_argument("-o", "--output", required=True, help="输出的五分钟 CSV 路径")
    return parser.parse_args()


def validate_header(fieldnames: Sequence[str]) -> tuple[dict[str, int], tuple[str, ...]]:
    duplicates = sorted({name for name in fieldnames if fieldnames.count(name) > 1})
    if duplicates:
        raise SystemExit("输入 CSV 表头包含重复字段：" + ", ".join(duplicates))

    column_index = {name: index for index, name in enumerate(fieldnames)}
    required = (TIMESTAMP_COLUMN, *OHLCV_COLUMNS)
    missing = [name for name in required if name not in column_index]
    if missing:
        raise SystemExit("输入 CSV 缺少必要字段：" + ", ".join(missing))
    group_columns = tuple(name for name in POSSIBLE_GROUP_COLUMNS if name in column_index)
    return column_index, group_columns


def sort_key(row: Sequence[str], column_index: dict[str, int], group_columns: Sequence[str]) -> tuple[str, ...]:
    """返回稳定且可比较的排序键；ts_utc 为同一 UTC 格式，字符串排序即时间排序。"""
    return tuple(row[column_index[column]] for column in group_columns) + (
        row[column_index[TIMESTAMP_COLUMN]],
    )


def inspect_source(
    input_path: Path, encoding: str
) -> tuple[list[str], dict[str, int], tuple[str, ...], bool]:
    with input_path.open("r", encoding=encoding, newline="") as source:
        reader = csv.reader(source)
        try:
            fieldnames = next(reader)
        except StopIteration as error:
            raise SystemExit("输入 CSV 为空。") from error
        column_index, group_columns = validate_header(fieldnames)
        expected_length = len(fieldnames)
        previous_key: tuple[str, ...] | None = None
        for row_number, row in enumerate(reader, start=2):
            if len(row) != expected_length:
                raise SystemExit(f"第 {row_number:,} 行的列数与表头不一致。")
            current_key = sort_key(row, column_index, group_columns)
            if previous_key is not None and current_key < previous_key:
                return fieldnames, column_index, group_columns, False
            previous_key = current_key
    return fieldnames, column_index, group_columns, True


def write_sorted_chunks(
    input_path: Path,
    fieldnames: Sequence[str],
    column_index: dict[str, int],
    group_columns: Sequence[str],
    temporary_dir: Path,
    encoding: str,
) -> list[Path]:
    """对大文件做分块外部排序，避免一次性将全部数据装入内存。"""
    chunk_paths: list[Path] = []
    expected_length = len(fieldnames)
    rows: list[tuple[tuple[str, ...], int, list[str]]] = []

    def flush_chunk() -> None:
        if not rows:
            return
        rows.sort(key=lambda item: (item[0], item[1]))
        chunk_path = temporary_dir / f"chunk_{len(chunk_paths):04d}.csv"
        with chunk_path.open("w", encoding=encoding, newline="") as chunk_file:
            writer = csv.writer(chunk_file)
            writer.writerow([*fieldnames, "__source_order"])
            for _, source_order, row in rows:
                writer.writerow([*row, source_order])
        chunk_paths.append(chunk_path)
        rows.clear()

    with input_path.open("r", encoding=encoding, newline="") as source:
        reader = csv.reader(source)
        next(reader)  # 表头已在预检阶段验证。
        for row_number, row in enumerate(reader, start=2):
            if len(row) != expected_length:
                raise SystemExit(f"第 {row_number:,} 行的列数与表头不一致。")
            rows.append((sort_key(row, column_index, group_columns), row_number, row))
            if len(rows) >= SORT_CHUNK_ROWS:
                flush_chunk()
                print(f"已创建 {len(chunk_paths):,} 个临时排序分块…")
        flush_chunk()
    return chunk_paths


def iter_sorted_rows(
    chunk_paths: Sequence[Path],
    fieldnames: Sequence[str],
    column_index: dict[str, int],
    group_columns: Sequence[str],
    encoding: str,
) -> Iterator[list[str]]:
    """用多路归并读取外部排序后的记录。"""
    readers: list[csv.reader] = []
    files = []
    heap: list[tuple[tuple[str, ...], int, int, list[str]]] = []
    expected_length = len(fieldnames) + 1

    try:
        for file_index, chunk_path in enumerate(chunk_paths):
            chunk_file = chunk_path.open("r", encoding=encoding, newline="")
            files.append(chunk_file)
            reader = csv.reader(chunk_file)
            next(reader)  # 临时文件表头
            readers.append(reader)
            try:
                row_with_order = next(reader)
            except StopIteration:
                continue
            if len(row_with_order) != expected_length:
                raise SystemExit(f"临时排序文件损坏：{chunk_path}")
            row, order_text = row_with_order[:-1], row_with_order[-1]
            heapq.heappush(
                heap,
                (sort_key(row, column_index, group_columns), int(order_text), file_index, row),
            )

        while heap:
            _, _, file_index, row = heapq.heappop(heap)
            yield row
            try:
                row_with_order = next(readers[file_index])
            except StopIteration:
                continue
            if len(row_with_order) != expected_length:
                raise SystemExit(f"临时排序文件损坏：{chunk_paths[file_index]}")
            next_row, order_text = row_with_order[:-1], row_with_order[-1]
            heapq.heappush(
                heap,
                (
                    sort_key(next_row, column_index, group_columns),
                    int(order_text),
                    file_index,
                    next_row,
                ),
            )
    finally:
        for chunk_file in files:
            chunk_file.close()


def parse_timestamp(value: str, row_number: int) -> datetime:
    try:
        return datetime.strptime(value, TIMESTAMP_FORMAT)
    except ValueError as error:
        raise SystemExit(f"第 {row_number:,} 行的 {TIMESTAMP_COLUMN} 无效：{value!r}") from error


def parse_number(value: str, column: str, row_number: int) -> Decimal:
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise SystemExit(f"第 {row_number:,} 行的 {column} 不是有效数值：{value!r}") from error
    if not number.is_finite():
        raise SystemExit(f"第 {row_number:,} 行的 {column} 必须是有限数值：{value!r}")
    return number


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def five_minute_start(timestamp: datetime) -> datetime:
    return timestamp.replace(minute=timestamp.minute - timestamp.minute % 5, second=0, microsecond=0)


def format_timestamp(timestamp: datetime) -> str:
    return timestamp.strftime("%Y-%m-%d %H:%M:%S%z")[:-2] + ":" + timestamp.strftime("%z")[-2:]


class Bar:
    def __init__(self, row: list[str], timestamp: datetime, column_index: dict[str, int]) -> None:
        self.first_row = row.copy()
        self.bucket_start = five_minute_start(timestamp)
        self.open = row[column_index["open"]]
        self.high = parse_number(row[column_index["high"]], "high", 0)
        self.low = parse_number(row[column_index["low"]], "low", 0)
        self.close = row[column_index["close"]]
        self.volume = parse_number(row[column_index["volume"]], "volume", 0)

    def add(self, row: list[str], column_index: dict[str, int], row_number: int) -> None:
        self.high = max(self.high, parse_number(row[column_index["high"]], "high", row_number))
        self.low = min(self.low, parse_number(row[column_index["low"]], "low", row_number))
        self.close = row[column_index["close"]]
        self.volume += parse_number(row[column_index["volume"]], "volume", row_number)

    def output_row(self, column_index: dict[str, int]) -> list[str]:
        output = self.first_row.copy()
        output[column_index[TIMESTAMP_COLUMN]] = format_timestamp(self.bucket_start)
        output[column_index["open"]] = self.open
        output[column_index["high"]] = decimal_text(self.high)
        output[column_index["low"]] = decimal_text(self.low)
        output[column_index["close"]] = self.close
        output[column_index["volume"]] = decimal_text(self.volume)
        return output


def aggregate(
    rows: Iterator[list[str]],
    output_path: Path,
    fieldnames: Sequence[str],
    column_index: dict[str, int],
    group_columns: Sequence[str],
    encoding: str,
) -> tuple[int, int]:
    temporary_output = output_path.with_name(f".{output_path.name}.tmp")
    source_rows = 0
    output_rows = 0
    current_bar: Bar | None = None
    current_group: tuple[str, ...] | None = None

    try:
        with temporary_output.open("w", encoding=encoding, newline="") as output_file:
            writer = csv.writer(output_file)
            writer.writerow(fieldnames)
            for source_rows, row in enumerate(rows, start=1):
                timestamp = parse_timestamp(row[column_index[TIMESTAMP_COLUMN]], source_rows)
                group = tuple(row[column_index[column]] for column in group_columns)
                bucket_start = five_minute_start(timestamp)
                if current_bar is None or group != current_group or bucket_start != current_bar.bucket_start:
                    if current_bar is not None:
                        writer.writerow(current_bar.output_row(column_index))
                        output_rows += 1
                    current_bar = Bar(row, timestamp, column_index)
                    current_group = group
                else:
                    current_bar.add(row, column_index, source_rows)

                if source_rows % 1_000_000 == 0:
                    print(f"已聚合 {source_rows:,} 条一分钟记录…")

            if current_bar is not None:
                writer.writerow(current_bar.output_row(column_index))
                output_rows += 1
        os.replace(temporary_output, output_path)
    except BaseException:
        temporary_output.unlink(missing_ok=True)
        raise
    return source_rows, output_rows


def main() -> None:
    args = parse_arguments()
    input_path = Path(args.input).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    if not input_path.is_file():
        raise SystemExit(f"未找到输入文件：{input_path}")
    if input_path == output_path:
        raise SystemExit("输入文件和输出文件不能相同。")

    encoding = "utf-8-sig"
    fieldnames, column_index, group_columns, source_is_sorted = inspect_source(input_path, encoding)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if group_columns:
        print("合约分组字段：" + ", ".join(group_columns))
    else:
        print("未检测到合约标识字段：将所有记录视为同一品种。")

    with tempfile.TemporaryDirectory(prefix="nq_1to5_") as temporary_directory_name:
        temporary_directory = Path(temporary_directory_name)
        if source_is_sorted:
            print("输入已排序，直接聚合。")
            def source_rows() -> Iterator[list[str]]:
                with input_path.open("r", encoding=encoding, newline="") as source:
                    reader = csv.reader(source)
                    next(reader)
                    yield from reader
            row_iterator = source_rows()
        else:
            print("输入未排序，正在进行磁盘临时排序（大文件可能需要一些时间）。")
            chunk_paths = write_sorted_chunks(
                input_path, fieldnames, column_index, group_columns, temporary_directory, encoding
            )
            row_iterator = iter_sorted_rows(
                chunk_paths, fieldnames, column_index, group_columns, encoding
            )

        input_rows, output_rows = aggregate(
            row_iterator, output_path, fieldnames, column_index, group_columns, encoding
        )

    print("转换完成")
    print(f"输入：{input_path}")
    print(f"输出：{output_path}")
    print(f"一分钟记录：{input_rows:,}")
    print(f"五分钟 K 线：{output_rows:,}")


if __name__ == "__main__":
    main()
