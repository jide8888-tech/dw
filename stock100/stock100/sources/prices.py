"""시세 데이터 소스.

- KrxSource   : pykrx (KRX 정보데이터시스템, KRX_ID/KRX_PW 로그인 필요) — 과거 날짜 포함 전종목
- NaverSource : 네이버 금융 상한가/거래량 상위 페이지 — 로그인 불필요, '오늘'만 가능
- 일봉(OHLCV)은 공통으로 네이버 fchart 를 사용하고 실패하면 pykrx 로 대체
"""
from __future__ import annotations

import datetime as dt
import io
import re

import pandas as pd

from ..util import http_get, to_date, ymd

COLUMNS = ["code", "name", "market", "open", "high", "low", "close",
           "change_pct", "volume", "value", "marcap"]


def _num(x):
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    s = re.sub(r"[^0-9.\-]", "", str(x))
    if s in ("", "-", "."):
        return None
    return float(s)


# ---------------------------------------------------------------- 일봉
def naver_ohlcv(code: str, count: int = 400) -> pd.DataFrame:
    url = "https://fchart.stock.naver.com/sise.nhn"
    xml = http_get(url, params={"symbol": code, "timeframe": "day",
                                "count": count, "requestType": 0}, encoding="euc-kr")
    rows = []
    for m in re.finditer(r'data="(\d{8})\|([\d.]+)\|([\d.]+)\|([\d.]+)\|([\d.]+)\|(\d+)"', xml):
        d, o, h, l, c, v = m.groups()
        rows.append((pd.Timestamp(d), float(o), float(h), float(l), float(c), int(v)))
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
    return df.set_index("date")


def krx_ohlcv(code: str, start, end) -> pd.DataFrame:
    from pykrx import stock
    df = stock.get_market_ohlcv(ymd(start), ymd(end), code)
    df = df.rename(columns={"시가": "open", "고가": "high", "저가": "low",
                            "종가": "close", "거래량": "volume"})
    df.index.name = "date"
    return df[["open", "high", "low", "close", "volume"]]


class BaseSource:
    name = "base"

    def movers(self, date, f: dict) -> pd.DataFrame:
        """f = config filters. 상한가 / 천만주 / 대금상위(거래대금·등락률) 중 하나라도 해당하는 종목"""
        raise NotImplementedError

    def ohlcv(self, code: str, start, end) -> pd.DataFrame:
        start, end = to_date(start), to_date(end)
        days = (dt.date.today() - start).days
        try:
            df = naver_ohlcv(code, count=max(60, int(days * 0.75) + 30))
        except Exception:
            df = krx_ohlcv(code, start, end)
        return df.loc[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]

    def trading_days(self, end, n: int) -> list[dt.date]:
        """end 이전(포함) n 거래일. 삼성전자 일봉으로 판단."""
        end = to_date(end)
        df = self.ohlcv("005930", end - dt.timedelta(days=int(n * 1.6) + 20), end)
        return [d.date() for d in df.index[-n:]]


FLAGS = ["is_upper", "is_volume", "is_hot"]


def _filter(df: pd.DataFrame, f: dict) -> pd.DataFrame:
    df = df.copy()
    if "value" not in df or df["value"].isna().all():
        df["value"] = df["close"] * df["volume"]          # 거래대금 근사치
    else:
        df["value"] = df["value"].fillna(df["close"] * df["volume"])
    df["is_upper"] = df["change_pct"] >= f["upper_limit_pct"]
    df["is_volume"] = df["volume"] >= f["volume_min"]
    df["is_hot"] = (df["value"] >= f.get("value_min_eok", 100) * 1e8) & \
                   (df["change_pct"] >= f.get("hot_pct_min", 10.0))
    return df[df["is_upper"] | df["is_volume"] | df["is_hot"]].reset_index(drop=True)


class KrxSource(BaseSource):
    name = "krx"

    def __init__(self, markets):
        self.markets = markets

    def movers(self, date, f):
        from pykrx import stock
        d = ymd(date)
        frames = []
        for m in self.markets:
            o = stock.get_market_ohlcv_by_ticker(d, market=m)
            if o.empty or (o[["시가", "고가", "저가", "종가"]] == 0).all(axis=None):
                continue  # 휴장일
            cap = stock.get_market_cap(d, market=m)
            o = o.join(cap[["시가총액"]], how="left")
            o["market"] = m
            frames.append(o)
        if not frames:
            return pd.DataFrame(columns=COLUMNS + FLAGS)
        df = pd.concat(frames).reset_index()
        df = df.rename(columns={df.columns[0]: "code", "시가": "open", "고가": "high",
                                "저가": "low", "종가": "close", "거래량": "volume",
                                "거래대금": "value", "등락률": "change_pct", "시가총액": "marcap"})
        df = _filter(df, f)
        df["name"] = [stock.get_market_ticker_name(c) for c in df["code"]]
        return df[COLUMNS + FLAGS]


class NaverSource(BaseSource):
    """네이버 금융 — 당일(최근 거래일) 데이터만 제공."""
    name = "naver"
    LINK = re.compile(r'href="/item/main\.n(?:aver|hn)\?code=(\d{6})"[^>]*>([^<]+)</a>')

    def _tables(self, url):
        html = http_get(url, encoding="euc-kr")
        names = {}
        for code, nm in self.LINK.findall(html):
            names.setdefault(nm.strip(), code)
        tables = [t for t in pd.read_html(io.StringIO(html)) if "종목명" in t.columns]
        return tables, names

    def _norm(self, t, names, market):
        t = t.dropna(subset=["종목명"])
        out = pd.DataFrame({
            "name": t["종목명"].astype(str).str.strip(),
            "market": market,
            "close": t["현재가"].map(_num),
            "change_pct": t["등락률"].map(_num),
            "volume": t["거래량"].map(_num),
        })
        out["open"] = t["시가"].map(_num) if "시가" in t else None
        out["high"] = t["고가"].map(_num) if "고가" in t else None
        out["low"] = t["저가"].map(_num) if "저가" in t else None
        out["value"] = t["거래대금"].map(_num) * 1e6 if "거래대금" in t else None   # 백만원
        out["marcap"] = t["시가총액"].map(_num) * 1e8 if "시가총액" in t else None  # 억원
        out["code"] = out["name"].map(names)
        return out.dropna(subset=["code", "close"])

    def movers(self, date, f):
        if to_date(date) != dt.date.today():
            raise RuntimeError("네이버 소스는 오늘 데이터만 제공합니다. 과거 날짜는 KRX_ID/KRX_PW 를 설정하세요.")
        frames = []
        tables, names = self._tables("https://finance.naver.com/sise/sise_upper.naver")
        for i, t in enumerate(tables):
            frames.append(self._norm(t, names, "KOSPI" if i == 0 else "KOSDAQ"))
        for sosok, m in ((0, "KOSPI"), (1, "KOSDAQ")):
            # 거래량 상위 + 상승 종목(등락률순). 상승 페이지엔 거래대금이 없어 종가×거래량으로 근사
            for page in ("sise_quant", "sise_rise"):
                try:
                    tables, names = self._tables(f"https://finance.naver.com/sise/{page}.naver?sosok={sosok}")
                except Exception as e:
                    print(f"[warn] 네이버 {page} 실패: {e}")
                    continue
                for t in tables:
                    frames.append(self._norm(t, names, m))
        df = pd.concat(frames, ignore_index=True)
        df = df.drop_duplicates("code", keep="last")
        df = _filter(df, f)
        return df[COLUMNS + FLAGS]


class AutoSource(BaseSource):
    """과거 날짜면 KRX, 오늘이면 KRX → 실패 시 네이버."""
    name = "auto"

    def __init__(self, markets):
        self.krx = KrxSource(markets)
        self.naver = NaverSource()

    def movers(self, date, f):
        try:
            return self.krx.movers(date, f)
        except Exception as e:
            if to_date(date) == dt.date.today():
                print(f"[info] KRX 실패({e.__class__.__name__}) → 네이버 금융 사용")
                return self.naver.movers(date, f)
            raise


def make_source(cfg):
    kind = cfg["sources"]["prices"]
    if kind == "demo":
        from .demo import DemoPriceSource
        return DemoPriceSource()
    if kind == "krx":
        return KrxSource(cfg["markets"])
    if kind == "naver":
        return NaverSource()
    return AutoSource(cfg["markets"])
