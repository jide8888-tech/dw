"""추적 (Day 51~) + 소액 매매 모의 (Day 71~)."""
from __future__ import annotations

import datetime as dt
import json

import pandas as pd

from .util import iso, to_date


def update_signals(cfg, con, src, asof, quiet=False):
    hz = sorted(cfg["track"]["horizons"])
    hit_pct = cfg["track"]["hit_pct"]
    asof = to_date(asof)
    closed = []
    for s in con.execute("SELECT * FROM signals WHERE status='open' AND date<=?", (iso(asof),)).fetchall():
        sd = to_date(s["date"])
        try:
            df = src.ohlcv(s["code"], sd - dt.timedelta(days=15), asof)
        except Exception as e:
            print(f"[warn] 추적 실패 {s['code']}: {e}")
            continue
        before = df[df.index < pd.Timestamp(sd)]
        after = df[df.index >= pd.Timestamp(sd)]
        base = s["base_close"] or (float(before["close"].iloc[-1]) if len(before) else None)
        if not base or after.empty:
            continue
        rets = {f"D+{h}": round((after["close"].iloc[h - 1] / base - 1) * 100, 2)
                for h in hz if len(after) >= h}
        win = after.iloc[: hz[-1]]
        max_ret = round((win["high"].max() / base - 1) * 100, 2)
        done = len(after) >= hz[-1]
        hit = int(max_ret >= hit_pct)
        con.execute("UPDATE signals SET base_close=?, ret_json=?, max_ret=?, hit=?, status=? WHERE id=?",
                    (base, json.dumps(rets), max_ret, hit, "closed" if done else "open", s["id"]))
        if done:
            name = con.execute("SELECT name FROM stocks WHERE code=?", (s["code"],)).fetchone()
            closed.append({"date": s["date"], "code": s["code"], "name": name["name"] if name else s["code"],
                           "max_ret": max_ret, "hit": hit, "basis": s["basis"], "rets": rets})
    con.commit()
    st = con.execute("SELECT COUNT(*) n, COALESCE(SUM(hit),0) hits, AVG(max_ret) avg_max "
                     "FROM signals WHERE status='closed'").fetchone()
    stats = {"n": st["n"], "hits": st["hits"], "hit_rate": (st["hits"] / st["n"] * 100) if st["n"] else 0,
             "avg_max": st["avg_max"] or 0}
    if not quiet:
        print(f"[{iso(asof)}] 추적: 종료 {len(closed)}건 · 누적 적중률 {stats['hit_rate']:.0f}% ({stats['hits']}/{stats['n']})")
    return {"closed": closed, "stats": stats}


def paper_trade(cfg, con, src, stage, date, quiet=False):
    """오늘 시그널 상위 종목을 시가 매수 → 익절(욕심 라인)/손절/종가 청산으로 모의 체결.
    실제 주문은 내지 않는다. 같은 날 고가·저가 순서는 알 수 없으므로 손절을 먼저 가정(보수적)."""
    p = cfg["paper"]
    d = iso(date)
    today = []
    if p.get("enabled", True):
        sigs = con.execute("SELECT * FROM signals WHERE date=? ORDER BY score DESC LIMIT ?",
                           (d, int(p["max_per_day"]))).fetchall()
        for s in sigs:
            try:
                df = src.ohlcv(s["code"], to_date(date), to_date(date))
            except Exception as e:
                print(f"[warn] 모의매매 시세 실패 {s['code']}: {e}")
                continue
            if df.empty:
                continue
            bar = df.iloc[-1]
            entry = float(bar["open"])
            qty = stage.position_size(entry)
            if qty <= 0:
                continue
            tp = entry * (1 + p["take_profit_pct"] / 100)
            sl = entry * (1 - p["stop_loss_pct"] / 100)
            if bar["low"] <= sl:
                ex, why = sl, "손절"
            elif bar["high"] >= tp:
                ex, why = tp, "익절(욕심 신호)"
            else:
                ex, why = float(bar["close"]), "종가 청산"
            pnl = (ex - entry) * qty
            con.execute("""INSERT OR REPLACE INTO paper_trades(date, code, stage, qty, entry, exit, exit_reason,
                           pnl, pnl_pct, signal_id) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (d, s["code"], stage.name, qty, entry, round(ex, 1), why, round(pnl),
                         round((ex / entry - 1) * 100, 2), s["id"]))
            name = con.execute("SELECT name FROM stocks WHERE code=?", (s["code"],)).fetchone()
            today.append({"name": name["name"] if name else s["code"], "qty": qty, "entry": entry,
                          "exit": ex, "exit_reason": why, "pnl": pnl, "pnl_pct": (ex / entry - 1) * 100})
        con.commit()
    st = con.execute("SELECT COUNT(*) n, SUM(pnl>0) w, COALESCE(SUM(pnl),0) pnl FROM paper_trades").fetchone()
    stats = {"n": st["n"], "win_rate": (st["w"] or 0) / st["n"] * 100 if st["n"] else 0, "pnl": st["pnl"]}
    if not quiet and today:
        print(f"[{d}] 모의매매 {len(today)}건 · 누적손익 {stats['pnl']:,.0f}원")
    return {"today": today, "stats": stats}
