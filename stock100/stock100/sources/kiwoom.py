"""키움증권 REST API (https://openapi.kiwoom.com) 시세 소스.

- 인증: POST /oauth2/token {grant_type, appkey, secretkey} → Bearer 토큰 (파일에 캐시)
- 순위: /api/dostk/rkinfo  ka10032 거래대금상위, ka10027 등락률상위, ka10030 당일거래량상위
- 종목정보: /api/dostk/stkinfo  ka10099 종목 리스트(상장주식수 → 시가총액)
- 일봉: /api/dostk/chart  ka10081 주식일봉차트
순위 API 는 '오늘(실시간)'만 제공하므로 과거 날짜 movers 는 KRX 로 넘긴다.
주문 API 는 사용하지 않는다(조회 전용).
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from pathlib import Path

import pandas as pd
import requests

from ..util import to_date, ymd
from .prices import COLUMNS, BaseSource, _filter

REAL = "https://api.kiwoom.com"
MOCK = "https://mockapi.kiwoom.com"
MARKETS = {"KOSPI": ("001", "0"), "KOSDAQ": ("101", "10")}   # (순위 mrkt_tp, ka10099 mrkt_tp)


def _num(s) -> float | None:
    """'+12,345' / '-1.23' / '000123' → 숫자. 키움은 부호로 등락 방향을 표시하므로 가격은 abs 처리."""
    if s is None:
        return None
    t = re.sub(r"[^0-9.\-+]", "", str(s))
    if t in ("", "+", "-", "."):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _code(s: str) -> str:
    s = str(s).strip().split("_")[0]
    if len(s) == 7 and s[0] == "A":
        s = s[1:]
    return s


def _enough(n):
    """연속조회 누적 n건 이상이면 중단"""
    acc = []

    def stop(page):
        acc.extend(page)
        return len(acc) >= n
    return stop


class KiwoomError(RuntimeError):
    pass


class KiwoomClient:
    def __init__(self, appkey: str, secretkey: str, cache_dir: Path, mock: bool = False, min_interval=0.25):
        if not (appkey and secretkey):
            raise KiwoomError("키움 appkey/secretkey 가 없습니다 (config keys.kiwoom_appkey / kiwoom_secretkey).")
        self.appkey, self.secretkey = appkey, secretkey
        self.base = MOCK if mock else REAL
        self.cache = cache_dir / ("kiwoom_token_mock.json" if mock else "kiwoom_token.json")
        self.s = requests.Session()
        self._token = None
        self._exp = None
        self._last = 0.0
        self.min_interval = min_interval

    # ---------------- 인증
    def token(self) -> str:
        now = dt.datetime.now()
        if self._token and self._exp and now < self._exp - dt.timedelta(minutes=5):
            return self._token
        if self.cache.exists():
            try:
                c = json.loads(self.cache.read_text(encoding="utf-8"))
                exp = dt.datetime.strptime(c["expires_dt"], "%Y%m%d%H%M%S")
                if c.get("appkey") == self.appkey[-6:] and now < exp - dt.timedelta(minutes=5):
                    self._token, self._exp = c["token"], exp
                    return self._token
            except Exception:
                pass
        r = self.s.post(f"{self.base}/oauth2/token", timeout=15,
                        headers={"Content-Type": "application/json;charset=UTF-8"},
                        json={"grant_type": "client_credentials", "appkey": self.appkey,
                              "secretkey": self.secretkey})
        data = r.json() if r.content else {}
        if r.status_code != 200 or not data.get("token"):
            raise KiwoomError(f"토큰 발급 실패 (HTTP {r.status_code}): {data.get('return_msg') or r.text[:200]}")
        self._token = data["token"]
        exp = data.get("expires_dt")
        self._exp = dt.datetime.strptime(exp, "%Y%m%d%H%M%S") if exp else now + dt.timedelta(hours=12)
        self.cache.write_text(json.dumps({"token": self._token, "expires_dt": self._exp.strftime("%Y%m%d%H%M%S"),
                                          "appkey": self.appkey[-6:]}), encoding="utf-8")
        return self._token

    # ---------------- 공통 호출 (연속조회 지원)
    def call(self, path: str, api_id: str, body: dict, max_pages: int = 1, stop=None) -> list[dict]:
        rows, cont, key = [], "N", ""
        for _ in range(max_pages):
            wait = self.min_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            headers = {"Content-Type": "application/json;charset=UTF-8", "api-id": api_id,
                       "authorization": f"Bearer {self.token()}", "cont-yn": cont, "next-key": key}
            r = self.s.post(f"{self.base}{path}", headers=headers, json=body, timeout=20)
            self._last = time.time()
            if r.status_code == 429:
                time.sleep(1.5)
                continue
            data = r.json() if r.content else {}
            if r.status_code != 200 or data.get("return_code", 0) not in (0, "0"):
                raise KiwoomError(f"{api_id} 실패 (HTTP {r.status_code}): {data.get('return_msg') or r.text[:200]}")
            page = next((v for v in data.values() if isinstance(v, list)), [])
            rows += page
            cont = r.headers.get("cont-yn", "N")
            key = r.headers.get("next-key", "")
            if cont != "Y" or not page or (stop and stop(page)):
                break
        return rows


class KiwoomSource(BaseSource):
    name = "kiwoom"

    def __init__(self, cfg):
        k = cfg.key
        self.cfg = cfg
        self.markets = cfg["markets"]
        self.c = KiwoomClient(k.get("kiwoom_appkey"), k.get("kiwoom_secretkey"), cfg.data_dir,
                              mock=bool(cfg.get("kiwoom", {}).get("mock", False)))
        self._shares: dict[str, tuple[str, float]] | None = None

    # ---------------- 상장주식수 (시가총액 계산용, 하루 1회 캐시)
    def shares(self) -> dict:
        if self._shares is not None:
            return self._shares
        p = self.cfg.data_dir / f"kiwoom_list_{dt.date.today():%Y%m%d}.json"
        if p.exists():
            self._shares = json.loads(p.read_text(encoding="utf-8"))
            return self._shares
        out = {}
        for m in self.markets:
            for r in self.c.call("/api/dostk/stkinfo", "ka10099", {"mrkt_tp": MARKETS[m][1]}, max_pages=20):
                out[_code(r.get("code", ""))] = [r.get("name", ""), _num(r.get("listCount")) or 0, m]
        p.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        self._shares = out
        return out

    # ---------------- 오늘 상한가 / 천만주 / 거래대금 상위
    def movers(self, date, f):
        if to_date(date) != dt.date.today():
            raise KiwoomError("키움 순위 API 는 오늘 데이터만 제공합니다.")
        recs: dict[str, dict] = {}

        def add(r, market, value=None):
            code = _code(r.get("stk_cd", ""))
            if not code:
                return
            close = abs(_num(r.get("cur_prc")) or 0)
            vol = _num(r.get("now_trde_qty") or r.get("trde_qty")) or 0
            d = recs.setdefault(code, {"code": code, "name": r.get("stk_nm", "").strip(), "market": market,
                                       "open": None, "high": None, "low": None})
            d["close"] = close or d.get("close")
            d["change_pct"] = _num(r.get("flu_rt")) if r.get("flu_rt") is not None else d.get("change_pct")
            d["volume"] = max(vol, d.get("volume") or 0)
            if value:
                d["value"] = value

        pct_min = min(f.get("hot_pct_min", 10.0), f["upper_limit_pct"])
        val_min = f.get("value_min_eok", 100) * 1e8
        for m in self.markets:
            mt = MARKETS[m][0]
            # 1) 거래대금 상위 — 기준 미만이 나오면 중단
            rows = self.c.call("/api/dostk/rkinfo", "ka10032", {"mrkt_tp": mt, "mang_stk_incls": "1", "stex_tp": "1"},
                               max_pages=5, stop=lambda pg: self._value(pg[-1]) < val_min)
            for r in rows:
                add(r, m, self._value(r))
            # 2) 등락률 상위 (상한가 포함) — 기준 미만이 나오면 중단
            rows = self.c.call("/api/dostk/rkinfo", "ka10027",
                               {"mrkt_tp": mt, "sort_tp": "1", "trde_qty_cnd": "0000", "stk_cnd": "16",
                                "crd_cnd": "0", "updown_incls": "1", "pric_cnd": "0", "trde_prica_cnd": "0",
                                "stex_tp": "1"},
                               max_pages=5, stop=lambda pg: (_num(pg[-1].get("flu_rt")) or 0) < pct_min)
            for r in rows:
                add(r, m)
            # 3) 당일 거래량 상위 — 천만 주 미만이 나오면 중단
            rows = self.c.call("/api/dostk/rkinfo", "ka10030",
                               {"mrkt_tp": mt, "sort_tp": "1", "mang_stk_incls": "1", "crd_tp": "0",
                                "trde_qty_tp": "0", "pric_tp": "0", "trde_prica_tp": "0", "mrkt_open_tp": "0",
                                "stex_tp": "1"},
                               max_pages=5, stop=lambda pg: (_num(pg[-1].get("trde_qty")) or 0) < f["volume_min"])
            for r in rows:
                add(r, m)
        if not recs:
            return pd.DataFrame(columns=COLUMNS)
        df = pd.DataFrame(recs.values())
        try:
            sh = self.shares()
            df["marcap"] = [df.close[i] * (sh.get(c, [None, 0])[1] or 0) or None for i, c in enumerate(df.code)]
        except Exception as e:
            print(f"[warn] 키움 종목리스트 실패(시총 생략): {e}")
            df["marcap"] = None
        if "value" not in df:
            df["value"] = None
        return _filter(df[COLUMNS], f)[COLUMNS + ["is_upper", "is_volume", "is_hot"]]

    def value_top(self, date, n=100):
        """오늘 거래대금 상위 n (전체 시장, ka10032)"""
        if to_date(date) != dt.date.today():
            raise KiwoomError("키움 순위 API 는 오늘 데이터만 제공합니다.")
        rows = self.c.call("/api/dostk/rkinfo", "ka10032", {"mrkt_tp": "000", "mang_stk_incls": "1", "stex_tp": "1"},
                           max_pages=10, stop=_enough(n))
        out = []
        for i, r in enumerate(rows[:n], 1):
            out.append({"code": _code(r.get("stk_cd", "")), "name": (r.get("stk_nm") or "").strip(), "market": "",
                        "close": abs(_num(r.get("cur_prc")) or 0), "change_pct": _num(r.get("flu_rt")) or 0,
                        "value": self._value(r), "rank": int(_num(r.get("now_rank")) or i)})
        return pd.DataFrame(out, columns=["code", "name", "market", "close", "change_pct", "value", "rank"])

    # ---------------- 5분 감시용 현재가 (관심종목정보 ka10095, 여러 종목 한 번에)
    def quotes(self, codes: list[str], chunk: int = 30) -> pd.DataFrame:
        out = []
        for i in range(0, len(codes), chunk):
            rows = self.c.call("/api/dostk/stkinfo", "ka10095", {"stk_cd": "|".join(codes[i:i + chunk])},
                               max_pages=3)
            for r in rows:
                out.append({"code": _code(r.get("stk_cd", "")), "name": (r.get("stk_nm") or "").strip(),
                            "price": abs(_num(r.get("cur_prc")) or 0), "pct": _num(r.get("flu_rt")) or 0.0,
                            "value": self._value(r), "strength": _num(r.get("cntr_str"))})
        return pd.DataFrame(out, columns=["code", "name", "price", "pct", "value", "strength"])

    def market_rank(self, n: int = 300) -> dict[str, int]:
        rows = self.c.call("/api/dostk/rkinfo", "ka10032", {"mrkt_tp": "000", "mang_stk_incls": "1", "stex_tp": "1"},
                           max_pages=10, stop=_enough(n))
        return {_code(r.get("stk_cd", "")): int(_num(r.get("now_rank")) or i) for i, r in enumerate(rows[:n], 1)}

    @staticmethod
    def _value(r) -> float:
        """거래대금(원). 키움 순위의 trde_prica 단위(백만원/원)를 종가×거래량과 비교해 판별."""
        raw = _num(r.get("trde_prica")) or 0
        approx = abs(_num(r.get("cur_prc")) or 0) * (_num(r.get("now_trde_qty") or r.get("trde_qty")) or 0)
        if approx <= 0:
            return raw * 1e6
        for unit in (1e6, 1, 1e3, 1e8):
            if 0.3 < raw * unit / approx < 3:
                return raw * unit
        return approx

    # ---------------- 일봉
    def ohlcv(self, code, start, end):
        start, end = to_date(start), to_date(end)
        rows = self.c.call("/api/dostk/chart", "ka10081", {"stk_cd": code, "base_dt": ymd(end), "upd_stkpc_tp": "1"},
                           max_pages=10, stop=lambda pg: str(pg[-1].get("dt", "99999999")) < ymd(start))
        recs = []
        for r in rows:
            if not r.get("dt"):
                continue
            recs.append((pd.Timestamp(r["dt"]), abs(_num(r.get("open_pric")) or 0), abs(_num(r.get("high_pric")) or 0),
                         abs(_num(r.get("low_pric")) or 0), abs(_num(r.get("cur_prc")) or 0),
                         int(_num(r.get("trde_qty")) or 0)))
        df = pd.DataFrame(recs, columns=["date", "open", "high", "low", "close", "volume"]).drop_duplicates("date")
        df = df.set_index("date").sort_index()
        return df.loc[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]


def check(cfg) -> None:
    """연결 점검: 토큰 → 거래대금 상위 5 → 삼성전자 일봉 5개."""
    src = KiwoomSource(cfg)
    print(f"서버: {src.c.base}")
    src.c.token()
    print("① 토큰 발급 OK")
    rows = src.c.call("/api/dostk/rkinfo", "ka10032", {"mrkt_tp": "000", "mang_stk_incls": "1", "stex_tp": "1"})
    print(f"② 거래대금 상위 OK ({len(rows)}건). 상위 5:")
    for r in rows[:5]:
        print(f"   {r.get('stk_nm')}({_code(r.get('stk_cd', ''))}) {r.get('flu_rt')}% "
              f"거래대금≈{KiwoomSource._value(r) / 1e8:,.0f}억 (원본 trde_prica={r.get('trde_prica')})")
    df = src.ohlcv("005930", dt.date.today() - dt.timedelta(days=14), dt.date.today())
    print(f"③ 일봉 OK (삼성전자 {len(df)}일)")
    print(df.tail(5).to_string())
