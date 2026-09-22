#!/usr/bin/env python3
"""NQ IMM-Kalman 可视化界面。

启动：
    python3 app.py

浏览器打开终端显示的本地地址后，上传 OHLCV CSV 即可运行。结果数据默认保存至
Data_view，图表默认保存至 Chart；两者均可在页面中改为其他目录。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import gradio as gr
import pandas as pd

from nq_imm_kalman import calculate_indicator, read_bars, resolve_columns, save_chart


DEFAULT_DATA_DIRECTORY = Path("Data_view")
DEFAULT_CHART_DIRECTORY = Path("Chart")


def run_indicator(
    uploaded_csv: str | None,
    symbol: str,
    start: str,
    end: str,
    tail: int,
    velocity_observation: str,
    atr_scale: str,
    adaptive_r: bool,
    cascade_corrector: bool,
    data_directory: str,
    chart_directory: str,
) -> tuple[str, str, str, pd.DataFrame]:
    """读取上传文件、运行计算，并返回说明、图像、CSV 下载和结果预览。"""
    if not uploaded_csv:
        raise gr.Error("请先上传 CSV 文件。")
    if tail is not None and tail < 2:
        raise gr.Error("最近 K 线数必须至少为 2。")
    try:
        source = Path(uploaded_csv)
        columns = resolve_columns(source)
        bars = read_bars(source, columns, symbol.strip() or None)
        if start.strip():
            bars = bars.loc[bars["timestamp"] >= pd.to_datetime(start.strip(), utc=True)]
        if end.strip():
            bars = bars.loc[bars["timestamp"] <= pd.to_datetime(end.strip(), utc=True)]
        if tail:
            bars = bars.tail(int(tail))
        bars = bars.reset_index(drop=True)
        if len(bars) < 2:
            raise ValueError("筛选后的有效 K 线少于两根。")

        result = calculate_indicator(
            bars,
            velocity_observation,
            atr_scale,
            adaptive_r,
            cascade_corrector,
        )
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_stem = "".join(character if character.isalnum() or character in "-_" else "_" for character in source.stem)
        data_dir = Path(data_directory.strip() or DEFAULT_DATA_DIRECTORY)
        chart_dir = Path(chart_directory.strip() or DEFAULT_CHART_DIRECTORY)
        data_dir.mkdir(parents=True, exist_ok=True)
        chart_dir.mkdir(parents=True, exist_ok=True)
        csv_path = data_dir / f"{safe_stem}_kalman_{timestamp}.csv"
        chart_path = chart_dir / f"{safe_stem}_kalman_{timestamp}.png"
        result.to_csv(csv_path, index=False, encoding="utf-8-sig")
        save_chart(result, chart_path)

        latest = result.iloc[-1]
        direction = "多头" if latest["css_direction"] > 0 else "空头" if latest["css_direction"] < 0 else "中性"
        message = (
            f"完成：计算 {len(result):,} 根 K 线。最新 CSS 为 **{direction}**，"
            f"持续 {int(latest['css_count'])} 根；MA（反转模型）概率为 {latest['mu_ma']:.1%}。\n\n"
            f"结果 CSV：`{csv_path}`  \n图表 PNG：`{chart_path}`"
        )
        preview_columns = [
            "timestamp", "open", "high", "low", "close", "position", "velocity",
            "acceleration", "mu_mv", "mu_ma", "css_direction", "css_count",
            "fingerprint_6bit", "bull_flip", "bear_flip",
        ]
        return message, str(chart_path), str(csv_path), result[preview_columns].tail(200)
    except gr.Error:
        raise
    except Exception as error:
        raise gr.Error(f"无法完成计算：{error}") from error


with gr.Blocks(title="NQ IMM-Kalman 可视化") as demo:
    gr.Markdown(
        "# NQ 7 阶 IMM-Kalman / CSS 方向\n"
        "上传含 `ts_utc`（或其他可识别时间列）、`open`、`high`、`low`、`close` 的 CSV。"
        "`volume` 可选；没有 `symbol` 列时请保留合约代码为空。"
    )
    with gr.Row():
        with gr.Column(scale=1):
            uploaded_csv = gr.File(label="输入 CSV", file_types=[".csv"], type="filepath")
            symbol = gr.Textbox(label="合约代码（可选）", placeholder="例如 NQU6；没有 symbol 列则留空")
            with gr.Row():
                start = gr.Textbox(label="开始时间（可选）", placeholder="2026-07-01T00:00:00Z")
                end = gr.Textbox(label="结束时间（可选）", placeholder="2026-07-24T23:59:59Z")
            tail = gr.Number(label="最近 K 线数（0 表示全部）", value=10000, precision=0, minimum=0)
            velocity_observation = gr.Dropdown(
                label="速度观测", choices=["blend", "midpoint", "ohlc4", "clv"], value="blend"
            )
            atr_scale = gr.Radio(label="ATR 缩放", choices=["squared", "linear"], value="squared")
            adaptive_r = gr.Checkbox(label="启用创新自适应 R", value=True)
            cascade_corrector = gr.Checkbox(label="启用 CSS 级联翻转校正", value=True)
            with gr.Accordion("输出位置", open=False):
                data_directory = gr.Textbox(label="结果 CSV 目录", value=str(DEFAULT_DATA_DIRECTORY))
                chart_directory = gr.Textbox(label="图表 PNG 目录", value=str(DEFAULT_CHART_DIRECTORY))
            run_button = gr.Button("运行计算", variant="primary")
        with gr.Column(scale=1):
            status = gr.Markdown("等待上传数据。")
            chart = gr.Image(label="IMM-Kalman 图表", type="filepath")
            csv_download = gr.File(label="下载结果 CSV")
    preview = gr.Dataframe(label="最近 200 根计算结果", interactive=False, max_height=460)
    run_button.click(
        run_indicator,
        inputs=[
            uploaded_csv, symbol, start, end, tail, velocity_observation, atr_scale,
            adaptive_r, cascade_corrector, data_directory, chart_directory,
        ],
        outputs=[status, chart, csv_download, preview],
    )


if __name__ == "__main__":
    demo.launch()
