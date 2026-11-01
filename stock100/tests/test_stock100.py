import datetime as dt
import io

import pandas as pd

from stock100 import analyze, challenge
from stock100.__main__ import App
from stock100.config import Config
from stock100.sources.news import Article
from stock100.sources.prices import NaverSource
from stock100 import preview


def test_keywords_and_reason():
    titles = ["[특징주] 에스엠랩, 폭스바겐과 배터리 합작사 설립에 상한가",
              "에스엠랩 세계 최초 니켈 기술 구현… DSC인베스트먼트 급등"]
    kws = [k for k, _ in analyze.keywords(titles, exclude={"에스엠랩"})]
    assert "폭스바겐" in kws and "상한가" not in kws and "상한" not in kws
    assert analyze.reason("에스엠랩", titles).startswith("[특징주]")


def test_stage_sizes():
    cfg = Config.load(None, {"mode": "challenge", "challenge_start": "2026-01-01"})
    s = challenge.current(cfg, dt.date(2026, 1, 1) + dt.timedelta(days=74))
    assert s.day == 75 and s.position_size(50_000) == 1
    s = challenge.current(cfg, dt.date(2026, 1, 1) + dt.timedelta(days=87))
    assert s.position_size(10_000) == 100
    s = challenge.current(cfg, dt.date(2026, 1, 1) + dt.timedelta(days=95))
    assert s.position_size(10_000) == 500
    assert "preview" not in challenge.current(cfg, dt.date(2026, 1, 20)).features


def test_naver_table_parse():
    html = """<table><tr><th>N</th><th>종목명</th><th>현재가</th><th>전일비</th><th>등락률</th>
    <th>거래량</th><th>거래대금</th><th>시가총액</th></tr>
    <tr><td>1</td><td><a href="/item/main.naver?code=053610" class="tltle">프로텍</a></td>
    <td>27,400</td><td>6,300</td><td>+29.86%</td><td>12,345,678</td><td>330,000</td><td>3,003</td></tr></table>"""
    src = NaverSource()
    names = dict((n, c) for c, n in src.LINK.findall(html))
    t = pd.read_html(io.StringIO(html))[0]
    out = src._norm(t, names, "KOSDAQ")
    r = out.iloc[0]
    assert r.code == "053610" and r.close == 27400 and abs(r.change_pct - 29.86) < 1e-6
    assert r.volume == 12345678 and r.marcap == 3003e8


def test_demo_end_to_end(tmp_path):
    cfg = Config.load(None, {"sources": {"prices": "demo"}, "data_dir": str(tmp_path), "mode": "full",
                             "filters": {"volume_min": 3_000_000}})
    app = App(cfg)
    days = app.src.trading_days(dt.date.today(), 25)
    for d in days:
        app.morning(d, quiet=True)
        app.evening(d, quiet=True)
    assert app.con.execute("SELECT COUNT(*) FROM events").fetchone()[0] > 0
    assert app.con.execute("SELECT COUNT(*) FROM signals WHERE status='closed'").fetchone()[0] > 0
    assert app.con.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0] > 0
    assert list((tmp_path / "notes").glob("053610_*.md"))
    assert list((tmp_path / "reports").glob("*_morning.md"))
    # 연관 종목 시그널: 삼성전자 뉴스 → 프로텍
    heads = [Article("삼성전자, 차세대 HBM 패키징 투자 확대", "x", "")]
    sc = {s["code"]: s for s in preview.score_headlines(app.con, heads, cfg["focus_sectors"])}
    assert "053610" in sc and "연관" in sc["053610"]["basis"][0] + "".join(sc["053610"]["basis"])
