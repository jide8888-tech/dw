"""예습 (Day 41~): 새벽 뉴스를 읽고, 복습으로 쌓은 지식(종목명·키워드·연관 종목)과 연결해 시그널을 만든다."""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

from . import analyze
from .util import iso, to_date

GENERIC_DF = 6   # 6개 이상 종목에 공통인 키워드는 변별력이 없어 제외


def build_index(con):
    names = {r["name"]: r["code"] for r in con.execute("SELECT code, name FROM stocks")
             if r["name"] and len(r["name"]) >= 2}
    kw = defaultdict(list)
    for r in con.execute("SELECT code, keyword, count FROM keywords"):
        kw[r["keyword"]].append((r["code"], r["count"]))
    kw = {k: v for k, v in kw.items() if len(v) < GENERIC_DF and k not in names}
    rel = defaultdict(list)
    for r in con.execute("SELECT code_a, code_b, count FROM relations"):
        rel[r["code_a"]].append((r["code_b"], r["count"]))
    return names, kw, rel


def score_headlines(con, heads, focus):
    names, kw, rel = build_index(con)
    code2name = {v: k for k, v in names.items()}
    agg: dict[str, dict] = {}

    def add(code, pts, basis, h):
        a = agg.setdefault(code, {"code": code, "score": 0.0, "basis": [], "heads": []})
        a["score"] += pts
        if basis not in a["basis"]:
            a["basis"].append(basis)
        if all(x["link"] != h.link for x in a["heads"]):
            a["heads"].append({"title": h.title, "link": h.link})

    for h in heads:
        t = h.title
        direct = [c for n, c in names.items() if n in t]
        for c in direct:
            add(c, 3, f"'{code2name[c]}' 직접 언급", h)
            for b, n in rel.get(c, []):
                add(b, 2 if n < 3 else 3, f"연관 종목 '{code2name[c]}' 뉴스", h)
        for tok in set(analyze.tokens(t)):
            for c, n in kw.get(tok, []):
                if c not in direct:
                    add(c, 1 + (0.5 if n >= 3 else 0), f"키워드 '{tok}'", h)
    out = []
    for c, a in agg.items():
        tags = analyze.focus_tags(" ".join(x["title"] for x in a["heads"]), focus)
        if tags:
            a["score"] += 0.5
            a["basis"].append("주력 분야(" + "/".join(tags) + ")")
        a["name"] = code2name.get(c) or c
        out.append(a)
    return sorted(out, key=lambda x: -x["score"])


def run(cfg, con, src, news, date, quiet=False):
    d = iso(date)
    heads = news.headlines(cfg["morning"]["queries"])
    for h in heads:
        con.execute("INSERT OR IGNORE INTO headlines VALUES (?,?,?,?)", (d, h.title, h.link, h.source))
    cands = [s for s in score_headlines(con, heads, cfg["focus_sectors"])
             if s["score"] >= cfg["morning"]["min_score"]][:20]
    for s in cands:
        if not s.get("name") or s["name"] == s["code"]:
            row = con.execute("SELECT name FROM stocks WHERE code=?", (s["code"],)).fetchone()
            s["name"] = row["name"] if row else s["code"]
        last = con.execute("SELECT date, type, reason FROM events WHERE code=? AND date<? "
                           "ORDER BY date DESC LIMIT 1", (s["code"], d)).fetchone()
        hist = f"{last['date']} {last['type']}(사유: {last['reason']})" if last else "과거 기록"
        s["estimate"] = f"{hist} → 오늘 '{s['heads'][0]['title']}' → 상승 가능성 추정"
        base = None
        try:
            df = src.ohlcv(s["code"], to_date(date) - dt.timedelta(days=15), to_date(date) - dt.timedelta(days=1))
            if not df.empty:
                base = float(df["close"].iloc[-1])
        except Exception as e:
            print(f"[warn] 기준가 조회 실패 {s['code']}: {e}")
        s["basis"] = ", ".join(s["basis"])
        con.execute("""INSERT INTO signals(date, code, score, basis, headline, link, estimate, base_close)
                       VALUES (?,?,?,?,?,?,?,?)
                       ON CONFLICT(date, code) DO UPDATE SET score=excluded.score, basis=excluded.basis,
                         headline=excluded.headline, link=excluded.link, estimate=excluded.estimate""",
                    (d, s["code"], s["score"], s["basis"], s["heads"][0]["title"], s["heads"][0]["link"],
                     s["estimate"], base))
    con.commit()
    if not quiet:
        print(f"[{d}] 예습 완료: 헤드라인 {len(heads)}개 → 시그널 {len(cands)}개")
    return heads, cands
