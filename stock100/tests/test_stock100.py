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


def _watch_env(tmp_path, monkeypatch, over=None):
    from stock100.db import connect
    monkeypatch.chdir(tmp_path)
    cfg = Config.load(None, {"data_dir": str(tmp_path), **(over or {})})
    return cfg, connect(tmp_path / "kb.sqlite")


def test_watch_history_universe(tmp_path, monkeypatch):
    """② 최근 10거래일 거래대금 100위 이내 + 15% 이상 (+extra_codes), 판다스 경고 없음"""
    import warnings
    import pandas as pd
    from stock100 import watch
    cfg, con = _watch_env(tmp_path, monkeypatch, {"watch": {"extra_codes": ["4090"]}})
    days = [dt.date(2026, 10, 27) + dt.timedelta(days=i) for i in range(12)]

    class Src:
        def trading_days(self, end, n):
            return [d for d in days if d <= end][-n:]

        def value_top(self, date, n=100):
            rows = [{"code": "A00000", "name": "에이", "change_pct": 16.0, "rank": 3},     # 15%↑ → 포함
                    {"code": "B00000", "name": "비", "change_pct": 14.9, "rank": 5},       # 15% 미만 → 제외
                    {"code": "C00000", "name": "씨", "change_pct": 30.0, "rank": 101}]     # 100위 밖 → 제외
            if date == days[0]:                                                          # 11거래일 전 → 기간 밖
                rows.append({"code": "D00000", "name": "디", "change_pct": 25.0, "rank": 1})
            return pd.DataFrame([{**r, "market": "", "close": 1, "value": 1e9} for r in rows])

    today = days[-1]
    watch.ensure_history(cfg, con, Src(), today, quiet=True)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        uni = watch.history_universe(cfg, con, Src(), today)
    assert list(uni.code) == ["A00000", "004090"]


def test_watch_expected_pick_filters(tmp_path, monkeypatch):
    """① 예상체결: 우선주·스팩·1만 주 미만·1천 원 미만 제외 후 상위 100, 저장/재사용"""
    import pandas as pd
    from stock100 import watch
    cfg, con = _watch_env(tmp_path, monkeypatch)
    assert watch.is_pref_or_spac("005935", "삼성전자우") and watch.is_pref_or_spac("18064K", "한진칼우")
    assert watch.is_pref_or_spac("123450", "하나스팩10호") and not watch.is_pref_or_spac("005930", "삼성전자")

    class Feed:
        def expected_top(self, n):
            rows = [{"code": f"{i:05d}0", "name": f"종목{i}", "price": 5000, "pct": 30 - i * 0.1, "qty": 20000}
                    for i in range(1, 130)]
            rows[0].update(code="000015", name="뭐뭐우")       # 우선주
            rows[1].update(name="엔에이치스팩")                  # 스팩
            rows[2].update(qty=9999)                            # 거래량 미달
            rows[3].update(price=990)                           # 동전주
            df = pd.DataFrame(rows)
            df["rank"] = range(1, len(df) + 1)
            return df

    uni = watch.pick_expected(cfg, con, Feed(), dt.date(2026, 11, 10), "확정", quiet=True)
    assert len(uni) == 100 and uni.iloc[0].code == "000050" and list(uni["rank"])[:3] == [1, 2, 3]
    assert len(watch.load_universe(con, dt.date(2026, 11, 10), "expected")) == 100


def test_watch_signals_and_outcomes(tmp_path, monkeypatch):
    """주목(5억+1.5%p) / 순위 상승 매수(그룹 보유금 20%) / 30분·1시간·종가 성과 / 그룹 비교"""
    import pandas as pd
    from stock100 import watch
    cfg, con = _watch_env(tmp_path, monkeypatch)
    uni = pd.DataFrame({"code": ["A", "B"], "name": ["에이", "비"], "rank": [None, None], "reason": ["", ""]})
    path = [{"A": (10000, 5.0, 100e8), "B": (5000, 3.0, 90e8)},
            {"A": (10100, 6.0, 102e8), "B": (5200, 5.0, 96e8)},    # B 주목만 (순위 그대로)
            {"A": (10150, 6.5, 103e8), "B": (5400, 7.0, 104e8)}]   # B 2위→1위 매수
    path += [{"A": (10150, 6.5, 103e8 + i * 1e7), "B": (5400 + 54 * (i + 1), 7.0, 105e8 + i * 1e7)}
             for i in range(15)]                                   # 이후 B 1%씩 상승

    class Feed:
        def quotes(self, codes):
            t = path.pop(0)
            return pd.DataFrame([{"code": c, "name": "", "price": p, "pct": r, "value": v, "strength": 100}
                                 for c, (p, r, v) in t.items() if c in codes])

        def market_rank(self, n=300):
            return {}

    tf = watch.TickFeed(Feed())
    w = watch.Watcher(cfg, con, tf, uni, dt.date(2026, 11, 10), "history")
    t0 = dt.datetime(2026, 11, 10, 9, 0)
    res = []
    for k in range(18):
        tf.begin(["A", "B"])
        res.append(w.tick(t0 + dt.timedelta(minutes=5 * k), final=(k == 17)))
    assert not res[0]["signals"]
    assert [(s["code"], s["kind"]) for s in res[1]["signals"]] == [("B", "주목")]
    buy = next(s for s in res[2]["signals"] if s["kind"] == "매수")
    assert (buy["rank_prev"], buy["rank_now"]) == (2, 1) and buy["qty"] == int(2_000_000 // 5400)
    o = dict(con.execute("SELECT * FROM watch_outcomes").fetchone())
    assert o["time"] == "09:10" and o["entry"] == 5400
    assert o["r30"] == 6.0 and o["r60"] == 12.0 and o["rclose"] == 15.0 and o["rmax"] == 15.0
    out, sig = watch.compare_rows(con)
    assert out["history"]["n"] == 1 and out["history"]["win"] == 1 and sig["history"] == 2   # 09:05, 09:10 주목
    p = watch.write_excel(cfg, con, dt.date(2026, 11, 10), {"history": uni}, tmp_path / "w.xlsx")
    import openpyxl
    assert openpyxl.load_workbook(p).sheetnames[:2] == ["성과비교", "실시간_②거래대금이력"]


def test_watch_premarket_timing(tmp_path, monkeypatch):
    """08:50 시작 → 08:55 1차 선정 → 08:59 확정 → 09:00~15:30 5분 간격 → 종료"""
    from stock100 import watch
    from stock100.__main__ import App
    monkeypatch.chdir(tmp_path)
    cfg = Config.load(None, {"data_dir": str(tmp_path), "sources": {"prices": "demo"}})
    app = App(cfg)
    feed = watch.DemoFeed()
    monkeypatch.setattr(watch, "_feed", lambda a, demo: feed)
    picks = []
    real = watch.pick_expected
    monkeypatch.setattr(watch, "pick_expected",
                        lambda c, con, f, d, tag, quiet=False: (picks.append((now[0].strftime("%H:%M"), tag)),
                                                                real(c, con, f, d, tag, quiet=True))[1])
    monkeypatch.setattr(watch, "ensure_history", lambda *a, **k: watch._demo_history(cfg, app.con, app.src, a[3]))
    now = [dt.datetime.combine(dt.date.today(), dt.time(8, 50))]

    def sleep(sec):
        now[0] += dt.timedelta(seconds=sec + 2)

    watch.run(app, clock=lambda: now[0], sleep=sleep)
    assert picks == [("08:55", "1차"), ("08:59", "확정")]
    times = [r[0] for r in app.con.execute("SELECT DISTINCT time FROM watch_ticks2 ORDER BY time")]
    assert times[0] == "09:00" and times[-1] == "15:30" and len(times) == 79
    grps = {r[0] for r in app.con.execute("SELECT DISTINCT grp FROM watch_ticks2")}
    assert grps == {"expected", "history"}


def test_watch_midday_start_aligns_to_grid(tmp_path, monkeypatch):
    """12:09 에 켜도 첫 비교가 1분짜리가 되지 않게 12:10 에 기준값, 12:15 부터 5분 비교"""
    from stock100 import watch
    from stock100.__main__ import App
    monkeypatch.chdir(tmp_path)
    cfg = Config.load(None, {"data_dir": str(tmp_path), "sources": {"prices": "demo"},
                             "watch": {"groups": {"expected": {"enabled": False}}}})
    app = App(cfg)
    monkeypatch.setattr(watch, "_feed", lambda a, demo: watch.DemoFeed())
    monkeypatch.setattr(watch, "ensure_history", lambda *a, **k: watch._demo_history(cfg, app.con, app.src, a[3]))
    now = [dt.datetime.combine(dt.date.today(), dt.time(12, 9, 10))]

    def sleep(sec):
        now[0] += dt.timedelta(seconds=sec + 2)

    watch.run(app, clock=lambda: now[0], sleep=sleep)
    times = [r[0] for r in app.con.execute("SELECT DISTINCT time FROM watch_ticks2 ORDER BY time")]
    assert times[:3] == ["12:10", "12:15", "12:20"] and times[-1] == "15:30"


def test_update_keeps_config_and_data(tmp_path):
    """update: 바뀐 코드만 반영 + 백업, config.yaml/data 보존, CRLF 그대로, 같은 버전이면 아무것도 안 함"""
    import io
    import zipfile
    from stock100 import update

    root = tmp_path / "stock100"
    (root / "stock100").mkdir(parents=True)
    (root / "stock100" / "__init__.py").write_text("old")
    (root / "stock100" / "same.py").write_text("same")
    (root / "config.yaml").write_text("keys: secret")
    (root / "data").mkdir()
    (root / "data" / "kb.sqlite").write_bytes(b"db")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("dw-main/", "")
        z.writestr("dw-main/index.html", "other project")                 # 저장소의 다른 파일 → 무시
        z.writestr("dw-main/stock100/stock100/__init__.py", "new")
        z.writestr("dw-main/stock100/stock100/same.py", "same")
        z.writestr("dw-main/stock100/scripts/run_watch.bat", b"@echo off\r\nrem x\r\n")
        z.writestr("dw-main/stock100/config.yaml", "keys: SHOULD NOT OVERWRITE")  # 보호
        z.writestr("dw-main/stock100/data/kb.sqlite", b"SHOULD NOT")             # 보호
    zb = buf.getvalue()

    rc = update.run(root=root, pip=False, _download=lambda r, b: zb, _sha=lambda r, b: "a" * 40)
    assert rc == 0
    assert (root / "stock100" / "__init__.py").read_text() == "new"
    assert (root / "scripts" / "run_watch.bat").read_bytes() == b"@echo off\r\nrem x\r\n"
    assert (root / "config.yaml").read_text() == "keys: secret"
    assert (root / "data" / "kb.sqlite").read_bytes() == b"db"
    assert not (root / "index.html").exists()
    backups = list((root / "backup").glob("*/stock100/__init__.py"))
    assert len(backups) == 1 and backups[0].read_text() == "old"
    assert (root / ".version").read_text() == "a" * 40
    assert update.plan(zb, root) == []                                    # 두 번째 실행: 바뀐 것 없음
