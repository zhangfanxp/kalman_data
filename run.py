#!/usr/bin/env python3
"""识别五分钟 K 线市场结构并生成带买卖参考位的图表。

基本用法：
    python run.py -i 路径/xxx.csv -o 路径/输出目录

程序会从包含多个合约的数据中自动选择时间最新的合约；也可用 --symbol
指定合约。输出 SVG 文件名为 market_structure_YYYYMMDD_HHMMSS_NNNN.svg，
其中 NNNN 是输出目录中当日的递增流水号。
"""

from __future__ import annotations

import argparse
import csv
from html import escape
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator, Sequence


TIMESTAMP_ALIASES = ("ts_utc", "ts_event", "timestamp", "datetime", "date_time", "time", "date")
PRICE_ALIASES = {
    "open": ("open", "o"),
    "high": ("high", "h"),
    "low": ("low", "l"),
    "close": ("close", "c", "last"),
}
GROUP_COLUMNS = ("publisher_id", "instrument_id", "symbol")
FRACTIONAL_SECONDS = re.compile(r"(\.\d{6})\d+")
OUTPUT_PREFIX = "market_structure"


@dataclass(frozen=True)
class Columns:
    timestamp: str
    open: str
    high: str
    low: str
    close: str
    group: tuple[str, ...]
    symbol: str | None


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class Pivot:
    index: int
    kind: str  # "high" or "low"
    price: float
    label: str
    confirmed_at: int


@dataclass(frozen=True)
class BreakEvent:
    index: int
    direction: int  # 1: 向上突破，-1: 向下跌破
    level: float


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="识别 K 线市场结构、趋势、支撑阻力及趋势转折点，并输出 SVG 图表。"
    )
    parser.add_argument("-i", "--input", required=True, help="输入 5 分钟 OHLC/CSV 文件")
    parser.add_argument("-o", "--output", required=True, help="SVG 输出目录")
    parser.add_argument(
        "--symbol", help="可选：只分析指定 symbol；省略时自动选择时间最新的合约"
    )
    parser.add_argument(
        "--bars", type=int, default=240, help="图中展示的最近 K 线数（默认：240）"
    )
    parser.add_argument(
        "--left", type=int, default=5, help="摆动点左侧确认 K 线数（默认：5）"
    )
    parser.add_argument(
        "--right", type=int, default=5, help="摆动点右侧确认 K 线数（默认：5）"
    )
    args = parser.parse_args()
    if args.bars < 30:
        parser.error("--bars 必须至少为 30。")
    if args.left < 1 or args.right < 1:
        parser.error("--left 与 --right 均必须至少为 1。")
    return args


def normalised_headers(fieldnames: Sequence[str]) -> dict[str, str]:
    """返回小写规范名到 CSV 原始字段名的映射（兼容 UTF-8 BOM）。"""
    result: dict[str, str] = {}
    for name in fieldnames:
        key = name.lstrip("\ufeff").strip().lower()
        if key in result:
            raise ValueError(f"CSV 表头在忽略大小写后重复：{name}")
        result[key] = name
    return result


def first_present(headers: dict[str, str], aliases: Iterable[str], label: str) -> str:
    for alias in aliases:
        if alias in headers:
            return headers[alias]
    raise ValueError(f"输入 CSV 缺少 {label} 字段；可识别字段：{', '.join(sorted(headers.values()))}")


def discover_columns(input_path: Path) -> Columns:
    with input_path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if not reader.fieldnames:
            raise ValueError("输入 CSV 为空或没有表头。")
        headers = normalised_headers(reader.fieldnames)
    group = tuple(headers[column] for column in GROUP_COLUMNS if column in headers)
    return Columns(
        timestamp=first_present(headers, TIMESTAMP_ALIASES, "时间"),
        open=first_present(headers, PRICE_ALIASES["open"], "open"),
        high=first_present(headers, PRICE_ALIASES["high"], "high"),
        low=first_present(headers, PRICE_ALIASES["low"], "low"),
        close=first_present(headers, PRICE_ALIASES["close"], "close"),
        group=group,
        symbol=headers.get("symbol"),
    )


def parse_timestamp(value: str) -> datetime:
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    # datetime.fromisoformat 仅支持最多 6 位小数；Databento 时间戳常有 9 位。
    value = FRACTIONAL_SECONDS.sub(r"\1", value)
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"无法解析时间 {value!r}") from error


def group_key(row: dict[str, str], group_columns: Sequence[str]) -> tuple[str, ...]:
    return tuple((row.get(column) or "").strip() for column in group_columns)


def rows(input_path: Path) -> Iterator[tuple[int, dict[str, str]]]:
    with input_path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        for line_number, row in enumerate(reader, start=2):
            if None in row:
                raise ValueError(f"第 {line_number:,} 行的列数多于表头。")
            yield line_number, row


def choose_target_group(input_path: Path, columns: Columns, symbol: str | None) -> tuple[str, ...]:
    """选择目标合约。默认选择全文件中时间最新的记录所在的合约。"""
    latest_timestamp: datetime | None = None
    selected: tuple[str, ...] | None = None
    matching_rows = 0

    for line_number, row in rows(input_path):
        if symbol and (columns.symbol is None or row.get(columns.symbol, "").strip() != symbol):
            continue
        try:
            timestamp = parse_timestamp(row[columns.timestamp])
        except (KeyError, ValueError) as error:
            raise ValueError(f"第 {line_number:,} 行时间无效：{error}") from error
        matching_rows += 1
        if latest_timestamp is None or timestamp > latest_timestamp:
            latest_timestamp = timestamp
            selected = group_key(row, columns.group)

    if not matching_rows:
        suffix = f"（symbol={symbol!r}）" if symbol else ""
        raise ValueError(f"输入 CSV 中没有可分析的数据{suffix}。")
    assert selected is not None
    return selected


def read_target_bars(input_path: Path, columns: Columns, selected: tuple[str, ...]) -> list[Bar]:
    result: list[Bar] = []
    invalid_rows = 0
    for line_number, row in rows(input_path):
        if group_key(row, columns.group) != selected:
            continue
        try:
            bar = Bar(
                timestamp=parse_timestamp(row[columns.timestamp]),
                open=float(row[columns.open]),
                high=float(row[columns.high]),
                low=float(row[columns.low]),
                close=float(row[columns.close]),
            )
            if not (bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high):
                raise ValueError("OHLC 价格关系不成立")
        except (KeyError, TypeError, ValueError) as error:
            invalid_rows += 1
            if invalid_rows <= 3:
                print(f"跳过第 {line_number:,} 行：{error}", file=sys.stderr)
            continue
        result.append(bar)

    # 同一时间若重复，以文件中最后一条有效记录为准。
    ordered = sorted(enumerate(result), key=lambda item: (item[1].timestamp, item[0]))
    deduplicated: dict[datetime, Bar] = {bar.timestamp: bar for _, bar in ordered}
    bars = [deduplicated[timestamp] for timestamp in sorted(deduplicated)]
    if len(bars) < 2:
        raise ValueError("目标合约的有效 K 线少于两根。")
    if invalid_rows:
        print(f"已跳过 {invalid_rows:,} 条无效 OHLC 记录。", file=sys.stderr)
    return bars


def raw_pivots(bars: Sequence[Bar], left: int, right: int) -> list[tuple[int, str, float]]:
    """使用左右确认窗口发现已确认的局部高低点。"""
    found: list[tuple[int, str, float]] = []
    for index in range(left, len(bars) - right):
        highs = [bar.high for bar in bars[index - left : index + right + 1]]
        lows = [bar.low for bar in bars[index - left : index + right + 1]]
        high = bars[index].high
        low = bars[index].low
        # 一端严格、一端允许相等，使平台高低点只保留第一个，降低重复标记。
        is_high = high > max(highs[:left]) and high >= max(highs[left + 1 :])
        is_low = low < min(lows[:left]) and low <= min(lows[left + 1 :])
        if is_high:
            found.append((index, "high", high))
        elif is_low:
            found.append((index, "low", low))
    return found


def build_pivots(bars: Sequence[Bar], left: int, right: int) -> list[Pivot]:
    """将局部点规整为交替的 ZigZag 点，并标记 HH/HL/LH/LL。"""
    alternating: list[tuple[int, str, float]] = []
    for candidate in raw_pivots(bars, left, right):
        if not alternating:
            alternating.append(candidate)
            continue
        previous = alternating[-1]
        if candidate[1] == previous[1]:
            more_extreme = candidate[2] > previous[2] if candidate[1] == "high" else candidate[2] < previous[2]
            if more_extreme:
                alternating[-1] = candidate
        else:
            alternating.append(candidate)

    previous_high: float | None = None
    previous_low: float | None = None
    pivots: list[Pivot] = []
    for index, kind, price in alternating:
        if kind == "high":
            label = "HH" if previous_high is None or price > previous_high else "LH"
            previous_high = price
        else:
            label = "HL" if previous_low is None or price > previous_low else "LL"
            previous_low = price
        pivots.append(Pivot(index, kind, price, label, index + right))
    return pivots


def structure_state(bars: Sequence[Bar], pivots: Sequence[Pivot]) -> tuple[int, float | None, float | None, list[BreakEvent]]:
    """根据已确认摆动点及收盘突破，返回当前趋势、支撑、阻力和转折事件。"""
    by_confirmation: dict[int, list[Pivot]] = {}
    for pivot in pivots:
        by_confirmation.setdefault(pivot.confirmed_at, []).append(pivot)

    trend = 0
    support: float | None = None
    resistance: float | None = None
    events: list[BreakEvent] = []
    for index, bar in enumerate(bars):
        for pivot in by_confirmation.get(index, []):
            if pivot.kind == "high":
                resistance = pivot.price
            else:
                support = pivot.price
        if resistance is not None and bar.close > resistance and trend != 1:
            trend = 1
            events.append(BreakEvent(index, 1, resistance))
        elif support is not None and bar.close < support and trend != -1:
            trend = -1
            events.append(BreakEvent(index, -1, support))

    # 如尚未完成价格突破，则用最近两个同类摆动点推断当前结构方向。
    if trend == 0 and len(pivots) >= 2:
        last_labels = {pivot.label for pivot in pivots[-4:]}
        if "HH" in last_labels or "HL" in last_labels:
            trend = 1
        elif "LH" in last_labels or "LL" in last_labels:
            trend = -1
    return trend, support, resistance, events


def price_text(price: float) -> str:
    return f"{price:,.2f}".rstrip("0").rstrip(".")


def next_output_path(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    day = now.strftime("%Y%m%d")
    pattern = re.compile(rf"^{re.escape(OUTPUT_PREFIX)}_{day}_\d{{6}}_(\d{{4}})\.svg$")
    sequence = 0
    for path in output_dir.iterdir():
        match = pattern.match(path.name)
        if match:
            sequence = max(sequence, int(match.group(1)))
    return output_dir / f"{OUTPUT_PREFIX}_{day}_{now.strftime('%H%M%S')}_{sequence + 1:04d}.svg"


def plot_chart(
    bars: Sequence[Bar],
    pivots: Sequence[Pivot],
    trend: int,
    support: float | None,
    resistance: float | None,
    events: Sequence[BreakEvent],
    output_path: Path,
    shown_bars: int,
    group: Sequence[str],
) -> None:
    """生成无需第三方库、可在浏览器直接打开的 SVG K 线图。"""
    display_start = max(0, len(bars) - shown_bars)
    displayed = bars[display_start:]
    width, height = 1800, 1000
    left, right, top, bottom = 95, 45, 105, 105
    chart_width, chart_height = width - left - right, height - top - bottom
    price_values = [bar.low for bar in displayed] + [bar.high for bar in displayed]
    if support is not None:
        price_values.append(support)
    if resistance is not None:
        price_values.append(resistance)
    minimum, maximum = min(price_values), max(price_values)
    price_range = maximum - minimum or max(abs(maximum) * 0.02, 1.0)
    minimum -= price_range * 0.10
    maximum += price_range * 0.10
    price_range = maximum - minimum

    def x_at(index: int) -> float:
        return left + index * chart_width / max(len(displayed) - 1, 1)

    def y_at(price: float) -> float:
        return top + (maximum - price) * chart_height / price_range

    content: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#f8fafc"/>',
        f'<rect x="{left}" y="{top}" width="{chart_width}" height="{chart_height}" fill="#ffffff" stroke="#cbd5e1"/>',
        '<style>text{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,"Microsoft YaHei",sans-serif}.small{font-size:13px;fill:#475569}.label{font-size:12px;font-weight:700}.title{font-size:24px;font-weight:700}</style>',
    ]
    for level in range(7):
        price = minimum + price_range * level / 6
        y = y_at(price)
        content.append(f'<line x1="{left}" y1="{y:.2f}" x2="{width-right}" y2="{y:.2f}" stroke="#e2e8f0" stroke-width="1"/>')
        content.append(f'<text x="{left-10}" y="{y+4:.2f}" text-anchor="end" class="small">{escape(price_text(price))}</text>')

    candle_width = max(1.0, min(10.0, chart_width / max(len(displayed), 1) * 0.62))
    for index, bar in enumerate(displayed):
        x = x_at(index)
        color = "#158f68" if bar.close >= bar.open else "#dc4c64"
        content.append(f'<line x1="{x:.2f}" y1="{y_at(bar.low):.2f}" x2="{x:.2f}" y2="{y_at(bar.high):.2f}" stroke="{color}" stroke-width="1.2"/>')
        body_y = y_at(max(bar.open, bar.close))
        body_height = abs(y_at(bar.open) - y_at(bar.close))
        if body_height < 1:
            content.append(f'<line x1="{x-candle_width/2:.2f}" y1="{body_y:.2f}" x2="{x+candle_width/2:.2f}" y2="{body_y:.2f}" stroke="{color}" stroke-width="2"/>')
        else:
            content.append(f'<rect x="{x-candle_width/2:.2f}" y="{body_y:.2f}" width="{candle_width:.2f}" height="{body_height:.2f}" fill="{color}"/>')

    def horizontal_reference(price: float, color: str, text: str) -> None:
        y = y_at(price)
        content.append(f'<line x1="{left}" y1="{y:.2f}" x2="{width-right}" y2="{y:.2f}" stroke="{color}" stroke-width="1.5" stroke-dasharray="7 5"/>')
        label_width = min(315, max(160, len(text) * 7.2))
        label_y = max(top + 2, min(y - 20, height - bottom - 26))
        content.append(f'<rect x="{width-right-label_width:.2f}" y="{label_y:.2f}" width="{label_width:.2f}" height="22" rx="4" fill="white" stroke="{color}"/>')
        content.append(f'<text x="{width-right-7:.2f}" y="{label_y+15:.2f}" text-anchor="end" class="small" style="fill:{color}">{escape(text)}</text>')

    if support is not None:
        horizontal_reference(support, "#16a34a", f"支撑 / 买入参考  {price_text(support)}")
    if resistance is not None:
        horizontal_reference(resistance, "#dc2626", f"阻力 / 卖出参考  {price_text(resistance)}")

    for pivot in (pivot for pivot in pivots if pivot.index >= display_start):
        x, y = x_at(pivot.index - display_start), y_at(pivot.price)
        is_high = pivot.kind == "high"
        color = "#15803d" if pivot.label in {"HH", "HL"} else "#b91c1c"
        label_y = y - 12 if is_high else y + 20
        content.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="3.5" fill="{color}" stroke="white" stroke-width="1"/>')
        content.append(f'<text x="{x:.2f}" y="{label_y:.2f}" text-anchor="middle" class="label" fill="{color}">{pivot.label}</text>')

    for event in (event for event in events if event.index >= display_start):
        x, y = x_at(event.index - display_start), y_at(event.level)
        color = "#2563eb" if event.direction == 1 else "#ea580c"
        label = "向上突破" if event.direction == 1 else "向下跌破"
        points = f"{x:.2f},{y-9:.2f} {x-7:.2f},{y+5:.2f} {x+7:.2f},{y+5:.2f}" if event.direction == 1 else f"{x:.2f},{y+9:.2f} {x-7:.2f},{y-5:.2f} {x+7:.2f},{y-5:.2f}"
        label_y = y - 18 if event.direction == 1 else y + 25
        content.append(f'<polygon points="{points}" fill="{color}" stroke="white" stroke-width="1"/>')
        content.append(f'<text x="{x:.2f}" y="{label_y:.2f}" text-anchor="middle" class="label" fill="{color}">{label}</text>')

    trend_name = {1: "上升趋势", -1: "下降趋势", 0: "震荡 / 未确认"}[trend]
    trend_color = {1: "#15803d", -1: "#b91c1c", 0: "#475569"}[trend]
    group_text = " | ".join(value for value in group if value) or "单一序列"
    content.append(f'<text x="{left}" y="42" class="title" fill="{trend_color}">市场结构 · {trend_name} · {escape(group_text)}</text>')
    subtitle = f"最新收盘：{price_text(displayed[-1].close)}　|　已确认摆动点：{len(pivots)}　|　图中 K 线：{len(displayed)}"
    content.append(f'<text x="{left}" y="70" class="small">{escape(subtitle)}</text>')
    tick_count = min(10, max(4, len(displayed) // 25))
    for position in {round(index * (len(displayed) - 1) / (tick_count - 1)) for index in range(tick_count)}:
        x = x_at(position)
        stamp = displayed[position].timestamp.strftime("%m-%d %H:%M")
        content.append(f'<line x1="{x:.2f}" y1="{height-bottom}" x2="{x:.2f}" y2="{height-bottom+6}" stroke="#64748b"/>')
        content.append(f'<text x="{x:.2f}" y="{height-bottom+25}" text-anchor="middle" class="small">{stamp}</text>')
    content.extend([f'<text x="{left}" y="{height-28}" class="small">HH = 更高高点　HL = 更高低点　LH = 更低高点　LL = 更低低点</text>', "</svg>"])
    output_path.write_text("\n".join(content), encoding="utf-8")


def main() -> int:
    args = parse_arguments()
    input_path = Path(args.input).expanduser()
    output_dir = Path(args.output).expanduser()
    if not input_path.is_file():
        print(f"错误：找不到输入文件：{input_path}", file=sys.stderr)
        return 2
    try:
        columns = discover_columns(input_path)
        selected_group = choose_target_group(input_path, columns, args.symbol)
        bars = read_target_bars(input_path, columns, selected_group)
        pivots = build_pivots(bars, args.left, args.right)
        trend, support, resistance, events = structure_state(bars, pivots)
        output_path = next_output_path(output_dir)
        plot_chart(bars, pivots, trend, support, resistance, events, output_path, args.bars, selected_group)
    except (OSError, ValueError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2

    trend_text = {1: "上升趋势", -1: "下降趋势", 0: "震荡 / 未确认"}[trend]
    print(f"已生成：{output_path}")
    print(f"分析合约：{' | '.join(selected_group) or '单一序列'}；有效 K 线：{len(bars):,}；当前结构：{trend_text}")
    if support is not None:
        print(f"支撑 / 买入参考：{price_text(support)}")
    if resistance is not None:
        print(f"阻力 / 卖出参考：{price_text(resistance)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
