"""에버노트 대신 Markdown 종목 노트 / 일일 리포트 생성.

종목 노트의 '## 내 메모' 아래 내용은 자동 갱신 시에도 보존됩니다.
"""
from __future__ import annotations

import json
import re

from .util import fmt_eok, fmt_num

MEMO_HEAD = "## 내 메모"


def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name or "")


def note_path(cfg, code, name):
    return cfg.sub("notes") / f"{code}_{_safe(name)}.md"


def write_stock_note(cfg, con, code):
    s = con.execute("SELECT * FROM stocks WHERE code=?", (code,)).fetchone()
    if not s:
        return None
    path = note_path(cfg, code, s["name"])
    memo = "(이 아래는 자동 갱신 시에도 지워지지 않습니다. 직접 분석한 내용을 적으세요.)\n"
    if path.exists():
        old = path.read_text(encoding="utf-8")
        if MEMO_HEAD in old:
            memo = old.split(MEMO_HEAD, 1)[1].lstrip("\n")

    ev = con.execute("SELECT * FROM events WHERE code=? ORDER BY date DESC", (code,)).fetchall()
    kw = con.execute("SELECT keyword, count FROM keywords WHERE code=? ORDER BY count DESC LIMIT 15",
                     (code,)).fetchall()
    rel = con.execute("""SELECT s.code, s.name, r.count FROM relations r JOIN stocks s ON s.code=r.code_b
                         WHERE r.code_a=? ORDER BY r.count DESC LIMIT 10""", (code,)).fetchall()
    nw = con.execute("SELECT * FROM news WHERE code=? ORDER BY date DESC LIMIT 25", (code,)).fetchall()
    ds = con.execute("SELECT * FROM disclosures WHERE code=? ORDER BY date DESC LIMIT 10", (code,)).fetchall()
    fin = con.execute("SELECT * FROM financials WHERE code=? ORDER BY year DESC", (code,)).fetchall()
    sig = con.execute("SELECT * FROM signals WHERE code=? ORDER BY date DESC LIMIT 10", (code,)).fetchall()

    L = [f"# {s['name']} ({code})", ""]
    L += ["## 요약", ""]
    L.append(f"- 시장: {s['market'] or '-'}  ·  시총: {fmt_eok(s['marcap'])}")
    if s["major_holder_pct"] is not None:
        L.append(f"- 최대주주 및 특수관계인 지분: {s['major_holder_pct']}%")
    if s["sector"]:
        L.append(f"- 업종코드(DART): {s['sector']}")
    if s["homepage"]:
        L.append(f"- 홈페이지: {s['homepage']}")
    if fin:
        profit_years = sum(1 for f in fin if (f["net_income"] or 0) > 0)
        L.append(f"- 최근 {len(fin)}년 중 {profit_years}년 흑자")
    L.append(f"- 상한가/천만주 기록: {len(ev)}회 (최초 {s['first_seen']}, 최근 {s['last_seen']})")
    if kw:
        L.append("- 키워드: " + ", ".join(f"{k['keyword']}({k['count']})" for k in kw))
    if rel:
        L.append("- 연관 종목: " + ", ".join(f"{r['name']}({r['code']})" for r in rel))
    L.append("")

    L += ["## 이벤트 기록 (왜 올랐나)", "", "| 날짜 | 구분 | 등락률 | 거래량 | 사유 |", "|---|---|---|---|---|"]
    for e in ev:
        L.append(f"| {e['date']} | {e['type']} | {e['change_pct']:+.2f}% | {fmt_num(e['volume'])} | "
                 f"{(e['reason'] or '').replace('|', '/')} |")
    L.append("")
    if (cfg.sub("charts") / f"{code}.png").exists():
        L += ["## 차트", "", f"![chart](../charts/{code}.png)", "",
              "노란 세로선 = 상한가/천만주 기록일", ""]
    if fin:
        L += ["## 재무 추이 (DART, 억 원)", "", "| 연도 | 매출액 | 영업이익 | 당기순이익 |", "|---|---|---|---|"]
        for f in fin:
            L.append(f"| {f['year']} | {fmt_eok(f['revenue'])} | {fmt_eok(f['op_income'])} | {fmt_eok(f['net_income'])} |")
        L.append("")
    if ds:
        L += ["## 전자공시 (최근)", ""]
        for x in ds:
            L.append(f"- {x['date']} [{x['title']}](https://dart.fss.or.kr/dsaf001/main.do?rcpNo={x['rcept_no']})")
        L.append("")
    if sig:
        L += ["## 시그널 기록", ""]
        for x in sig:
            res = "" if x["max_ret"] is None else f" → 최고 {x['max_ret']:+.1f}% {'적중' if x['hit'] else '미적중'}"
            L.append(f"- {x['date']} (점수 {x['score']:.0f}) {x['estimate']}{res}")
        L.append("")
    L += ["## 관련 기사", ""]
    for n in nw:
        L.append(f"- {n['date']} [{n['title']}]({n['link']}) {n['source'] or ''}")
    L += ["", MEMO_HEAD, "", memo.rstrip() + "\n"]
    path.write_text("\n".join(L), encoding="utf-8")
    return path


def evening_report(cfg, con, stage, review, track_res, paper_res):
    d = review["date"]
    items = review["items"]
    L = [f"# 이브닝 리포트 {d}", "", f"**{stage.name}** (Day {stage.day}) — {stage.desc}", ""]
    focus = [i for i in items if i["focus"]]
    rep = [i for i in items if i["repeat_n"] > 0]
    L += [f"- 대상 종목 {len(items)}개 · 상한가 {sum('상한가' in i['types'] for i in items)}개 · "
          f"주력 분야 {len(focus)}개 · 반복 등장 {len(rep)}개", ""]
    if rep:
        L += ["## 반복 등장 (시장은 반복된다)", ""]
        for i in rep:
            last = i["repeat"][0]
            L.append(f"- **{i['name']}** — 이전 {i['repeat_n']}회, 최근 {last['date']} ({last['reason']})")
        L.append("")
    if focus:
        L += ["## 주력 분야", ""]
        for i in focus:
            L.append(f"- [{'/'.join(i['focus'])}] **{i['name']}** {i['change_pct']:+.2f}% — {i['reason']}")
        L.append("")
    L += ["## 상한가 · 천만 주 정리", "", "| 종목 | 구분 | 등락률 | 거래량 | 시총 | 사유 | 키워드 |",
          "|---|---|---|---|---|---|---|"]
    for i in sorted(items, key=lambda x: -x["change_pct"]):
        link = f"[{i['name']}](../notes/{note_path(cfg, i['code'], i['name']).name})"
        L.append(f"| {link} | {i['types']} | {i['change_pct']:+.2f}% | {fmt_num(i['volume'])} | "
                 f"{fmt_eok(i['marcap'])} | {i['reason'].replace('|', '/')} | {', '.join(i['keywords'][:5])} |")
    L.append("")
    if track_res:
        L += track_section(track_res)
    if paper_res:
        L += paper_section(paper_res)
    path = cfg.sub("reports") / f"{d}_evening.md"
    path.write_text("\n".join(L), encoding="utf-8")
    return path


def track_section(t):
    L = ["## 추정 → 관찰 → 기록 (시그널 추적)", ""]
    s = t["stats"]
    if s["n"]:
        L.append(f"- 누적 시그널 {s['n']}개 · 적중 {s['hits']}개 · 적중률 {s['hit_rate']:.0f}% · "
                 f"평균 최고수익 {s['avg_max']:+.1f}%")
    for x in t["closed"]:
        L.append(f"- {x['date']} {x['name']}: 최고 {x['max_ret']:+.1f}% → {'적중' if x['hit'] else '미적중'} "
                 f"(근거: {x['basis']})")
    L.append("")
    return L


def paper_section(p):
    L = ["## 소액 매매 (모의)", ""]
    for x in p["today"]:
        L.append(f"- {x['name']} {x['qty']}주 · 진입 {fmt_num(x['entry'])} → 청산 {fmt_num(x['exit'])} "
                 f"({x['exit_reason']}) · 손익 {fmt_num(x['pnl'])}원 ({x['pnl_pct']:+.2f}%)")
    s = p["stats"]
    if s["n"]:
        L.append(f"- 누적 {s['n']}회 · 승률 {s['win_rate']:.0f}% · 누적손익 {fmt_num(s['pnl'])}원")
    L.append("")
    return L


def morning_report(cfg, stage, date, heads, signals):
    L = [f"# 데일리(새벽) 리포트 {date}", "", f"**{stage.name}** (Day {stage.day})", "",
         "지식 → 시그널 → 실행 전략 결정 → 주시 → 변화 감지 → 대응 → 반성 → 반복", ""]
    L += ["## 시그널 (과거 복습 ↔ 오늘 뉴스 연결)", ""]
    if not signals:
        L.append("- 오늘은 연결되는 시그널이 없습니다.")
    for s in signals:
        L.append(f"### {s['name']} ({s['code']}) — 점수 {s['score']:.0f}")
        L.append(f"- 추정: {s['estimate']}")
        L.append(f"- 근거: {s['basis']}")
        for h in s["heads"][:3]:
            L.append(f"- 뉴스: [{h['title']}]({h['link']})")
        L.append("")
    L += ["## 읽은 헤드라인", ""]
    for h in heads[:80]:
        L.append(f"- [{h.title}]({h.link}) {h.source}")
    path = cfg.sub("reports") / f"{date}_morning.md"
    path.write_text("\n".join(L), encoding="utf-8")
    return path
