"""장중 5분 감시 — 기존 HTS 실시간 연동 엑셀(5분 매크로)을 키움 REST API 로 옮긴 것.

감시 대상: 최근 1주일(lookback_days 거래일) 안에 '거래대금 상위 100위 이내 + 등락률 20% 이상'이었던 종목
5분마다:
  ① 5분 거래대금 증가 ≥ 5억 AND 5분 등락률 변화 ≥ +1.5%p  → 🔴 주목 (엑셀 빨간 음영)
  ② 거래대금 순위가 위로 올라감 (예: 34위 → 30위)           → ▲ 순위 상승
  ③ 매수 신호 (buy_rule: both = ①+②, rank_only = ②만)     → 보유금의 20% 매수 제안
실제 주문은 하지 않는다. 제안 수량과 남은 보유금은 모의로만 기록한다.
"""
from __future__ import annotations

import datetime as dt
import math
import time as _time

import numpy as np
import pandas as pd

from .util import iso, to_date


# ------------------------------------------------------------------ 감시 대상
def record_day(con, src, date, n=100) -> int:
    """그날 거래대금 상위 n 을 저장 (evening 에서 매일 호출 → 감시 대상의 재료)"""
    df = src.value_top(date, n)
    d = iso(date)
    for r in df.itertuples():
        con.execute("INSERT OR REPLACE INTO value_rank_daily VALUES (?,?,?,?,?,?,?)",
                    (d, r.code, r.name, int(r.rank), float(r.value or 0), float(r.change_pct or 0),
                     float(r.close or 0)))
    con.commit()
    return len(df)


def lookback_dates(src, today, n) -> list[dt.date]:
    """오늘 이전 n 거래일 (오늘 장중이면 오늘 제외)"""
    days = src.trading_days(to_date(today) - dt.timedelta(days=1), n)
    return [d for d in days if d < to_date(today)][-n:]


def ensure_history(cfg, con, src, today, quiet=False):
    w = cfg["watch"]
    for d in lookback_dates(src, today, w["lookback_days"]):
        if con.execute("SELECT 1 FROM value_rank_daily WHERE date=? LIMIT 1", (iso(d),)).fetchone():
            continue
        try:
            k = record_day(con, src, d, w["top_n"])
            if not quiet:
                print(f"[감시] {d} 거래대금 상위 {k}종목 저장")
        except Exception as e:
            if not quiet:
                print(f"[warn] {d} 거래대금 순위를 가져오지 못함 ({e.__class__.__name__}). "
                      f"과거 날짜는 KRX 로그인이 필요하고, 매일 evening 을 돌리면 자동으로 쌓입니다.")


def universe(cfg, con, src, today) -> pd.DataFrame:
    w = cfg["watch"]
    days = lookback_dates(src, today, w["lookback_days"])
    rows = []
    if days:
        rows = con.execute(
            """SELECT code, MAX(name) name, MAX(date) last_date, MIN(rank) best_rank, MAX(change_pct) max_pct,
                      COUNT(*) n_days
               FROM value_rank_daily WHERE date>=? AND date<? AND rank<=? AND change_pct>=?
               GROUP BY code ORDER BY last_date DESC, max_pct DESC""",
            (iso(days[0]), iso(today), w["top_n"], w["min_pct"])).fetchall()
    df = pd.DataFrame([dict(r) for r in rows],
                      columns=["code", "name", "last_date", "best_rank", "max_pct", "n_days"])
    df["reason"] = [f"{r.last_date} 거래대금 {r.best_rank}위, {r.max_pct:+.1f}%" for r in df.itertuples()]
    extra = [str(c).zfill(6) for c in (w.get("extra_codes") or []) if str(c).strip()]
    for c in extra:
        if c not in set(df["code"]):
            nm = con.execute("SELECT name FROM stocks WHERE code=?", (c,)).fetchone()
            df.loc[len(df)] = [c, nm["name"] if nm else "", "", None, None, 0, "직접 추가(extra_codes)"]
    return df.reset_index(drop=True)


# ------------------------------------------------------------------ 감시 로직
class Watcher:
    def __init__(self, cfg, con, feed, uni: pd.DataFrame, date):
        self.cfg, self.con, self.feed, self.uni = cfg, con, feed, uni
        self.w = cfg["watch"]
        self.date = iso(date)
        self.codes = list(uni["code"])
        self.names = dict(zip(uni["code"], uni["name"]))
        self.prev: pd.DataFrame | None = None
        last = con.execute("SELECT cash_left FROM watch_signals WHERE date=? AND kind='매수' "
                           "ORDER BY time DESC LIMIT 1", (self.date,)).fetchone()
        self.cash = float(last["cash_left"]) if last else float(self.w["cash"])
        self.bought = {r["code"] for r in con.execute(
            "SELECT DISTINCT code FROM watch_signals WHERE date=? AND kind='매수'", (self.date,))}
        self._resume()

    def _resume(self):
        """같은 날 재시작하면 마지막 스냅샷을 기준으로 이어서 비교"""
        t = self.con.execute("SELECT MAX(time) FROM watch_ticks WHERE date=?", (self.date,)).fetchone()[0]
        if not t:
            return
        rows = self.con.execute("SELECT code, price, pct, value, strength, wl_rank, mk_rank FROM watch_ticks "
                                "WHERE date=? AND time=?", (self.date, t)).fetchall()
        self.prev = pd.DataFrame([dict(r) for r in rows]).set_index("code")

    def tick(self, now: dt.datetime) -> dict:
        w = self.w
        q = self.feed.quotes(self.codes)
        if q.empty:
            return {"time": now.strftime("%H:%M"), "rows": q, "signals": []}
        q = q.drop_duplicates("code").set_index("code")
        q["name"] = [n or self.names.get(c, c) for c, n in zip(q.index, q["name"])]
        q["wl_rank"] = q["value"].rank(ascending=False, method="first").astype(int)
        try:
            mk = self.feed.market_rank(300)
        except Exception as e:
            print(f"[warn] 시장 거래대금 순위 조회 실패: {e}")
            mk = {}
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
            q["red"] = (q["d_value"] >= w["value_delta_eok"] * 1e8) & (q["d_pct"] >= w["pct_delta"])
            rank_ok = q["rank_up"] >= w["rank_up_min"]
            q["buy"] = (q["red"] & rank_ok) if w["buy_rule"] == "both" else rank_ok
            q["buy"] = q["buy"].fillna(False).astype(bool)
            q["red"] = q["red"].fillna(False).astype(bool)

        hm = now.strftime("%H:%M")
        signals = []
        for c, r in q.iterrows():
            kinds = []
            if r.red:
                kinds.append("주목")
            if r.buy:
                kinds.append("매수" if c not in self.bought else "재신호")
            for k in kinds:
                qty = amount = 0
                if k == "매수" and r.price > 0:
                    qty = int(math.floor(self.cash * w["buy_ratio"] / r.price))
                    amount = qty * r.price
                    self.cash -= amount
                    self.bought.add(c)
                sig = {"time": hm, "code": c, "name": r["name"], "kind": k, "price": r.price, "pct": r.pct,
                       "d_value": r.d_value, "d_pct": r.d_pct, "rank_prev": _int(r.rank_prev),
                       "rank_now": _int(r[basis]), "qty": qty, "amount": amount, "cash_left": self.cash}
                signals.append(sig)
                self.con.execute("INSERT OR REPLACE INTO watch_signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (self.date, hm, c, sig["name"], k, r.price, r.pct, _f(r.d_value), _f(r.d_pct),
                                  sig["rank_prev"], sig["rank_now"], qty, amount,
                                  self.cash if k == "매수" else None))
        for c, r in q.iterrows():
            self.con.execute("INSERT OR REPLACE INTO watch_ticks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (self.date, hm, c, r["name"], r.price, r.pct, r.value, _f(r.strength),
                              int(r.wl_rank), _int(r.mk_rank), _f(r.d_value), _f(r.d_pct), _f(r.d_strength),
                              _f(r.per_won), _int(r.rank_prev), _int(r.rank_up), int(r.red), int(r.buy)))
        self.con.commit()
        self.prev = q[["price", "pct", "value", "strength", "wl_rank", "mk_rank"]].copy()
        return {"time": hm, "rows": q.reset_index(), "signals": signals, "basis": basis}


def _f(x):
    try:
        x = float(x)
        return None if math.isnan(x) else x
    except (TypeError, ValueError):
        return None


def _int(x):
    x = _f(x)
    return None if x is None else int(x)


# ------------------------------------------------------------------ 출력
def print_tick(res, w):
    rows = res["rows"]
    n_red = int(rows["red"].sum()) if len(rows) else 0
    print(f"[{res['time']}] 감시 {len(rows)}종목 · 🔴 주목 {n_red} · 매수 신호 "
          f"{sum(s['kind'] == '매수' for s in res['signals'])}")
    for s in res["signals"]:
        if s["kind"] == "주목":
            continue
        rk = f"{s['rank_prev']}→{s['rank_now']}위" if s["rank_prev"] else f"{s['rank_now']}위"
        msg = (f"   ▶ {s['kind']} {s['name']}({s['code']}) {s['price']:,.0f}원 {s['pct']:+.2f}% · "
               f"5분 대금 {(_f(s['d_value']) or 0) / 1e8:+.1f}억 · 등락 {(_f(s['d_pct']) or 0):+.2f}%p · {rk}")
        if s["kind"] == "매수":
            msg += (f"\n     → 보유금 {w['buy_ratio'] * 100:.0f}% 매수 제안: {s['qty']:,}주 "
                    f"({s['amount']:,.0f}원), 남은 보유금 {s['cash_left']:,.0f}원")
        print(msg)


def beep():
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except Exception:
        print("\a", end="")


def write_excel(cfg, con, date, uni, path):
    from openpyxl import Workbook
    from openpyxl.formatting.rule import ColorScaleRule
    from openpyxl.styles import Font, PatternFill
    from .excel import _sheet

    w = cfg["watch"]
    d = iso(date)
    RED = PatternFill("solid", fgColor="FFC7CE")
    BUY = PatternFill("solid", fgColor="FF7C80")
    wb = Workbook()
    wb.remove(wb.active)

    t = con.execute("SELECT MAX(time) FROM watch_ticks WHERE date=?", (d,)).fetchone()[0]
    ws = _sheet(wb, "실시간",
                ["순위", "이전순위", "순위변동", "시장순위", "종목명", "코드", "현재가", "등락률", "거래대금(억)",
                 "5분 대금증가(억)", "5분 등락률변화(%p)", "가격1원당 유입(만원)", "체결강도", "체결강도변화", "신호"],
                [6, 8, 8, 8, 16, 9, 11, 9, 11, 13, 14, 14, 9, 11, 10])
    basis = "mk_rank" if w["rank_basis"] == "market" else "wl_rank"
    if t:
        rows = con.execute(f"SELECT * FROM watch_ticks WHERE date=? AND time=? ORDER BY {basis} IS NULL, {basis}",
                           (d, t)).fetchall()
        for r in rows:
            sig = "매수" if r["buy"] else ("주목" if r["red"] else "")
            up = r["rank_up"]
            ws.append([r[basis], r["rank_prev"], (f"▲{up}" if up and up > 0 else (f"▼{-up}" if up else "")),
                       r["mk_rank"], r["name"], r["code"], r["price"], r["pct"], round((r["value"] or 0) / 1e8, 1),
                       None if r["d_value"] is None else round(r["d_value"] / 1e8, 2), r["d_pct"],
                       None if r["per_won"] is None else round(r["per_won"] / 1e4, 1),
                       r["strength"], r["d_strength"], sig])
            if r["red"] or r["buy"]:
                for c in ws[ws.max_row]:
                    c.fill = BUY if r["buy"] else RED
                if r["buy"]:
                    ws.cell(ws.max_row, 5).font = Font(bold=True)
        ws.cell(1, 16, f"기준 시각 {d} {t}")
        for col in ("H", "K"):
            for c in ws[col][1:]:
                c.number_format = '+0.00;-0.00'

    ws = _sheet(wb, "신호기록", ["시각", "구분", "종목명", "코드", "현재가", "등락률", "5분 대금증가(억)",
                             "5분 등락률변화(%p)", "이전순위", "현재순위", "제안수량", "제안금액", "남은 보유금"],
                [7, 7, 16, 9, 11, 9, 13, 14, 8, 8, 10, 13, 13])
    for r in con.execute("SELECT * FROM watch_signals WHERE date=? ORDER BY time, kind", (d,)):
        ws.append([r["time"], r["kind"], r["name"], r["code"], r["price"], r["pct"],
                   None if r["d_value"] is None else round(r["d_value"] / 1e8, 2), r["d_pct"], r["rank_prev"],
                   r["rank_now"], r["qty"] or None, r["amount"] or None, r["cash_left"]])
        if r["kind"] == "매수":
            for c in ws[ws.max_row]:
                c.fill = BUY
        elif r["kind"] == "주목":
            for c in ws[ws.max_row]:
                c.fill = RED
    for col in ("L", "M"):
        for c in ws[col][1:]:
            c.number_format = "#,##0"

    times = [r[0] for r in con.execute("SELECT DISTINCT time FROM watch_ticks WHERE date=? ORDER BY time", (d,))]
    names = [r[0] for r in con.execute("SELECT name FROM watch_ticks WHERE date=? GROUP BY code "
                                       "ORDER BY MAX(value) DESC", (d,))]
    ws = _sheet(wb, "5분대금흐름(억)", ["종목명"] + times, [16] + [6] * len(times))
    grid = {(r["name"], r["time"]): r["d_value"] for r in
            con.execute("SELECT name, time, d_value FROM watch_ticks WHERE date=?", (d,))}
    for n in names:
        ws.append([n] + [None if grid.get((n, x)) is None else round(grid[(n, x)] / 1e8, 1) for x in times])
    if names and times:
        from openpyxl.utils import get_column_letter
        ws.conditional_formatting.add(f"B2:{get_column_letter(len(times) + 1)}{len(names) + 1}", ColorScaleRule(
            start_type="num", start_value=0, start_color="FFFFFF",
            end_type="num", end_value=max(w["value_delta_eok"] * 2, 1), end_color="F8696B"))

    ws = _sheet(wb, "감시대상", ["종목명", "코드", "선정 사유", "최근 해당일", "최고 순위", "최고 등락률", "해당 일수"],
                [16, 9, 40, 12, 9, 10, 8])
    for r in uni.itertuples():
        ws.append([r.name, r.code, r.reason, r.last_date, r.best_rank, r.max_pct, r.n_days])

    ws = _sheet(wb, "기준", ["항목", "값"], [26, 70])
    for row in (("감시 대상", f"최근 {w['lookback_days']}거래일 안에 거래대금 {w['top_n']}위 이내 + 등락률 {w['min_pct']}% 이상"
                              + (" + 직접 추가 종목" if w.get('extra_codes') else "")),
                ("주목(빨간 음영)", f"{w['interval_min']}분 거래대금 증가 ≥ {w['value_delta_eok']}억 AND "
                                 f"{w['interval_min']}분 등락률 변화 ≥ +{w['pct_delta']}%p"),
                ("순위 기준", "감시 종목 안에서의 거래대금 순위" if basis == "wl_rank" else "전체 시장 거래대금 순위"),
                ("매수 신호", ("주목 조건 + " if w["buy_rule"] == "both" else "") +
                 f"거래대금 순위 {w['rank_up_min']}계단 이상 상승"),
                ("매수 제안 금액", f"남은 보유금의 {w['buy_ratio'] * 100:.0f}% (시작 보유금 {w['cash']:,}원), "
                                f"같은 종목은 하루 1번만 '매수', 이후는 '재신호'"),
                ("주의", "실제 주문은 하지 않습니다. 제안 수량·보유금은 모의 계산입니다.")):
        ws.append(list(row))

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + "~tmp.xlsx")
    wb.save(tmp)
    try:
        tmp.replace(path)
    except PermissionError:
        alt = path.with_name(path.stem + "_최신.xlsx")
        try:
            tmp.replace(alt)
        except PermissionError:
            print(f"[warn] 엑셀 파일이 열려 있어 저장하지 못했습니다: {path.name} (닫으면 다음 회차에 저장)")
            return None
        print(f"[info] {path.name} 이 열려 있어 {alt.name} 로 저장했습니다.")
        return alt
    return path


# ------------------------------------------------------------------ 실행 루프
def _at(date, hhmm) -> dt.datetime:
    h, m = map(int, hhmm.split(":"))
    return dt.datetime.combine(to_date(date), dt.time(h, m))


def run(app, once=False, demo=False, list_only=False, clock=None, sleep=_time.sleep):
    cfg, con, src = app.cfg, app.con, app.src
    w = cfg["watch"]
    now = (clock or dt.datetime.now)()
    today = now.date()
    if demo:
        _demo_history(con, src, today, w)
    else:
        ensure_history(cfg, con, src, today)
    uni = universe(cfg, con, src, today)
    print(f"[감시] 대상 {len(uni)}종목 (최근 {w['lookback_days']}거래일 거래대금 {w['top_n']}위 이내 "
          f"+ {w['min_pct']}%↑)")
    for r in uni.head(30).itertuples():
        print(f"   - {r.name}({r.code}) {r.reason}")
    if list_only:
        return uni
    if uni.empty:
        print("감시 대상이 없습니다. evening 을 며칠 돌려 거래대금 순위를 쌓거나, KRX 로그인을 설정하거나, "
              "config 의 watch.extra_codes 에 종목코드를 넣으세요.")
        return uni
    feed = _feed(app, uni, demo)
    watcher = Watcher(cfg, con, feed, uni, today)
    path = cfg.sub("watch") / f"{iso(today)}_실시간감시.xlsx"
    step = dt.timedelta(minutes=w["interval_min"])
    start, end = _at(today, w["start"]), _at(today, w["end"])

    if demo:            # 가짜 장: 09:00~15:30 을 즉시 재생
        t = start
        while t <= end:
            res = watcher.tick(t)
            if res["signals"]:
                print_tick(res, w)
            t += step
        print(f"  → {write_excel(cfg, con, today, uni, path)}")
        return uni

    while True:
        now = (clock or dt.datetime.now)()
        if not once and now < start:
            print(f"[감시] 장 시작({w['start']}) 대기 중...")
            sleep(max(1.0, (start - now).total_seconds()))
            continue
        if not once and now > end + dt.timedelta(minutes=1):
            print(f"[감시] 장 종료({w['end']}). 오늘 결과: {path}")
            break
        try:
            res = watcher.tick(now)
            print_tick(res, w)
            if w.get("beep") and any(s["kind"] == "매수" for s in res["signals"]):
                beep()
            write_excel(cfg, con, today, uni, path)
        except Exception as e:
            print(f"[warn] {now:%H:%M} 감시 실패: {e.__class__.__name__}: {str(e)[:200]}")
        if once:
            print(f"  → {path}")
            break
        nxt = start + step * (int((now - start) / step) + 1)      # 09:05, 09:10 ... 에 맞춰 실행
        sleep(max(1.0, (nxt - (clock or dt.datetime.now)()).total_seconds()))
    return uni


def _feed(app, uni, demo):
    if demo:
        return DemoFeed(uni)
    k = getattr(app.src, "kiwoom", None) or (app.src if hasattr(app.src, "quotes") else None)
    if k is None:
        raise RuntimeError("5분 감시는 키움 REST API 가 필요합니다. config.yaml 의 keys.kiwoom_appkey / "
                           "kiwoom_secretkey 를 입력하세요.")
    return k


# ------------------------------------------------------------------ 데모
class DemoFeed:
    """가짜 장중 시세: 대부분 잔잔하고, 가끔 대금이 몰리며 가격이 뛰는 종목이 나온다."""

    def __init__(self, uni, seed=7):
        self.rng = np.random.default_rng(seed)
        self.state = {c: {"name": n, "price": 10000.0 + 1000 * i, "pct": 0.0, "value": 0.0, "strength": 100.0}
                      for i, (c, n) in enumerate(zip(uni["code"], uni["name"]))}

    def quotes(self, codes):
        out = []
        for c in codes:
            s = self.state[c]
            burst = self.rng.random() < 0.06
            dv = self.rng.lognormal(19.2, 0.9) + (self.rng.uniform(6, 20) * 1e8 if burst else 0)
            dp = self.rng.normal(0, 0.5) + (self.rng.uniform(1.5, 3.5) if burst else 0)
            s["value"] += dv
            s["pct"] = float(np.clip(s["pct"] + dp, -29.9, 29.9))
            s["price"] = round(10000 * (1 + s["pct"] / 100), -1)
            s["strength"] = float(np.clip(s["strength"] + self.rng.normal(0, 8) + (20 if burst else 0), 30, 300))
            out.append({"code": c, "name": s["name"], "price": s["price"], "pct": round(s["pct"], 2),
                        "value": s["value"], "strength": round(s["strength"], 2)})
        return pd.DataFrame(out)

    def market_rank(self, n=300):
        order = sorted(self.state, key=lambda c: -self.state[c]["value"])
        return {c: 20 + i * 3 for i, c in enumerate(order)}


def _demo_history(con, src, today, w):
    for d in lookback_dates(src, today, w["lookback_days"]):
        df = src.value_top(d, w["top_n"])
        df["change_pct"] = 25.0          # 데모: 전부 감시 대상이 되도록
        for r in df.itertuples():
            con.execute("INSERT OR REPLACE INTO value_rank_daily VALUES (?,?,?,?,?,?,?)",
                        (iso(d), r.code, r.name, int(r.rank), float(r.value), 25.0, float(r.close)))
    con.commit()
