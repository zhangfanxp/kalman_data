#!/usr/bin/env python3
"""NQ 7 阶 IMM-Kalman / CSS 方向指标。

这是对 ``kalman代码.txt`` 中核心滤波链的 Python 实现：两个 7 阶运动
模型（MV 趋势、MA 反转）经 IMM 混合，分别以价格、速度和加速度观测更新，
再由平滑速度和 ATR 阈值输出 CSS 方向。结果写入 CSV；可选 PNG 图表。

示例：
    python nq_imm_kalman.py -i 5min/raw_clean12.csv -o Data_view/nq_kalman.csv \
        --tail 10000 --chart Data_view/nq_kalman.png
    python nq_imm_kalman.py -i raw-1m.csv -o Data_view/nq_kalman.csv --symbol NQU6

输入文件需要时间、open、high、low、close 字段；volume 与 symbol 为可选字段。
未指定 --symbol 时，会自动选择时间最新的合约。大文件采用分块读取，以避免
一次将原始分钟数据全部载入内存。
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


TIME_ALIASES = ("ts_utc", "ts_event", "timestamp", "datetime", "date_time", "time", "date")
PRICE_ALIASES = {
    "open": ("open", "o"),
    "high": ("high", "h"),
    "low": ("low", "l"),
    "close": ("close", "c", "last"),
    "volume": ("volume", "vol", "size"),
}
GROUP_COLUMNS = ("publisher_id", "instrument_id", "symbol")
EPS = 1.0e-10


@dataclass(frozen=True)
class ModelParameters:
    q: tuple[float, float, float, float, float, float, float]
    r_price: float
    r_vel: float
    r_acc: float
    r_acc_likelihood: float


MV = ModelParameters((0.020, 0.050, 0.010, 0.002, 0.0004, 0.00008, 0.000016), 0.400, 0.500, 5.0, 0.5)
MA = ModelParameters((0.020, 0.150, 0.030, 0.006, 0.0012, 0.00024, 0.000048), 0.400, 5.3194, 50.0, 5.3)


@dataclass
class FilterModel:
    x: np.ndarray
    p: np.ndarray


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行 NQ 7 阶 IMM-Kalman 与 CSS 方向计算。")
    parser.add_argument("-i", "--input", required=True, type=Path, help="输入 OHLCV CSV")
    parser.add_argument("-o", "--output", required=True, type=Path, help="输出结果 CSV")
    parser.add_argument("--symbol", help="指定合约；省略时自动选时间最新的合约")
    parser.add_argument("--start", help="起始时间（例如 2026-07-01T00:00:00Z）")
    parser.add_argument("--end", help="结束时间（含）")
    parser.add_argument("--tail", type=int, help="只计算筛选后最近 N 根 K 线，便于快速试跑")
    parser.add_argument("--chart", type=Path, help="可选：输出 PNG 图表路径")
    parser.add_argument("--velocity-observation", choices=("midpoint", "ohlc4", "clv", "blend"), default="blend")
    parser.add_argument("--atr-scale", choices=("squared", "linear"), default="squared")
    parser.add_argument("--no-adaptive-r", action="store_true", help="关闭创新自适应观测噪声")
    parser.add_argument("--no-corrector", action="store_true", help="关闭 CSS Cascade Fingerprint 翻转否决")
    args = parser.parse_args()
    if args.tail is not None and args.tail < 2:
        parser.error("--tail 必须至少为 2。")
    return args


def resolve_columns(path: Path) -> dict[str, str | None]:
    header = pd.read_csv(path, nrows=0, encoding="utf-8-sig").columns.tolist()
    lowered = {name.lstrip("\ufeff").strip().lower(): name for name in header}

    def first(aliases: Iterable[str], required: bool = True) -> str | None:
        for alias in aliases:
            if alias in lowered:
                return lowered[alias]
        if required:
            raise ValueError(f"输入 CSV 缺少字段 {tuple(aliases)}；实际表头：{header}")
        return None

    columns: dict[str, str | None] = {"time": first(TIME_ALIASES)}
    for name in ("open", "high", "low", "close"):
        columns[name] = first(PRICE_ALIASES[name])
    columns["volume"] = first(PRICE_ALIASES["volume"], required=False)
    for name in GROUP_COLUMNS:
        columns[name] = lowered.get(name)
    return columns


def read_bars(path: Path, columns: dict[str, str | None], symbol: str | None) -> pd.DataFrame:
    """两遍分块读取：先确定合约，再只保留它的有效 OHLC 行。"""
    requested = list(dict.fromkeys(value for value in columns.values() if value is not None))
    chosen_group: tuple[object, ...] | None = None
    latest_time: pd.Timestamp | None = None
    chunks = pd.read_csv(path, usecols=requested, chunksize=250_000, encoding="utf-8-sig", low_memory=False)
    group_names = [columns[name] for name in GROUP_COLUMNS if columns[name] is not None]
    symbol_col = columns["symbol"]

    for chunk in chunks:
        times = pd.to_datetime(chunk[columns["time"]], utc=True, errors="coerce")
        mask = times.notna()
        if symbol is not None:
            if symbol_col is None:
                raise ValueError("输入数据没有 symbol 字段，不能使用 --symbol。")
            mask &= chunk[symbol_col].astype(str).eq(symbol)
        if not mask.any():
            continue
        index = times[mask].idxmax()
        candidate_time = times.loc[index]
        if latest_time is None or candidate_time > latest_time:
            latest_time = candidate_time
            chosen_group = tuple(chunk.loc[index, name] for name in group_names)
    if chosen_group is None:
        suffix = f"（symbol={symbol}）" if symbol else ""
        raise ValueError(f"没有找到可分析的数据{suffix}。")

    result: list[pd.DataFrame] = []
    chunks = pd.read_csv(path, usecols=requested, chunksize=250_000, encoding="utf-8-sig", low_memory=False)
    for chunk in chunks:
        mask = pd.Series(True, index=chunk.index)
        for name, value in zip(group_names, chosen_group):
            mask &= chunk[name].eq(value)
        selected = chunk.loc[mask].copy()
        if not selected.empty:
            result.append(selected)
    if not result:
        raise ValueError("未能读取选定合约的数据。")

    bars = pd.concat(result, ignore_index=True)
    rename = {columns["time"]: "timestamp", columns["open"]: "open", columns["high"]: "high", columns["low"]: "low", columns["close"]: "close"}
    if columns["volume"]:
        rename[columns["volume"]] = "volume"
    if symbol_col:
        rename[symbol_col] = "symbol"
    bars = bars.rename(columns=rename)
    bars["timestamp"] = pd.to_datetime(bars["timestamp"], utc=True, errors="coerce")
    for name in ("open", "high", "low", "close"):
        bars[name] = pd.to_numeric(bars[name], errors="coerce")
    if "volume" in bars:
        bars["volume"] = pd.to_numeric(bars["volume"], errors="coerce").fillna(1.0).clip(lower=0.0)
    else:
        bars["volume"] = 1.0
    valid = bars[["timestamp", "open", "high", "low", "close"]].notna().all(axis=1)
    valid &= bars["low"] <= bars[["open", "close"]].min(axis=1)
    valid &= bars[["open", "close"]].max(axis=1) <= bars["high"]
    bars = bars.loc[valid].sort_values("timestamp").drop_duplicates("timestamp", keep="last").reset_index(drop=True)
    if len(bars) < 2:
        raise ValueError("有效 OHLC K 线少于两根。")
    return bars


def ema(values: np.ndarray, period: int) -> np.ndarray:
    alpha = 2.0 / (period + 1.0)
    result = np.empty(len(values), dtype=float)
    result[0] = values[0]
    for index in range(1, len(values)):
        result[index] = result[index - 1] + alpha * (values[index] - result[index - 1])
    return result


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    tr = np.empty(len(close), dtype=float)
    tr[0] = high[0] - low[0]
    for index in range(1, len(close)):
        tr[index] = max(high[index] - low[index], abs(high[index] - close[index - 1]), abs(low[index] - close[index - 1]))
    result = np.empty(len(close), dtype=float)
    result[0] = tr[0]
    for index in range(1, len(close)):
        result[index] = (result[index - 1] * (period - 1) + tr[index]) / period
    return result


def transition_matrix() -> np.ndarray:
    matrix = np.zeros((7, 7), dtype=float)
    for row in range(7):
        for column in range(row, 7):
            matrix[row, column] = 1.0 / float(math.factorial(column - row))
    return matrix


def initial_model(price: float) -> FilterModel:
    return FilterModel(np.array([price, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]), np.diag([1000.0, 1000.0, 1000.0, 100.0, 100.0, 100.0, 100.0]))


def mix_models(models: tuple[FilterModel, FilterModel], weights: np.ndarray, transition: np.ndarray) -> tuple[tuple[FilterModel, FilterModel], np.ndarray]:
    predicted_weights = transition.T @ weights
    mixed: list[FilterModel] = []
    for destination in range(2):
        probabilities = transition[:, destination] * weights / max(predicted_weights[destination], EPS)
        state = probabilities[0] * models[0].x + probabilities[1] * models[1].x
        covariance = sum(probabilities[source] * (models[source].p + np.outer(models[source].x - state, models[source].x - state)) for source in range(2))
        mixed.append(FilterModel(state, (covariance + covariance.T) * 0.5))
    return (mixed[0], mixed[1]), predicted_weights


def kalman_update(model: FilterModel, z: float, h: np.ndarray, r: float, likelihood_r: float | None = None) -> tuple[FilterModel, float]:
    innovation = z - float(h @ model.x)
    variance = max(float(h @ model.p @ h + r), EPS)
    gain = (model.p @ h) / variance
    state = model.x + gain * innovation
    # Joseph form preserves a positive semi-definite covariance after long runs.
    identity = np.eye(7)
    kh = np.outer(gain, h)
    covariance = (identity - kh) @ model.p @ (identity - kh).T + np.outer(gain, gain) * r
    likelihood_variance = max(float(h @ model.p @ h + (r if likelihood_r is None else likelihood_r)), EPS)
    return FilterModel(state, (covariance + covariance.T) * 0.5), -0.5 * (innovation * innovation / likelihood_variance + np.log(2.0 * np.pi * likelihood_variance))


def step_model(model: FilterModel, parameters: ModelParameters, q_vel_scale: float, r_price: float, r_vel: float, r_acc: float, r_acc_likelihood: float, z0: float, z1: float, z2: float, f: np.ndarray) -> tuple[FilterModel, float]:
    q = np.asarray(parameters.q, dtype=float)
    q[1] *= q_vel_scale
    predicted = FilterModel(f @ model.x, f @ model.p @ f.T + np.diag(q))
    updated, log_likelihood = kalman_update(predicted, z0, np.array([1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]), r_price)
    updated, ll = kalman_update(updated, z1, np.array([0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0]), r_vel)
    log_likelihood += ll
    # The reference uses a separate acceleration noise for model likelihood.
    updated, ll = kalman_update(updated, z2, np.array([0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0]), r_acc, r_acc_likelihood)
    log_likelihood += ll
    return updated, log_likelihood


def calculate_indicator(bars: pd.DataFrame, velocity_mode: str, atr_scale_mode: str, adaptive_r: bool, corrector: bool) -> pd.DataFrame:
    open_ = bars["open"].to_numpy(float)
    high = bars["high"].to_numpy(float)
    low = bars["low"].to_numpy(float)
    close = bars["close"].to_numpy(float)
    volume = bars["volume"].to_numpy(float)
    count = len(bars)
    midpoint = (high + low) * 0.5
    ohlc4 = (open_ + high + low + close) * 0.25
    atr_value = np.maximum(atr(high, low, close), 0.0001)
    atr_reference = np.maximum(ema(atr_value, 200), 0.0001)
    volume_average = np.maximum(pd.Series(volume).rolling(20, min_periods=1).mean().to_numpy(), 1.0)
    volume_mass = np.maximum(np.log1p(volume / volume_average) / np.log(2.0), 0.1)
    midpoint_velocity = np.diff(midpoint, prepend=midpoint[0])
    ohlc_velocity = np.diff(ohlc4, prepend=ohlc4[0])
    clv_velocity = 2.0 * (close - midpoint)
    velocity_observation = {"midpoint": midpoint_velocity, "ohlc4": ohlc_velocity, "clv": clv_velocity, "blend": 0.5 * (midpoint_velocity + clv_velocity)}[velocity_mode]
    acceleration_observation = np.diff(velocity_observation, prepend=velocity_observation[0])
    velocity_scale_50 = ema(np.abs(velocity_observation), 50)
    f = transition_matrix()
    imm_transition = np.array([[0.7837, 0.2163], [0.5730, 0.4270]])  # rows: source, columns: destination
    models = (initial_model(midpoint[0]), initial_model(midpoint[0]))
    weights = np.array([0.5, 0.5])
    innovation_state = 0.0
    nis_state = 0.0
    previous_smoothed_velocity = 0.0
    previous_css = 0
    css = 0
    css_count = 0
    bull_sustain = bear_sustain = 0
    columns = {name: np.empty(count, dtype=float) for name in (
        "position", "velocity", "acceleration", "jerk", "snap", "crackle", "pop", "mu_mv", "mu_ma", "observation_trust", "r_price_inflation", "r_velocity_inflation", "css_direction", "css_count", "css_raw", "flip_gauge", "fingerprint_6bit", "corrected_direction", "bull_flip", "bear_flip"
    )}
    velocity_values = np.empty(count, dtype=float)
    acceleration_values = np.empty(count, dtype=float)
    higher_values = np.empty((count, 3), dtype=float)

    for index in range(count):
        mixed, predicted_weights = mix_models(models, weights, imm_transition)
        predicted_x = predicted_weights[0] * mixed[0].x[0] + predicted_weights[1] * mixed[1].x[0]
        predicted_v = predicted_weights[0] * mixed[0].x[1] + predicted_weights[1] * mixed[1].x[1]
        price_innovation = abs(midpoint[index] - predicted_x) / atr_value[index]
        velocity_scale = max(velocity_scale_50[index], atr_value[index] * 0.10)
        velocity_innovation = abs(velocity_observation[index] - predicted_v) / velocity_scale
        nis_energy = price_innovation * price_innovation + velocity_innovation * velocity_innovation
        if index:
            relative_change = abs(nis_energy - nis_state) / (1.0 + abs(nis_state))
            alpha = 0.04 + 0.70 * relative_change / (1.0 + relative_change)
            innovation_state += alpha * (max(price_innovation, velocity_innovation) - innovation_state)
            nis_state += alpha * (nis_energy - nis_state)
        else:
            innovation_state, nis_state = max(price_innovation, velocity_innovation), nis_energy
        trust = 1.0 / (1.0 + 0.10 * nis_state)
        price_inflation = 1.0 / max(trust, 1.0e-6) if adaptive_r else 1.0
        velocity_inflation = np.sqrt(price_inflation) if adaptive_r else 1.0
        atr_ratio = atr_value[index] / atr_reference[index]
        scale = atr_ratio if atr_scale_mode == "linear" else atr_ratio * atr_ratio
        args = []
        for model, parameters in zip(mixed, (MV, MA)):
            args.append(step_model(model, parameters, scale, parameters.r_price * price_inflation, parameters.r_vel * scale / volume_mass[index] * velocity_inflation, parameters.r_acc * scale, parameters.r_acc_likelihood * scale, midpoint[index], velocity_observation[index], acceleration_observation[index], f))
        models = (args[0][0], args[1][0])
        log_weights = np.log(np.maximum(predicted_weights, EPS)) + np.array([args[0][1], args[1][1]])
        log_weights -= np.max(log_weights)
        weights = np.exp(log_weights)
        weights /= weights.sum()
        state = weights[0] * models[0].x + weights[1] * models[1].x
        covariance = sum(weights[source] * (models[source].p + np.outer(models[source].x - state, models[source].x - state)) for source in range(2))
        velocity_values[index], acceleration_values[index], higher_values[index] = state[1], state[2], state[3:6]
        smooth_velocity = previous_smoothed_velocity + (2.0 / 6.0) * (state[1] - previous_smoothed_velocity) if index else state[1]
        previous_smoothed_velocity = smooth_velocity
        threshold = atr_value[index] * 0.02
        if smooth_velocity < -threshold:
            bear_sustain, bull_sustain = bear_sustain + 1, 0
        elif smooth_velocity > threshold:
            bull_sustain, bear_sustain = bull_sustain + 1, 0
        else:
            bull_sustain = bear_sustain = 0
        if css == 0:
            css = 1 if smooth_velocity > 0.0 else -1 if smooth_velocity < 0.0 else 0
        elif css == 1 and bear_sustain >= 3:
            css, bear_sustain = -1, 0
        elif css == -1 and bull_sustain >= 3:
            css, bull_sustain = 1, 0
        css_raw = css
        # v5.3 Cascade Fingerprint Corrector: incomplete higher-order cascade vetoes a flip.
        if corrector and previous_css and css and css != previous_css and state[6] * previous_css > 0 and state[5] * previous_css > 0 and state[1] * previous_css < 0:
            css = previous_css
            bull_sustain = bear_sustain = 0
        if css == previous_css or previous_css == 0:
            css_count += 1
        elif css:
            css_count = 1
        prior_velocity = velocity_values[index - 1] if index else 0.0
        prior_jerk = higher_values[index - 1, 0] if index else 0.0
        bull_signal = int(higher_values[index, 2] > 0 and higher_values[index, 1] > 0 and state[3] > 0 and state[2] < 0 and state[1] < 0 and state[3] > prior_jerk and prior_velocity < 0)
        bear_signal = int(higher_values[index, 2] < 0 and higher_values[index, 1] < 0 and state[3] < 0 and state[2] > 0 and state[1] > 0 and state[3] < prior_jerk and prior_velocity > 0)
        fingerprint = sum(bit for bit, value in zip((32, 16, 8, 4, 2, 1), (state[6], state[5], state[4], state[3], state[2], state[1])) if value > 0)
        columns["position"][index] = state[0]
        columns["velocity"][index] = state[1]
        columns["acceleration"][index] = state[2]
        columns["jerk"][index], columns["snap"][index], columns["crackle"][index], columns["pop"][index] = state[3], state[4], state[5], state[6]
        columns["mu_mv"][index], columns["mu_ma"][index] = weights[0], weights[1]
        columns["observation_trust"][index], columns["r_price_inflation"][index], columns["r_velocity_inflation"][index] = trust, price_inflation, velocity_inflation
        columns["css_direction"][index], columns["css_count"][index], columns["css_raw"][index] = css, css_count, css_raw
        columns["flip_gauge"][index] = (smooth_velocity + threshold) / max(threshold, EPS) if css > 0 else (threshold - smooth_velocity) / max(threshold, EPS)
        columns["fingerprint_6bit"][index], columns["corrected_direction"][index] = fingerprint, bull_signal - bear_signal
        columns["bull_flip"][index], columns["bear_flip"][index] = int(css > 0 and previous_css <= 0), int(css < 0 and previous_css >= 0)
        previous_css = css

    result = bars.copy()
    result["atr_14"] = atr_value
    result["velocity_observation"] = velocity_observation
    result["velocity_ema_5"] = ema(velocity_values, 5)
    for name, values in columns.items():
        result[name] = values
    return result


def save_chart(result: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    figure, (price_axis, signal_axis) = plt.subplots(2, 1, figsize=(15, 9), sharex=True, height_ratios=(3, 2))
    price_axis.plot(result["timestamp"], result["close"], color="#aaaaaa", linewidth=0.8, label="Close")
    price_axis.plot(result["timestamp"], result["position"], color="#00aaff", linewidth=1.1, label="IMM-KF position")
    price_axis.legend(loc="upper left")
    price_axis.set_title("NQ 7D IMM-Kalman")
    signal_axis.plot(result["timestamp"], result["velocity_ema_5"], color="#6f8cff", linewidth=0.9, label="Velocity EMA(5)")
    signal_axis.step(result["timestamp"], result["css_direction"], where="post", color="#00a85a", linewidth=1.0, label="CSS direction")
    signal_axis.axhline(0.0, color="#777777", linewidth=0.6)
    signal_axis.legend(loc="upper left")
    signal_axis.set_ylabel("signal")
    figure.tight_layout()
    figure.savefig(path, dpi=160)
    plt.close(figure)


def main() -> None:
    args = parse_args()
    columns = resolve_columns(args.input)
    bars = read_bars(args.input, columns, args.symbol)
    if args.start:
        bars = bars.loc[bars["timestamp"] >= pd.to_datetime(args.start, utc=True)]
    if args.end:
        bars = bars.loc[bars["timestamp"] <= pd.to_datetime(args.end, utc=True)]
    if args.tail:
        bars = bars.tail(args.tail)
    bars = bars.reset_index(drop=True)
    if len(bars) < 2:
        raise ValueError("时间筛选后的 K 线少于两根。")
    selected_symbol = bars["symbol"].iloc[0] if "symbol" in bars else "未标识"
    print(f"正在计算 {selected_symbol}：{len(bars):,} 根 K 线。")
    result = calculate_indicator(bars, args.velocity_observation, args.atr_scale, not args.no_adaptive_r, not args.no_corrector)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(args.output, index=False, encoding="utf-8-sig")
    if args.chart:
        save_chart(result, args.chart)
    latest = result.iloc[-1]
    direction = "多头" if latest.css_direction > 0 else "空头" if latest.css_direction < 0 else "中性"
    print(f"已写入 {args.output}；最新 CSS：{direction}，持续 {int(latest.css_count)} 根，MA 概率 {latest.mu_ma:.1%}。")
    if args.chart:
        print(f"图表已写入 {args.chart}。")


if __name__ == "__main__":
    main()
