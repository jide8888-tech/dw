"""복습 (Day 4~): 상한가·천만 주 종목을 찾아 사유/기사/공시/차트를 종목 노트로 정리."""
from __future__ import annotations

import datetime as dt
import json

from . import analyze, chart, notes
from .util import iso, to_date


def known_names(con) -> dict[str, str]:
    return {r["name"]: r["code"] for r in con.execute("SELECT code, name FROM stocks") if r["name"]}


def run(cfg, con, src, news, dart, date, *, with_profile=True, with_chart=True, quiet=False) -> dict:
    d = iso(date)
    f = cfg["filters"]
    movers = src.movers(date, f["upper_limit_pct"], f["volume_min"])
    if movers.empty:
        if not quiet:
            print(f"[{d}] 대상 종목 없음 (휴장일이거나 데이터 없음)")
        return {"date": d, "items": []}
    names = known_names(con)
    names.update({r.name: r.code for r in movers.itertuples()})
    items = []
    for r in movers.itertuples():
        types = ",".join(t for t, ok in (("상한가", r.is_upper), ("천만주", r.is_volume)) if ok)
        prev = con.execute("SELECT date, type, reason FROM events WHERE code=? AND date<? ORDER BY date DESC",
                           (r.code, d)).fetchall()
        arts = news.search(r.name, around=to_date(date))
        titles = [a.title for a in arts]
        reason = analyze.reason(r.name, titles)
        kws = analyze.keywords(titles, exclude={r.name})
        focus = analyze.focus_tags(" ".join(titles), cfg["focus_sectors"])

        now = dt.datetime.now().isoformat(timespec="seconds")
        con.execute("""INSERT INTO stocks(code, name, market, marcap, first_seen, last_seen, updated)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(code) DO UPDATE SET name=excluded.name, market=excluded.market,
                         marcap=excluded.marcap, last_seen=MAX(last_seen, excluded.last_seen)""",
                    (r.code, r.name, r.market, _i(r.marcap), d, d, now))
        con.execute("UPDATE stocks SET first_seen=MIN(first_seen, ?) WHERE code=?", (d, r.code))
        con.execute("""INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (d, r.code, types, r.close, r.change_pct, _i(r.volume), _i(r.value), _i(r.marcap),
                     reason, json.dumps([k for k, _ in kws], ensure_ascii=False), ",".join(focus)))
        for a in arts:
            con.execute("INSERT OR IGNORE INTO news VALUES (?,?,?,?,?)",
                        (r.code, a.date or d, a.title, a.link, a.source))
        for k, n in kws:
            con.execute("""INSERT INTO keywords VALUES (?,?,?)
                           ON CONFLICT(code, keyword) DO UPDATE SET count=count+excluded.count""",
                        (r.code, k, n))
        for other, oc in names.items():          # 기사에 함께 나온 종목 → 연관 관계
            if oc != r.code and len(other) >= 2 and any(other in t for t in titles):
                for a_, b_ in ((r.code, oc), (oc, r.code)):
                    con.execute("""INSERT INTO relations VALUES (?,?,1)
                                   ON CONFLICT(code_a, code_b) DO UPDATE SET count=count+1""", (a_, b_))
        if with_profile:
            _profile(con, dart, r.code)
        items.append({"code": r.code, "name": r.name, "market": r.market, "types": types,
                      "close": r.close, "change_pct": r.change_pct, "volume": _i(r.volume),
                      "marcap": _i(r.marcap), "reason": reason, "keywords": [k for k, _ in kws],
                      "focus": focus, "repeat": [dict(p) for p in prev[:3]], "repeat_n": len(prev)})
    con.commit()
    for it in items:
        if with_chart:
            _chart(cfg, con, src, it["code"], it["name"], date)
        notes.write_stock_note(cfg, con, it["code"])
    if not quiet:
        print(f"[{d}] 복습 완료: {len(items)}종목 (상한가 {sum('상한가' in i['types'] for i in items)}, "
              f"반복 등장 {sum(i['repeat_n'] > 0 for i in items)})")
    return {"date": d, "items": items}


def _i(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def _profile(con, dart, code):
    row = con.execute("SELECT updated, sector FROM stocks WHERE code=?", (code,)).fetchone()
    if not dart.enabled:
        return
    if row and row["sector"] and row["updated"] and \
            row["updated"] > (dt.datetime.now() - dt.timedelta(days=30)).isoformat():
        return
    try:
        p = dart.profile(code)
    except Exception as e:
        print(f"[warn] DART {code}: {e}")
        return
    con.execute("UPDATE stocks SET sector=?, homepage=?, major_holder_pct=?, updated=? WHERE code=?",
                (p.get("sector"), p.get("homepage"), p.get("major_holder_pct"),
                 dt.datetime.now().isoformat(timespec="seconds"), code))
    for x in p["disclosures"]:
        con.execute("INSERT OR IGNORE INTO disclosures VALUES (?,?,?,?)",
                    (code, x["date"], x["title"], x["rcept_no"]))
    for x in p["financials"]:
        con.execute("INSERT OR REPLACE INTO financials VALUES (?,?,?,?,?)",
                    (code, x["year"], x.get("revenue"), x.get("op_income"), x.get("net_income")))
    con.commit()


def _chart(cfg, con, src, code, name, date):
    end = to_date(date)
    try:
        df = src.ohlcv(code, end - dt.timedelta(days=365), end)
    except Exception as e:
        print(f"[warn] 차트 데이터 실패 {code}: {e}")
        return
    ev = [r["date"] for r in con.execute("SELECT date FROM events WHERE code=?", (code,))]
    chart.draw(df, f"{name} ({code})", ev, cfg.sub("charts") / f"{code}.png")
