"""일봉 + 이동평균 + 거래량 차트. 상한가/천만주 날짜는 노란 세로선 (책의 프로텍 차트처럼)."""
from __future__ import annotations

from pathlib import Path

import logging

import matplotlib
matplotlib.use("Agg")
logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402
import pandas as pd  # noqa: E402

_FONT_SET = False


def _korean_font():
    global _FONT_SET
    if _FONT_SET:
        return
    names = {f.name for f in font_manager.fontManager.ttflist}
    for cand in ("Malgun Gothic", "AppleGothic", "NanumGothic", "Noto Sans CJK KR",
                 "Noto Sans KR", "WenQuanYi Zen Hei"):
        if cand in names:
            plt.rcParams["font.family"] = cand
            break
    plt.rcParams["axes.unicode_minus"] = False
    _FONT_SET = True


def draw(df: pd.DataFrame, title: str, event_dates: list, path: Path) -> Path | None:
    if df is None or df.empty:
        return None
    _korean_font()
    df = df.copy()
    x = range(len(df))
    fig, (ax, axv) = plt.subplots(2, 1, figsize=(11, 6), sharex=True,
                                  gridspec_kw={"height_ratios": [3, 1]})
    up = df["close"] >= df["open"]
    col = ["#d62728" if u else "#1f77b4" for u in up]
    ax.vlines(x, df["low"], df["high"], color=col, linewidth=0.8)
    ax.bar(x, (df["close"] - df["open"]).abs().clip(lower=df["close"] * 0.001),
           bottom=df[["open", "close"]].min(axis=1), color=col, width=0.7)
    for n, c in ((5, "#e377c2"), (20, "#ff7f0e"), (60, "#2ca02c"), (120, "#7f7f7f")):
        if len(df) >= n:
            ax.plot(x, df["close"].rolling(n).mean(), linewidth=1, color=c, label=f"MA{n}")
    axv.bar(x, df["volume"], color=col, width=0.7)
    idx = {d.date(): i for i, d in enumerate(df.index)}
    for d in event_dates:
        i = idx.get(pd.Timestamp(d).date())
        if i is not None:
            ax.axvline(i, color="#f5c518", alpha=0.6, linewidth=3)
            axv.axvline(i, color="#f5c518", alpha=0.6, linewidth=3)
    ticks = list(range(0, len(df), max(1, len(df) // 8)))
    axv.set_xticks(ticks)
    axv.set_xticklabels([df.index[i].strftime("%y-%m-%d") for i in ticks], fontsize=8)
    from matplotlib.ticker import FuncFormatter
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
    axv.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v / 1e4:,.0f}만"))
    ax.set_title(title)
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(alpha=0.2)
    axv.grid(alpha=0.2)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=90)
    plt.close(fig)
    return path
