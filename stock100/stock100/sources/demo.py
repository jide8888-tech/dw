"""오프라인 데모 데이터 — 네트워크 없이 전체 흐름(복습→예습→추적→모의매매)을 검증하기 위한 가짜 시장."""
from __future__ import annotations

import datetime as dt
import hashlib

import numpy as np
import pandas as pd

from .news import Article
from .prices import BaseSource, COLUMNS, _filter
from ..util import to_date

UNIVERSE = [
    ("053610", "프로텍", "KOSDAQ", "반도체 장비 갱본더 개발 삼성전자 공급"),
    ("332290", "누보", "KOSDAQ", "비료 농업"),
    ("241820", "피씨엘", "KOSDAQ", "진단키트"),
    ("027830", "대성창투", "KOSDAQ", "벤처캐피탈 투자"),
    ("000890", "보해양조", "KOSPI", "소주 주류"),
    ("194480", "데브시스터즈", "KOSDAQ", "게임 쿠키런 신작"),
    ("263750", "펄어비스", "KOSDAQ", "게임 붉은사막 신작"),
    ("245620", "EDGC", "KOSDAQ", "바이오 유전체"),
    ("091990", "셀트리온헬스케어", "KOSDAQ", "바이오시밀러"),
    ("241770", "메카로", "KOSDAQ", "반도체 HBM 소재"),
    ("033640", "네패스", "KOSDAQ", "반도체 패키징"),
    ("005930", "삼성전자", "KOSPI", "반도체"),
]
NAMES = {c: n for c, n, _, _ in UNIVERSE}
THEMES = {c: t for c, _, _, t in UNIVERSE}


def _seed(*parts) -> int:
    return int(hashlib.md5("|".join(map(str, parts)).encode()).hexdigest()[:8], 16)


def _bdays(end: dt.date, n: int):
    days, d = [], end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= dt.timedelta(days=1)
    return days[::-1]


class DemoPriceSource(BaseSource):
    name = "demo"

    def _series(self, code: str, end: dt.date, n: int = 400) -> pd.DataFrame:
        days = _bdays(max(end, dt.date.today()), n)
        rng = np.random.default_rng(_seed(code))
        rets = rng.normal(-0.004, 0.022, len(days))
        for i, d in enumerate(days):        # 주기적으로 상한가가 반복되는 테마 (책: '반복'을 체감)
            if _seed(code, d) % 23 == 0:
                rets[i] = 0.2995
        close = 10000 * np.cumprod(1 + rets)
        close = np.round(close, -1)
        prev = np.concatenate([[close[0]], close[:-1]])
        op = np.round(prev * (1 + rng.normal(0, 0.008, len(days))), -1)
        hi = np.maximum(op, close) * (1 + np.abs(rng.normal(0, 0.012, len(days))))
        lo = np.minimum(op, close) * (1 - np.abs(rng.normal(0, 0.012, len(days))))
        vol = (rng.lognormal(14, 0.8, len(days)) * (1 + 30 * (rets > 0.2))).astype(int)
        return pd.DataFrame({"open": op, "high": np.round(hi, -1), "low": np.round(lo, -1),
                             "close": close, "volume": vol, "change_pct": rets * 100},
                            index=pd.DatetimeIndex(days, name="date"))

    def ohlcv(self, code, start, end):
        df = self._series(code, to_date(end))
        df = df[(df.index >= pd.Timestamp(to_date(start))) & (df.index <= pd.Timestamp(to_date(end)))]
        return df[["open", "high", "low", "close", "volume"]]

    def trading_days(self, end, n):
        return _bdays(to_date(end), n)

    def movers(self, date, f):
        d = pd.Timestamp(to_date(date))
        rows = []
        for code, name, market, _ in UNIVERSE:
            s = self._series(code, to_date(date))
            if d not in s.index:
                continue
            r = s.loc[d]
            rows.append({"code": code, "name": name, "market": market, "open": r.open,
                         "high": r.high, "low": r.low, "close": r.close,
                         "change_pct": round(r.change_pct, 2), "volume": int(r.volume),
                         "value": int(r.volume * r.close), "marcap": int(r.close * 3e6)})
        return _filter(pd.DataFrame(rows, columns=COLUMNS), f)


    def value_top(self, date, n=100):
        d = pd.Timestamp(to_date(date))
        rows = []
        for code, name, market, _ in UNIVERSE:
            s = self._series(code, to_date(date))
            if d in s.index:
                r = s.loc[d]
                rows.append({"code": code, "name": name, "market": market, "close": r.close,
                             "change_pct": round(r.change_pct, 2), "value": float(r.volume * r.close)})
        df = pd.DataFrame(rows).sort_values("value", ascending=False).head(n).reset_index(drop=True)
        df["rank"] = range(1, len(df) + 1)
        return df


class DemoNews:
    def search(self, query, around=None, days_before=3, limit=20):
        code = next((c for c, n in NAMES.items() if n == query), None)
        theme = THEMES.get(code, "")
        d = to_date(around or dt.date.today()).isoformat()
        partner = "삼성전자" if "반도체" in theme else ("대성창투" if code == "245620" else "")
        arts = [Article(f"[특징주] {query}, {theme.split()[0]} 기대감에 상한가", f"https://demo/{code}/{d}/1", d, "데모경제"),
                Article(f"{query} {theme} 부각… 거래량 급증", f"https://demo/{code}/{d}/2", d, "데모일보")]
        if partner:
            arts.append(Article(f"{query}, {partner}와 협력 확대", f"https://demo/{code}/{d}/3", d, "데모뉴스"))
        return arts

    def headlines(self, queries):
        d = dt.date.today().isoformat()
        return [Article("삼성전자, 차세대 HBM 패키징 투자 확대", "https://demo/h/1", d, "데모경제"),
                Article("게임업계 대작 신작 출시 러시", "https://demo/h/2", d, "데모일보"),
                Article("정부, 농업 비료 수급 대책 발표", "https://demo/h/3", d, "데모뉴스"),
                Article("환율 안정에 외국인 순매수", "https://demo/h/4", d, "데모뉴스")]
