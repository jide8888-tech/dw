"""매수 신호 이후의 움직임 평가 — '종가'가 아니라 '신호 뒤 얼마나 올랐다가 얼마나 빠졌나'.

가격 경로는 저녁에 받아 둔 1분봉(minute_bars, 고가·저가 포함)을 우선 쓰고, 없으면 장중 5분 시세(watch_ticks2)를 쓴다.
신호가 난 그 분(分)의 봉은 신호 전 움직임이 섞일 수 있어 제외하고 다음 분부터 본다.

신호마다
  - 최고 상승률·시각·걸린 시간 / 최대 하락률·시각
  - +tp% 와 -sl% 중 무엇에 먼저 닿았나
  - 10·30·60분 안 최고·최저
  - 매도 방법별 결과: 고정 익절·손절 / 고점 대비 -3%·-5% 추적 / 절반 익절 + 나머지 추적 / 종가 보유
같은 봉에서 손절선과 목표가를 모두 건드리면 손절이 먼저라고 본다 (보수적).
"""
from __future__ import annotations

import datetime as dt

from .util import iso


def _hm(t: str) -> dt.datetime:
    return dt.datetime.strptime(t, "%H:%M")


def _mins(a: str, b: str) -> int:
    return int((_hm(b) - _hm(a)).total_seconds() // 60)


def _pct(x, entry):
    return round((x / entry - 1) * 100, 2)


def exit_cfg(cfg) -> dict:
    e = cfg["watch"].get("exit", {})
    return {"tp": float(e.get("take_profit_pct", 3.0)), "sl": float(e.get("stop_loss_pct", 3.0)),
            "end": e.get("end_time", "15:20"), "trails": [float(x) for x in e.get("trail_pcts", [3.0, 5.0])],
            "half_tp": float(e.get("half_take_pct", 3.0)), "half_trail": float(e.get("half_trail_pct", 5.0))}


def strategies(cfg) -> list[tuple[str, str]]:
    x = exit_cfg(cfg)
    out = [("fixed", f"고정 +{x['tp']:g}/-{x['sl']:g}")]
    out += [(f"trail{t:g}", f"고점-{t:g}%") for t in x["trails"]]
    out += [("half", f"절반+{x['half_tp']:g}% 후 고점-{x['half_trail']:g}%"), ("close", "종가 보유")]
    return out


def price_path(con, o, end: str):
    """[(시각, 고가, 저가, 종가)], 출처"""
    bars = con.execute("SELECT time, high, low, close FROM minute_bars WHERE date=? AND code=? AND time>? AND time<=? "
                       "ORDER BY time", (o["date"], o["code"], o["time"], end)).fetchall()
    if bars:
        return [(b["time"], b["high"], b["low"], b["close"]) for b in bars], "1분봉"
    ticks = con.execute("SELECT time, price FROM watch_ticks2 WHERE date=? AND grp=? AND code=? AND time>? AND time<=? "
                        "ORDER BY time", (o["date"], o["grp"], o["code"], o["time"], end)).fetchall()
    return [(t["time"], t["price"], t["price"], t["price"]) for t in ticks], "5분 시세"


def _fill(level, close, exact):
    """1분봉이면 손절·목표 가격에 체결된 것으로, 5분 시세면 그때 본 가격으로"""
    return level if exact else close


def run_strategies(entry, pts, x, exact, rclose=None) -> dict:
    out = {}
    end_px = pts[-1][3] if pts else None

    def finish(px, why, t):
        return (_pct(px, entry), why, t)

    # 고정 익절·손절
    res = None
    up, dn = entry * (1 + x["tp"] / 100), entry * (1 - x["sl"] / 100)
    for t, hi, lo, cl in pts:
        if lo <= dn:
            res = finish(_fill(dn, cl, exact), "손절", t)
            break
        if hi >= up:
            res = finish(_fill(up, cl, exact), "익절", t)
            break
    out["fixed"] = res or (finish(end_px, f"{x['end']} 정리", pts[-1][0]) if pts else None)

    # 고점 대비 추적 손절
    for tr in x["trails"]:
        peak, res = entry, None
        for t, hi, lo, cl in pts:
            stop = peak * (1 - tr / 100)
            if lo <= stop:
                res = finish(_fill(stop, cl, exact), "추적 매도", t)
                break
            peak = max(peak, hi)
        out[f"trail{tr:g}"] = res or (finish(end_px, f"{x['end']} 정리", pts[-1][0]) if pts else None)

    # 절반 익절 + 나머지 추적
    res, half, peak = None, None, entry
    hup = entry * (1 + x["half_tp"] / 100)
    for t, hi, lo, cl in pts:
        if half is None:
            if lo <= dn:
                res = finish(_fill(dn, cl, exact), "손절", t)
                break
            if hi >= hup:
                half = _fill(hup, cl, exact)
                peak = hi
            continue
        stop = peak * (1 - x["half_trail"] / 100)
        if lo <= stop:
            res = ((_pct(half, entry) + _pct(_fill(stop, cl, exact), entry)) / 2, "절반 익절+추적 매도", t)
            break
        peak = max(peak, hi)
    if res is None and pts:
        rest = _pct(end_px, entry)
        res = (round((_pct(half, entry) + rest) / 2, 2), "절반 익절+정리", pts[-1][0]) if half is not None \
            else finish(end_px, f"{x['end']} 정리", pts[-1][0])
    out["half"] = (round(res[0], 2), res[1], res[2]) if res else None

    out["close"] = (rclose, "종가", "15:30") if rclose is not None else \
        (finish(end_px, f"{x['end']} 가격", pts[-1][0]) if pts else None)
    return out


def evaluate(con, cfg, date=None) -> list[dict]:
    """매수 신호마다 움직임과 매도 방법별 결과. date=None 이면 전체 기간"""
    x = exit_cfg(cfg)
    cond, args = ("WHERE date=?", (iso(date),)) if date else ("", ())
    rows = []
    for o in con.execute(f"SELECT * FROM watch_outcomes {cond} ORDER BY date, time", args).fetchall():
        if not o["entry"]:
            continue
        pts, src = price_path(con, o, x["end"])
        e = o["entry"]
        r = dict(o)
        r["source"] = src
        if pts:
            ti, hi = max(((p[0], p[1]) for p in pts), key=lambda z: z[1])
            tl, lo = min(((p[0], p[2]) for p in pts), key=lambda z: z[1])
            r.update(up=_pct(hi, e), up_time=ti, up_min=_mins(o["time"], ti), down=_pct(lo, e), down_time=tl)
            first = "둘 다 아님"
            for t, h, low, c in pts:
                hit_up, hit_dn = h >= e * (1 + x["tp"] / 100), low <= e * (1 - x["sl"] / 100)
                if hit_up and hit_dn:
                    first = "같은 봉"
                    break
                if hit_up:
                    first = "상승 먼저"
                    break
                if hit_dn:
                    first = "하락 먼저"
                    break
            r["first"] = first
            for w in (10, 30, 60):
                win = [p for p in pts if _mins(o["time"], p[0]) <= w]
                r[f"up{w}"] = _pct(max(p[1] for p in win), e) if win else None
                r[f"dn{w}"] = _pct(min(p[2] for p in win), e) if win else None
        r["strat"] = run_strategies(e, pts, x, exact=(src == "1분봉"), rclose=o["rclose"])
        rows.append(r)
    return rows


def summarize(rs: list[dict], cfg) -> dict:
    """평균 최고·최대하락·상승먼저 비율·최고까지 시간 + 매도 방법별 평균·승률"""
    def avg(vals):
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 2) if vals else None
    s = {"n": len(rs), "up": avg(r.get("up") for r in rs), "down": avg(r.get("down") for r in rs),
         "up_min": avg(r.get("up_min") for r in rs)}
    decided = [r for r in rs if r.get("first") in ("상승 먼저", "하락 먼저", "같은 봉")]
    s["first_up"] = round(sum(r["first"] == "상승 먼저" for r in decided) / len(decided) * 100, 1) if decided else None
    best, best_v = None, None
    for key, name in strategies(cfg):
        vals = [r["strat"][key][0] for r in rs if r["strat"].get(key) and r["strat"][key][0] is not None]
        s[key] = round(sum(vals) / len(vals), 2) if vals else None
        s[key + "_win"] = round(sum(v > 0 for v in vals) / len(vals) * 100, 1) if vals else None
        if s[key] is not None and (best_v is None or s[key] > best_v):
            best, best_v = name, s[key]
    s["best"] = best
    return s
