"""장중 5분 감시 — 기존 HTS 실시간 연동 엑셀(5분 매크로)을 키움 REST API 로 옮긴 것.

감시 종목을 고르는 두 방식을 같은 날 동시에 돌려 비교한다.
  ① 예상체결  : 08:55 1차 → 08:59 확정, 장전 예상체결 등락률 상위 100
                (우선주·스팩·ETF·ETN 제외, 예상체결량 1만 주↑, 1천 원↑)
  ② 거래대금이력: 최근 10거래일 안에 거래대금 100위 이내 + 등락률 15% 이상이었던 종목 (+ extra_codes)
5분마다 그룹마다:
  🔴 주목 : 5분 거래대금 증가 ≥ 5억 AND 5분 등락률 변화 ≥ +1.5%p
  ▲ 순위 : 그룹 안 거래대금 순위가 위로 (예: 34위 → 30위)
  매수   : buy_rule=both 이면 주목 + 순위 상승, rank_only 면 순위 상승만 → 그룹 보유금의 20% 제안
매수 신호 뒤 30분·1시간·종가 수익률과 최고 수익률을 기록해 두 방식의 성과를 비교한다.
실제 주문은 하지 않는다.
"""
from __future__ import annotations

import datetime as dt
import math
import re
import time as _time

import numpy as np
import pandas as pd

from .util import iso, to_date

GROUPS = ["expected", "history"]
UNI_COLS = ["code", "name", "rank", "reason"]


def gcfg(cfg, g) -> dict:
    return cfg["watch"]["groups"][g]


def enabled(cfg) -> list[str]:
    return [g for g in GROUPS if gcfg(cfg, g).get("enabled", True)]


def label(cfg, g) -> str:
    return gcfg(cfg, g).get("label", g)


def _at(date, hhmm) -> dt.datetime:
    h, m = map(int, str(hhmm).split(":"))
    return dt.datetime.combine(to_date(date), dt.time(h, m))


def _f(x):
    try:
        x = float(x)
        return None if math.isnan(x) else x
    except (TypeError, ValueError):
        return None


def _int(x):
    x = _f(x)
    return None if x is None else int(x)


def save_universe(con, date, grp, df: pd.DataFrame, picked_at: str):
    con.execute("DELETE FROM watch_universe WHERE date=? AND grp=?", (iso(date), grp))
    for r in df.itertuples():
        con.execute("INSERT OR REPLACE INTO watch_universe VALUES (?,?,?,?,?,?,?)",
                    (iso(date), grp, r.code, r.name, _int(r.rank), r.reason, picked_at))
    con.commit()


def load_universe(con, date, grp) -> pd.DataFrame:
    rows = con.execute("SELECT code, name, rank, reason FROM watch_universe WHERE date=? AND grp=? ORDER BY rank",
                       (iso(date), grp)).fetchall()
    return pd.DataFrame([dict(r) for r in rows], columns=UNI_COLS)


# ------------------------------------------------------------------ ② 거래대금 이력
def record_day(con, src, date, n=100) -> int:
    """그날 거래대금 상위 n 저장 (evening 에서 매일 호출 → ② 그룹의 재료)"""
    df = src.value_top(date, n)
    d = iso(date)
    for r in df.itertuples():
        con.execute("INSERT OR REPLACE INTO value_rank_daily VALUES (?,?,?,?,?,?,?)",
                    (d, r.code, r.name, int(r.rank), float(r.value or 0), float(r.change_pct or 0),
                     float(r.close or 0)))
    con.commit()
    return len(df)


def lookback_dates(src, today, n) -> list[dt.date]:
    days = src.trading_days(to_date(today) - dt.timedelta(days=1), n)
    return [d for d in days if d < to_date(today)][-n:]


def ensure_history(cfg, con, src, today, quiet=False):
    h = gcfg(cfg, "history")
    for d in lookback_dates(src, today, h["lookback_days"]):
        if con.execute("SELECT 1 FROM value_rank_daily WHERE date=? LIMIT 1", (iso(d),)).fetchone():
            continue
        try:
            k = record_day(con, src, d, h["top_n"])
            if not quiet:
                print(f"[감시] {d} 거래대금 상위 {k}종목 저장")
        except Exception as e:
            if not quiet:
                print(f"[warn] {d} 거래대금 순위를 가져오지 못함 ({e.__class__.__name__}). "
                      f"과거 날짜는 KRX 로그인이 필요하고, 매일 evening 을 돌리면 자동으로 쌓입니다.")


def history_universe(cfg, con, src, today) -> pd.DataFrame:
    h = gcfg(cfg, "history")
    days = lookback_dates(src, today, h["lookback_days"])
    recs = []
    if days:
        for r in con.execute(
                """SELECT code, MAX(name) name, MAX(date) last_date, MIN(rank) best_rank, MAX(change_pct) max_pct
                   FROM value_rank_daily WHERE date>=? AND date<? AND rank<=? AND change_pct>=?
                   GROUP BY code ORDER BY last_date DESC, max_pct DESC""",
                (iso(days[0]), iso(today), h["top_n"], h["min_pct"])):
            recs.append({"code": r["code"], "name": r["name"], "rank": None,
                         "reason": f"{r['last_date']} 거래대금 {r['best_rank']}위, {r['max_pct']:+.1f}%"})
    have = {r["code"] for r in recs}
    for c in (str(c).strip().zfill(6) for c in (cfg["watch"].get("extra_codes") or []) if str(c).strip()):
        if c in have:
            continue
        have.add(c)
        nm = con.execute("SELECT name FROM stocks WHERE code=? UNION ALL "
                         "SELECT name FROM value_rank_daily WHERE code=? LIMIT 1", (c, c)).fetchone()
        recs.append({"code": c, "name": nm[0] if nm else "", "rank": None, "reason": "직접 추가(extra_codes)"})
    return pd.DataFrame(recs, columns=UNI_COLS)


# ------------------------------------------------------------------ ① 예상체결
_PREF = re.compile(r"(\d?우[A-C]?|우선주)$")


def is_pref_or_spac(code: str, name: str) -> bool:
    """우선주: 보통주 코드는 끝자리가 0 (005930 / 우선주 005935, 18064K) 또는 이름 끝 '우', '우B'. 스팩: 이름에 '스팩'."""
    code, name = str(code), str(name or "")
    return (code[-1:] != "0") or bool(_PREF.search(name)) or ("스팩" in name)


def pick_expected(cfg, con, feed, today, tag: str, quiet=False) -> pd.DataFrame:
    e = gcfg(cfg, "expected")
    raw = feed.expected_top(e["top_n"])
    df = raw.copy()
    if e.get("exclude_pref_spac", True):
        df = df[[not is_pref_or_spac(c, n) for c, n in zip(df["code"], df["name"])]]
    df = df[(df["qty"] >= e.get("min_qty", 0)) & (df["price"] >= e.get("min_price", 0))]
    df = df.head(e["top_n"]).reset_index(drop=True)
    df["rank"] = range(1, len(df) + 1)
    df["reason"] = [f"{tag} 예상 {p:+.2f}% ({q:,.0f}주)" for p, q in zip(df["pct"], df["qty"])]
    now = dt.datetime.now().strftime("%H:%M")
    save_universe(con, today, "expected", df[UNI_COLS], f"{tag} {now}")
    if not quiet:
        print(f"[감시] ①예상체결 {tag} 선정: {len(df)}종목 (원본 {len(raw)}개에서 우선주·스팩·소량·동전주 제외)")
    return df[UNI_COLS]


# ------------------------------------------------------------------ 한 회차에 한 번만 조회
class TickFeed:
    """두 그룹이 같은 시세를 쓰도록 회차마다 합집합을 한 번만 조회한다."""

    def __init__(self, feed):
        self.feed = feed
        self._q = pd.DataFrame()
        self._mk: dict = {}

    def begin(self, codes):
        self._q = self.feed.quotes(sorted(set(codes)))
        try:
            self._mk = self.feed.market_rank(300)
        except Exception as e:
            print(f"[warn] 시장 거래대금 순위 조회 실패: {e}")
            self._mk = {}

    def quotes(self, codes):
        if self._q.empty:
            return self._q
        return self._q[self._q["code"].isin(set(codes))].copy()

    def market_rank(self, n=300):
        return self._mk


# ------------------------------------------------------------------ 그룹별 감시
class Watcher:
    def __init__(self, cfg, con, feed, uni: pd.DataFrame, date, grp: str, now: dt.datetime | None = None):
        self.cfg, self.con, self.feed, self.uni, self.grp = cfg, con, feed, uni, grp
        self.w = cfg["watch"]
        self.date = iso(date)
        self.codes = list(uni["code"])
        self.names = dict(zip(uni["code"], uni["name"]))
        self.prev: pd.DataFrame | None = None
        last = con.execute("SELECT cash_left FROM watch_signals2 WHERE date=? AND grp=? AND kind='매수' "
                           "ORDER BY time DESC LIMIT 1", (self.date, grp)).fetchone()
        self.cash = float(last["cash_left"]) if last else float(self.w["cash"])
        self.bought = {r["code"] for r in con.execute(
            "SELECT DISTINCT code FROM watch_signals2 WHERE date=? AND grp=? AND kind='매수'", (self.date, grp))}
        t = con.execute("SELECT MAX(time) FROM watch_ticks2 WHERE date=? AND grp=?", (self.date, grp)).fetchone()[0]
        step = dt.timedelta(minutes=self.w["interval_min"])
        if t and now is not None and _at(self.date, t) < now - step * 2:
            t = None   # 오래 꺼져 있었으면 그 사이 변화가 한 번에 잡히지 않도록 새 기준값부터 시작
        if t:      # 같은 날 재시작: 마지막 스냅샷부터 이어서 비교
            rows = con.execute("SELECT code, price, pct, value, strength, wl_rank, mk_rank FROM watch_ticks2 "
                               "WHERE date=? AND grp=? AND time=?", (self.date, grp, t)).fetchall()
            self.prev = pd.DataFrame([dict(r) for r in rows]).set_index("code")

    def tick(self, now: dt.datetime, final=False) -> dict:
        w = self.w
        hm = now.strftime("%H:%M")
        q = self.feed.quotes(self.codes)
        if q.empty:
            return {"grp": self.grp, "time": hm, "rows": q, "signals": []}
        q = q.drop_duplicates("code").set_index("code")
        q["name"] = [n or self.names.get(c, c) for c, n in zip(q.index, q["name"])]
        blank = self.uni["name"].fillna("") == ""
        if blank.any():
            self.uni.loc[blank, "name"] = self.uni.loc[blank, "code"].map(q["name"]).fillna("")
            self.names.update({c: n for c, n in zip(self.uni["code"], self.uni["name"]) if n})
        q["wl_rank"] = q["value"].rank(ascending=False, method="first").astype(int)
        mk = self.feed.market_rank(300)
        q["mk_rank"] = [mk.get(c) for c in q.index]
        basis = "mk_rank" if w["rank_basis"] == "market" else "wl_rank"

        for col in ("d_value", "d_pct", "d_strength", "per_won", "rank_prev", "rank_up"):
            q[col] = np.nan
        q["red"] = False
        q["buy"] = False
        if self.prev is not None:
            p = self.prev.reindex(q.index)
            q["d_value"] = q["value"] - p["value"]
            q["d_pct"] = (q["pct"] - p["pct"]).round(2)
            q["d_strength"] = (q["strength"] - p["strength"]).round(2)
            dprice = q["price"] - p["price"]
            q["per_won"] = np.where(dprice.abs() > 0, q["d_value"] / dprice.where(dprice != 0), np.nan)
            q["rank_prev"] = p[basis]
            q["rank_up"] = q["rank_prev"] - q[basis]
            red = (q["d_value"] >= w["value_delta_eok"] * 1e8) & (q["d_pct"] >= w["pct_delta"])
            rank_ok = q["rank_up"] >= w["rank_up_min"]
            q["red"] = red.fillna(False).astype(bool)
            q["buy"] = ((red & rank_ok) if w["buy_rule"] == "both" else rank_ok).fillna(False).astype(bool)

        signals = []
        for c, r in q.iterrows():
            kinds = (["주목"] if r.red else []) + ([("매수" if c not in self.bought else "재신호")] if r.buy else [])
            for k in kinds:
                qty = amount = 0
                if k == "매수" and r.price > 0:
                    qty = int(math.floor(self.cash * w["buy_ratio"] / r.price))
                    amount = qty * r.price
                    self.cash -= amount
                    self.bought.add(c)
                    self.con.execute("INSERT OR REPLACE INTO watch_outcomes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                     (self.date, self.grp, hm, c, r["name"], r.price, qty, None, None, None, 0.0))
                sig = {"grp": self.grp, "time": hm, "code": c, "name": r["name"], "kind": k, "price": r.price,
                       "pct": r.pct, "d_value": r.d_value, "d_pct": r.d_pct, "rank_prev": _int(r.rank_prev),
                       "rank_now": _int(r[basis]), "qty": qty, "amount": amount, "cash_left": self.cash}
                signals.append(sig)
                self.con.execute("INSERT OR REPLACE INTO watch_signals2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (self.date, hm, self.grp, c, sig["name"], k, r.price, r.pct, _f(r.d_value),
                                  _f(r.d_pct), sig["rank_prev"], sig["rank_now"], qty, amount,
                                  self.cash if k == "매수" else None))
        for c, r in q.iterrows():
            self.con.execute("INSERT OR REPLACE INTO watch_ticks2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (self.date, hm, self.grp, c, r["name"], r.price, r.pct, r.value, _f(r.strength),
                              int(r.wl_rank), _int(r.mk_rank), _f(r.d_value), _f(r.d_pct), _f(r.d_strength),
                              _f(r.per_won), _int(r.rank_prev), _int(r.rank_up), int(r.red), int(r.buy)))
        self._outcomes(now, q["price"], final)
        self.con.commit()
        self.prev = q[["price", "pct", "value", "strength", "wl_rank", "mk_rank"]].copy()
        return {"grp": self.grp, "time": hm, "rows": q.reset_index(), "signals": signals, "basis": basis}

    def _outcomes(self, now, prices: pd.Series, final: bool):
        """매수 신호 이후 30분·1시간·종가 수익률, 최고 수익률 갱신"""
        for o in self.con.execute("SELECT * FROM watch_outcomes WHERE date=? AND grp=?", (self.date, self.grp)).fetchall():
            px = prices.get(o["code"])
            if px is None or not o["entry"]:
                continue
            ret = round((px / o["entry"] - 1) * 100, 2)
            st = _at(self.date, o["time"])
            upd = {"rmax": max(o["rmax"] or 0.0, ret)}
            if o["r30"] is None and now >= st + dt.timedelta(minutes=30):
                upd["r30"] = ret
            if o["r60"] is None and now >= st + dt.timedelta(minutes=60):
                upd["r60"] = ret
            if final:
                upd["rclose"] = ret
            sets = ", ".join(f"{k}=?" for k in upd)
            self.con.execute(f"UPDATE watch_outcomes SET {sets} WHERE date=? AND grp=? AND time=? AND code=?",
                             (*upd.values(), self.date, self.grp, o["time"], o["code"]))


def fill_close(con, src, date) -> int:
    """감시를 중간에 껐어도 저녁에 종가 수익률을 채운다"""
    n = 0
    for o in con.execute("SELECT * FROM watch_outcomes WHERE date=? AND rclose IS NULL", (iso(date),)).fetchall():
        try:
            df = src.ohlcv(o["code"], date, date)
        except Exception:
            continue
        if len(df) and o["entry"]:
            con.execute("UPDATE watch_outcomes SET rclose=? WHERE date=? AND grp=? AND time=? AND code=?",
                        (round((float(df["close"].iloc[-1]) / o["entry"] - 1) * 100, 2),
                         o["date"], o["grp"], o["time"], o["code"]))
            n += 1
    con.commit()
    return n


# ------------------------------------------------------------------ 출력
def print_tick(cfg, res):
    w = cfg["watch"]
    rows = res["rows"]
    n_red = int(rows["red"].sum()) if len(rows) else 0
    print(f"[{res['time']}] {label(cfg, res['grp'])} {len(rows)}종목 · 🔴 주목 {n_red} · 매수 "
          f"{sum(s['kind'] == '매수' for s in res['signals'])}")
    for s in res["signals"]:
        if s["kind"] == "주목":
            continue
        rk = f"{s['rank_prev']}→{s['rank_now']}위" if s["rank_prev"] else f"{s['rank_now']}위"
        msg = (f"   ▶ {s['kind']} {s['name']}({s['code']}) {s['price']:,.0f}원 {s['pct']:+.2f}% · "
               f"5분 대금 {(_f(s['d_value']) or 0) / 1e8:+.1f}억 · 등락 {(_f(s['d_pct']) or 0):+.2f}%p · {rk}")
        if s["kind"] == "매수":
            msg += (f"\n     → 보유금 {w['buy_ratio'] * 100:.0f}% 매수 제안: {s['qty']:,}주 "
                    f"({s['amount']:,.0f}원), 그룹 남은 보유금 {s['cash_left']:,.0f}원")
        print(msg)


def beep():
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except Exception:
        print("\a", end="")


def compare_rows(con, date=None):
    """그룹별 성과 요약. date=None 이면 누적"""
    cond, args = ("WHERE date=?", (iso(date),)) if date else ("", ())
    out = {}
    for r in con.execute(f"""SELECT grp, COUNT(*) n, AVG(r30) a30, AVG(r60) a60, AVG(rclose) ac, AVG(rmax) amax,
                                    SUM(CASE WHEN rclose>0 THEN 1 ELSE 0 END) win, COUNT(rclose) nc,
                                    SUM(qty*entry*rclose/100.0) pnl, COUNT(DISTINCT date) days
                             FROM watch_outcomes {cond} GROUP BY grp""", args):
        out[r["grp"]] = dict(r)
    sig = {r["grp"]: r["n"] for r in con.execute(
        f"SELECT grp, COUNT(*) n FROM watch_signals2 {cond} {'AND' if cond else 'WHERE'} kind='주목' GROUP BY grp",
        args)}
    return out, sig


def write_excel(cfg, con, date, unis: dict, path):
    from openpyxl import Workbook
    from openpyxl.formatting.rule import ColorScaleRule
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    from .excel import _sheet

    w = cfg["watch"]
    d = iso(date)
    RED = PatternFill("solid", fgColor="FFC7CE")
    BUY = PatternFill("solid", fgColor="FF7C80")
    basis = "mk_rank" if w["rank_basis"] == "market" else "wl_rank"
    wb = Workbook()
    wb.remove(wb.active)

    # 성과비교
    ws = _sheet(wb, "성과비교", ["그룹", "기간", "감시 종목 수", "주목 신호", "매수 신호", "30분 평균(%)",
                             "1시간 평균(%)", "종가 평균(%)", "종가 적중률(%)", "평균 최고수익(%)", "모의 손익(원)", "일수"],
                [16, 8, 11, 9, 9, 11, 11, 11, 12, 13, 13, 6])
    for period, dd in (("오늘", date), ("누적", None)):
        out, sig = compare_rows(con, dd)
        for g in GROUPS:
            o = out.get(g, {})
            nc = o.get("nc") or 0
            ws.append([label(cfg, g), period, len(unis.get(g, [])) if period == "오늘" else None, sig.get(g, 0),
                       o.get("n", 0), _r(o.get("a30")), _r(o.get("a60")), _r(o.get("ac")),
                       _r((o.get("win") or 0) / nc * 100) if nc else None, _r(o.get("amax")),
                       round(o["pnl"]) if o.get("pnl") is not None else None, o.get("days")])
    ws.append([])
    ws.append(["적중 = 매수 신호가 난 가격보다 종가가 높음. 모의 손익 = 제안 수량 × (종가 − 신호 가격)."])

    # 그룹별 실시간
    for g in GROUPS:
        if g not in unis:
            continue
        ws = _sheet(wb, f"실시간_{label(cfg, g)}",
                    ["순위", "이전순위", "순위변동", "시장순위", "종목명", "코드", "현재가", "등락률", "거래대금(억)",
                     "5분 대금증가(억)", "5분 등락률변화(%p)", "가격1원당 유입(만원)", "체결강도", "체결강도변화", "신호"],
                    [6, 8, 8, 8, 16, 9, 11, 9, 11, 13, 14, 14, 9, 11, 8])
        t = con.execute("SELECT MAX(time) FROM watch_ticks2 WHERE date=? AND grp=?", (d, g)).fetchone()[0]
        if not t:
            continue
        for r in con.execute(f"SELECT * FROM watch_ticks2 WHERE date=? AND grp=? AND time=? "
                             f"ORDER BY {basis} IS NULL, {basis}", (d, g, t)):
            up = r["rank_up"]
            ws.append([r[basis], r["rank_prev"], (f"▲{up}" if up and up > 0 else (f"▼{-up}" if up else "")),
                       r["mk_rank"], r["name"], r["code"], r["price"], r["pct"], round((r["value"] or 0) / 1e8, 1),
                       None if r["d_value"] is None else round(r["d_value"] / 1e8, 2), r["d_pct"],
                       None if r["per_won"] is None else round(r["per_won"] / 1e4, 1), r["strength"],
                       r["d_strength"], "매수" if r["buy"] else ("주목" if r["red"] else "")])
            if r["red"] or r["buy"]:
                for c in ws[ws.max_row]:
                    c.fill = BUY if r["buy"] else RED
                if r["buy"]:
                    ws.cell(ws.max_row, 5).font = Font(bold=True)
        ws.cell(1, 16, f"기준 시각 {d} {t}")

    # 신호 성과
    ws = _sheet(wb, "신호성과", ["그룹", "신호시각", "종목명", "코드", "신호가격", "제안수량", "30분(%)", "1시간(%)",
                             "종가(%)", "최고(%)"], [16, 8, 16, 9, 11, 9, 9, 9, 9, 9])
    for r in con.execute("SELECT * FROM watch_outcomes WHERE date=? ORDER BY time", (d,)):
        ws.append([label(cfg, r["grp"]), r["time"], r["name"], r["code"], r["entry"], r["qty"], r["r30"], r["r60"],
                   r["rclose"], r["rmax"]])

    # 신호기록
    ws = _sheet(wb, "신호기록", ["그룹", "시각", "구분", "종목명", "코드", "현재가", "등락률", "5분 대금증가(억)",
                             "5분 등락률변화(%p)", "이전순위", "현재순위", "제안수량", "제안금액", "그룹 남은 보유금"],
                [16, 7, 7, 16, 9, 11, 9, 13, 14, 8, 8, 10, 13, 14])
    for r in con.execute("SELECT * FROM watch_signals2 WHERE date=? ORDER BY time, grp, kind", (d,)):
        ws.append([label(cfg, r["grp"]), r["time"], r["kind"], r["name"], r["code"], r["price"], r["pct"],
                   None if r["d_value"] is None else round(r["d_value"] / 1e8, 2), r["d_pct"], r["rank_prev"],
                   r["rank_now"], r["qty"] or None, r["amount"] or None, r["cash_left"]])
        fill = BUY if r["kind"] == "매수" else (RED if r["kind"] == "주목" else None)
        if fill:
            for c in ws[ws.max_row]:
                c.fill = fill

    # 5분 대금 흐름
    times = [r[0] for r in con.execute("SELECT DISTINCT time FROM watch_ticks2 WHERE date=? ORDER BY time", (d,))]
    ws = _sheet(wb, "5분대금흐름(억)", ["그룹", "종목명"] + times, [14, 16] + [6] * len(times))
    grid = {(r["grp"], r["name"], r["time"]): r["d_value"] for r in
            con.execute("SELECT grp, name, time, d_value FROM watch_ticks2 WHERE date=?", (d,))}
    for g in GROUPS:
        for n in [r[0] for r in con.execute("SELECT name FROM watch_ticks2 WHERE date=? AND grp=? GROUP BY code "
                                            "ORDER BY MAX(value) DESC", (d, g))]:
            ws.append([label(cfg, g), n] + [None if grid.get((g, n, x)) is None else round(grid[(g, n, x)] / 1e8, 1)
                                            for x in times])
    if ws.max_row > 1 and times:
        ws.conditional_formatting.add(f"C2:{get_column_letter(len(times) + 2)}{ws.max_row}", ColorScaleRule(
            start_type="num", start_value=0, start_color="FFFFFF",
            end_type="num", end_value=max(w["value_delta_eok"] * 2, 1), end_color="F8696B"))

    # 감시 대상
    ws = _sheet(wb, "감시대상", ["그룹", "순위", "종목명", "코드", "선정 사유"], [16, 6, 16, 9, 44])
    for g in GROUPS:
        for r in unis.get(g, pd.DataFrame(columns=UNI_COLS)).itertuples():
            ws.append([label(cfg, g), r.rank, r.name, r.code, r.reason])

    # 기준
    e, h = gcfg(cfg, "expected"), gcfg(cfg, "history")
    ws = _sheet(wb, "기준", ["항목", "값"], [26, 80])
    for row in ((label(cfg, "expected"), f"{'/'.join(e['pick_times'])} 예상체결 등락률 상위 {e['top_n']} "
                 f"(우선주·스팩·ETF·ETN 제외, 예상체결량 {e['min_qty']:,}주↑, {e['min_price']:,}원↑)"),
                (label(cfg, "history"), f"최근 {h['lookback_days']}거래일 거래대금 {h['top_n']}위 이내 + "
                 f"등락률 {h['min_pct']}%↑" + (" + extra_codes" if w.get("extra_codes") else "")),
                ("주목(빨간 음영)", f"{w['interval_min']}분 거래대금 증가 ≥ {w['value_delta_eok']}억 AND "
                                 f"등락률 변화 ≥ +{w['pct_delta']}%p"),
                ("순위 기준", "그룹 안 거래대금 순위" if basis == "wl_rank" else "전체 시장 거래대금 순위"),
                ("매수 신호", ("주목 + " if w["buy_rule"] == "both" else "") + f"순위 {w['rank_up_min']}계단↑ 상승"),
                ("매수 제안", f"그룹별 보유금 {w['cash']:,}원에서 남은 금액의 {w['buy_ratio'] * 100:.0f}%. "
                           f"같은 종목은 하루 1번 '매수', 이후 '재신호'"),
                ("주의", "실제 주문은 하지 않습니다. 수량·손익은 모의 계산입니다.")):
        ws.append(list(row))

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + "~tmp.xlsx")
    wb.save(tmp)
    try:
        tmp.replace(path)
        return path
    except PermissionError:
        alt = path.with_name(path.stem + "_최신.xlsx")
        try:
            tmp.replace(alt)
        except PermissionError:
            print(f"[warn] 엑셀이 열려 있어 저장하지 못했습니다: {path.name} (닫으면 다음 회차에 저장)")
            return None
        print(f"[info] {path.name} 이 열려 있어 {alt.name} 로 저장했습니다.")
        return alt


def _r(x, n=2):
    x = _f(x)
    return None if x is None else round(x, n)


# ------------------------------------------------------------------ 실행
def _feed(app, demo):
    if demo:
        return DemoFeed()
    k = getattr(app.src, "kiwoom", None) or (app.src if hasattr(app.src, "quotes") else None)
    if k is None:
        raise RuntimeError("5분 감시는 키움 REST API 가 필요합니다. config.yaml 의 keys.kiwoom_appkey / "
                           "kiwoom_secretkey 를 입력하세요.")
    return k


def _show(cfg, g, uni):
    print(f"[감시] {label(cfg, g)} {len(uni)}종목")
    for r in uni.head(15).itertuples():
        print(f"   - {r.name}({r.code}) {r.reason}")
    if len(uni) > 15:
        print(f"   ... 외 {len(uni) - 15}종목 (엑셀 '감시대상' 시트)")


def run(app, once=False, demo=False, list_only=False, clock=None, sleep=_time.sleep):
    cfg, con, src = app.cfg, app.con, app.src
    w = cfg["watch"]
    clock = clock or dt.datetime.now
    now = clock()
    today = now.date()
    groups = enabled(cfg)
    feed = _feed(app, demo)
    start, end = _at(today, w["start"]), _at(today, w["end"])
    unis: dict[str, pd.DataFrame] = {}

    if "history" in groups:
        if demo:
            _demo_history(cfg, con, src, today)
        else:
            ensure_history(cfg, con, src, today)
        unis["history"] = history_universe(cfg, con, src, today)
        _show(cfg, "history", unis["history"])

    if "expected" in groups:
        e = gcfg(cfg, "expected")
        times = [_at(today, t) for t in e["pick_times"]]
        uni = load_universe(con, today, "expected")
        if demo:
            for i, t in enumerate(times):
                uni = pick_expected(cfg, con, feed, today, f"{i + 1}차" if i < len(times) - 1 else "확정")
        elif list_only or once or now >= start:
            if uni.empty:
                if now >= start:
                    print("[warn] 장 시작 후라 '예상체결' 순위가 아닌 값이 올 수 있습니다. 08:55~08:59 에 켜 두세요.")
                try:
                    uni = pick_expected(cfg, con, feed, today, "확정" if now >= times[-1] else "1차")
                except Exception as ex:
                    print(f"[warn] ①예상체결 선정 실패: {ex}")
        else:                                   # 장 시작 전: 08:55 1차 → 08:59 확정
            for i, t in enumerate(times):
                now = clock()
                if now < t:
                    print(f"[감시] ①예상체결 {t:%H:%M} 선정 대기...")
                    sleep(max(1.0, (t - now).total_seconds()))
                elif i < len(times) - 1 and clock() >= times[i + 1]:
                    continue                    # 늦게 켜서 1차 시각을 지났으면 바로 다음 단계로
                try:
                    uni = pick_expected(cfg, con, feed, today, "확정" if i == len(times) - 1 else f"{i + 1}차")
                except Exception as ex:
                    print(f"[warn] ①예상체결 선정 실패: {ex}")
        unis["expected"] = uni
        _show(cfg, "expected", uni)

    if list_only:
        return unis
    unis = {g: u for g, u in unis.items() if not u.empty}
    if not unis:
        print("감시할 종목이 없습니다. ①은 08:55~08:59 에 켜 두어야 하고, ②는 KRX 로그인 또는 며칠간의 evening 기록이 "
              "필요합니다. 급하면 config 의 watch.extra_codes 에 종목코드를 넣으세요.")
        return unis
    tf = TickFeed(feed)
    watchers = {g: Watcher(cfg, con, tf, u, today, g, now=clock()) for g, u in unis.items()}
    union = sorted({c for u in unis.values() for c in u["code"]})
    path = cfg.sub("watch") / f"{iso(today)}_실시간감시.xlsx"
    step = dt.timedelta(minutes=w["interval_min"])

    def one(t, final=False):
        tf.begin(union)
        results = [wt.tick(t, final=final) for wt in watchers.values()]
        return results

    if demo:
        t = start
        while t <= end:
            for res in one(t, final=(t + step > end)):
                if res["signals"]:
                    print_tick(cfg, res)
            t += step
        print(f"  → {write_excel(cfg, con, today, unis, path)}")
        return unis

    first = True
    while True:
        now = clock()
        if not once and now < start:
            print(f"[감시] 장 시작({w['start']}) 대기 중...")
            sleep(max(1.0, (start - now).total_seconds()))
            continue
        if not once and now > end + dt.timedelta(minutes=1):
            print(f"[감시] 장 종료({w['end']}). 오늘 결과: {path}")
            break
        off = (now - start) % step
        if not once and dt.timedelta(seconds=30) < off and first:
            # 장중에 켠 경우: 12:09 → 12:10 처럼 1분짜리 비교가 생기지 않도록 다음 정각(12:10)에 기준값을 잡는다
            nxt = now - off + step
            print(f"[감시] 5분 간격을 맞추기 위해 {nxt:%H:%M} 부터 시작합니다.")
            first = False
            sleep(max(1.0, (nxt - now).total_seconds()))
            continue
        first = False
        try:
            results = one(now, final=(now + step > end + dt.timedelta(minutes=1)))
            for res in results:
                print_tick(cfg, res)
            if w.get("beep") and any(s["kind"] == "매수" for res in results for s in res["signals"]):
                beep()
            write_excel(cfg, con, today, unis, path)
        except Exception as ex:
            print(f"[warn] {now:%H:%M} 감시 실패: {ex.__class__.__name__}: {str(ex)[:200]}")
        if once:
            print(f"  → {path}")
            break
        nxt = start + step * (int((now - start) / step) + 1)
        sleep(max(1.0, (nxt - clock()).total_seconds()))
    return unis


# ------------------------------------------------------------------ 데모
class DemoFeed:
    """가짜 시세: 대부분 잔잔하고, 가끔 대금이 몰리며 가격이 뛰는 종목이 나온다."""

    def __init__(self, seed=7):
        self.rng = np.random.default_rng(seed)
        self.state: dict[str, dict] = {}
        self.pool = [(f"9{i:04d}0", f"데모{i:03d}") for i in range(160)] + \
                    [("900015", "데모우"), ("900025", "데모스팩1호")]

    def _st(self, code, name=""):
        if code not in self.state:
            self.state[code] = {"name": name or code, "pct": 0.0, "value": 0.0, "strength": 100.0,
                                "base": 5000.0 + (hash(code) % 20) * 1000}
        return self.state[code]

    def expected_top(self, n=100):
        rows = []
        for i, (c, nm) in enumerate(self.pool):
            pct = round(float(self.rng.uniform(1, 25)), 2)
            rows.append({"code": c, "name": nm, "price": 1500.0 if i != 3 else 800.0, "pct": pct,
                         "qty": 50000.0 if i != 5 else 3000.0})
            self._st(c, nm)["pct"] = pct
        df = pd.DataFrame(rows).sort_values("pct", ascending=False).reset_index(drop=True)
        df["rank"] = range(1, len(df) + 1)
        return df

    def quotes(self, codes):
        out = []
        for c in codes:
            s = self._st(c)
            burst = self.rng.random() < 0.05
            s["value"] += self.rng.lognormal(19.2, 0.9) + (self.rng.uniform(6, 20) * 1e8 if burst else 0)
            s["pct"] = float(np.clip(s["pct"] + self.rng.normal(-0.02, 0.45) + (self.rng.uniform(1.5, 3.5) if burst
                                                                                  else 0), -29.9, 29.9))
            s["strength"] = float(np.clip(s["strength"] + self.rng.normal(0, 8) + (20 if burst else 0), 30, 300))
            out.append({"code": c, "name": s["name"], "price": round(s["base"] * (1 + s["pct"] / 100), -1),
                        "pct": round(s["pct"], 2), "value": s["value"], "strength": round(s["strength"], 2)})
        return pd.DataFrame(out, columns=["code", "name", "price", "pct", "value", "strength"])

    def market_rank(self, n=300):
        order = sorted(self.state, key=lambda c: -self.state[c]["value"])
        return {c: i + 1 for i, c in enumerate(order[:n])}


def _demo_history(cfg, con, src, today):
    h = gcfg(cfg, "history")
    for d in lookback_dates(src, today, h["lookback_days"]):
        df = src.value_top(d, h["top_n"])
        for r in df.itertuples():
            con.execute("INSERT OR REPLACE INTO value_rank_daily VALUES (?,?,?,?,?,?,?)",
                        (iso(d), r.code, r.name, int(r.rank), float(r.value), 25.0, float(r.close)))
    con.commit()
