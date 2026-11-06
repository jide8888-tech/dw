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


def test_theme_assign_and_excel(tmp_path, monkeypatch):
    from stock100 import excel, theme
    monkeypatch.chdir(tmp_path)           # config.example.yaml 의 사용자 테마 영향 제거
    cfg = Config.load(None, {"data_dir": str(tmp_path)})
    mk = lambda code, name, pct, titles, kws: {  # noqa: E731
        "code": code, "name": name, "market": "KOSDAQ", "types": "대금상위", "close": 1000, "change_pct": pct,
        "volume": 1, "value": 300e8, "marcap": 1e11, "reason": titles[0], "keywords": kws, "focus": [],
        "is_hot": True, "repeat": [], "repeat_n": 0,
        "articles": [{"title": t, "link": "https://x/" + code, "date": "2026-10-30", "source": "s"} for t in titles]}
    items = [mk("1", "가", 29.9, ["가, 휴머노이드 로봇 공개"], ["휴머노이드"]),
             mk("2", "나", 15.0, ["나, 로봇 부품 공급"], ["부품"]),
             mk("3", "다", 20.0, ["다, 양자암호 칩 개발"], ["양자암호", "칩"]),
             mk("4", "라", 12.0, ["라, 양자암호 통신 수주"], ["양자암호", "통신"]),
             mk("5", "마", 11.0, ["마, 최대주주 변경"], ["최대주주"])]
    con = __import__("stock100.db", fromlist=["connect"]).connect(tmp_path / "kb.sqlite")
    res = theme.build(cfg, con, {"date": "2026-10-30", "items": items})
    by = {t["theme"]: t for t in res["themes"]}
    assert by["로봇"]["n"] == 2 and by["로봇"]["leader"]["name"] == "가"
    assert by["로봇"]["status"] == "신규 형성"
    assert "#양자암호" in by and by["#양자암호"]["n"] == 2       # 사전에 없던 테마를 공통 키워드로 포착
    assert by["개별 이슈"]["n"] == 1
    p = excel.write(cfg, res, tmp_path / "t.xlsx")
    import openpyxl
    wb = openpyxl.load_workbook(p)
    assert wb.sheetnames == ["테마요약", "종목상세", "뉴스", "테마흐름(20일)", "기준"]
    assert wb["종목상세"].max_row == 6
    # 다음 날 로봇 종목 3개 → 확산
    items2 = [mk(c, n, 20, ["로봇 수혜 " + n], ["로봇"]) for c, n in (("1", "가"), ("2", "나"), ("6", "바"))]
    res2 = theme.build(cfg, con, {"date": "2026-10-31", "items": items2})
    assert res2["themes"][0]["status"] == "확산" and res2["themes"][0]["streak"] == 2


def test_kiwoom_source_with_fake_server(tmp_path, monkeypatch):
    """키움 서버 대신 가짜 응답으로 토큰·연속조회·단위 판별·필터를 검증."""
    import json as _json
    from stock100.sources import kiwoom as kw

    class R:
        def __init__(self, data, headers=None, status=200):
            self._d, self.headers, self.status_code = data, headers or {}, status
            self.content = _json.dumps(data).encode()
            self.text = self.content.decode()

        def json(self):
            return self._d

    calls = []
    today = dt.date.today().strftime("%Y%m%d")

    def fake_post(self, url, headers=None, json=None, timeout=None):
        calls.append((url, (headers or {}).get("api-id"), (headers or {}).get("cont-yn")))
        if url.endswith("/oauth2/token"):
            return R({"token": "T", "expires_dt": "29991231235959", "return_code": 0})
        api = headers["api-id"]
        if api == "ka10032":   # 거래대금 상위 (백만원 단위) — 2페이지
            if headers["cont-yn"] == "N":
                return R({"trde_prica_upper": [
                    {"stk_cd": "053610", "stk_nm": "프로텍", "cur_prc": "+27400", "flu_rt": "+29.86",
                     "now_trde_qty": "2000000", "trde_prica": "54800"}], "return_code": 0},
                    {"cont-yn": "Y", "next-key": "k1"})
            return R({"trde_prica_upper": [
                {"stk_cd": "A005930", "stk_nm": "삼성전자", "cur_prc": "-70000", "flu_rt": "-1.20",
                 "now_trde_qty": "100000", "trde_prica": "7000"}], "return_code": 0})
        if api == "ka10027":
            return R({"pred_pre_flu_rt_upper": [
                {"stk_cd": "111111", "stk_nm": "작은종목", "cur_prc": "+1000", "flu_rt": "+15.00",
                 "now_trde_qty": "500000"}], "return_code": 0})
        if api == "ka10030":
            return R({"tdy_trde_qty_upper": [
                {"stk_cd": "222222", "stk_nm": "대량종목", "cur_prc": "+500", "flu_rt": "+1.00",
                 "trde_qty": "25000000"}], "return_code": 0})
        if api == "ka10099":
            return R({"list": [{"code": "053610", "name": "프로텍", "listCount": "0000000011000000"}],
                      "return_code": 0})
        if api == "ka10081":
            return R({"stk_dt_pole_chart_qry": [
                {"dt": today, "open_pric": "+100", "high_pric": "+110", "low_pric": "-95", "cur_prc": "+105",
                 "trde_qty": "1000"}], "return_code": 0})
        raise AssertionError(api)

    monkeypatch.setattr(kw.requests.Session, "post", fake_post)
    monkeypatch.setattr(kw.time, "sleep", lambda s: None)
    monkeypatch.chdir(tmp_path)
    cfg = Config.load(None, {"data_dir": str(tmp_path), "markets": ["KOSDAQ"],
                             "keys": {"kiwoom_appkey": "AAAAAAAA", "kiwoom_secretkey": "S"}})
    src = kw.KiwoomSource(cfg)
    df = src.movers(dt.date.today(), cfg["filters"]).set_index("code")
    assert set(df.index) == {"053610", "222222"}             # 삼성전자(하락)·작은종목(대금 미달) 제외
    p = df.loc["053610"]
    assert p.value == 54800e6 and p.is_hot and p.is_upper and p.marcap == 27400 * 11_000_000
    assert df.loc["222222"].is_volume and not df.loc["222222"].is_hot
    assert ("https://api.kiwoom.com/api/dostk/rkinfo", "ka10032", "Y") in calls   # 연속조회
    o = src.ohlcv("053610", dt.date.today() - dt.timedelta(days=3), dt.date.today())
    assert o.iloc[-1].low == 95 and o.iloc[-1].close == 105
    n_token = sum(1 for c in calls if c[0].endswith("/oauth2/token"))
    kw.KiwoomSource(cfg).c.token()                            # 캐시된 토큰 재사용
    assert sum(1 for c in calls if c[0].endswith("/oauth2/token")) == n_token == 1


def test_watch_rules(tmp_path, monkeypatch):
    """5분 감시: 대상 선정(1주일 내 거래대금 100위+20%) / 주목(5억+1.5%p) / 순위 상승 매수(보유금 20%)."""
    import pandas as pd
    from stock100 import watch
    from stock100.db import connect
    monkeypatch.chdir(tmp_path)
    cfg = Config.load(None, {"data_dir": str(tmp_path)})
    con = connect(tmp_path / "kb.sqlite")

    class Src:
        def trading_days(self, end, n):
            return [dt.date(2026, 11, 2), dt.date(2026, 11, 3), dt.date(2026, 11, 4), dt.date(2026, 11, 5)][-n:]

        def value_top(self, date, n=100):
            return pd.DataFrame([
                {"code": "A", "name": "에이", "market": "", "close": 1, "change_pct": 25.0, "value": 9e10, "rank": 3},
                {"code": "B", "name": "비", "market": "", "close": 1, "change_pct": 21.0, "value": 5e10, "rank": 40},
                {"code": "C", "name": "씨", "market": "", "close": 1, "change_pct": 5.0, "value": 8e10, "rank": 5},
                {"code": "D", "name": "디", "market": "", "close": 1, "change_pct": 30.0, "value": 1e9, "rank": 101}])

    today = dt.date(2026, 11, 6)
    watch.ensure_history(cfg, con, Src(), today, quiet=True)
    uni = watch.universe(cfg, con, Src(), today)
    assert set(uni.code) == {"A", "B"}          # C: 등락률 미달, D: 100위 밖

    class Feed:
        def __init__(self):
            self.ticks = [
                {"A": (10000, 5.0, 100e8), "B": (5000, 3.0, 90e8)},
                {"A": (10100, 6.0, 102e8), "B": (5200, 5.0, 96e8)},    # B: +6억, +2.0%p 인데 순위 그대로(2위) → 주목만
                {"A": (10150, 6.5, 103e8), "B": (5400, 7.0, 104e8)},   # B: +8억, +2.0%p, 2위→1위 → 매수
                {"A": (10200, 7.0, 104e8), "B": (5600, 9.0, 112e8)},   # B: 또 조건 충족 → 재신호
            ]

        def quotes(self, codes):
            t = self.ticks.pop(0)
            return pd.DataFrame([{"code": c, "name": "", "price": p, "pct": r, "value": v, "strength": 100}
                                 for c, (p, r, v) in t.items()])

        def market_rank(self, n=300):
            return {}

    w = watch.Watcher(cfg, con, Feed(), uni, today)
    t0 = dt.datetime(2026, 11, 6, 9, 0)
    r0 = w.tick(t0)
    assert not r0["signals"]                       # 첫 회차 = 기준값
    r1 = w.tick(t0 + dt.timedelta(minutes=5))
    assert [(s["code"], s["kind"]) for s in r1["signals"]] == [("B", "주목")]
    r2 = w.tick(t0 + dt.timedelta(minutes=10))
    kinds = {(s["code"], s["kind"]) for s in r2["signals"]}
    assert kinds == {("B", "주목"), ("B", "매수")}
    buy = next(s for s in r2["signals"] if s["kind"] == "매수")
    assert buy["rank_prev"] == 2 and buy["rank_now"] == 1
    assert buy["qty"] == int(10_000_000 * 0.2 // 5400) and buy["cash_left"] == 10_000_000 - buy["qty"] * 5400
    r3 = w.tick(t0 + dt.timedelta(minutes=15))
    assert ("B", "재신호") not in {(s["code"], s["kind"]) for s in r3["signals"]}   # 1위 유지 → 순위 상승 없음
    p = watch.write_excel(cfg, con, today, uni, tmp_path / "w.xlsx")
    import openpyxl
    assert openpyxl.load_workbook(p).sheetnames == ["실시간", "신호기록", "5분대금흐름(억)", "감시대상", "기준"]
