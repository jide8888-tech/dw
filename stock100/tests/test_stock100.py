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
        if api == "ka90001":   # 테마 전체 목록 — 2페이지
            assert json["qry_tp"] == "0"
            if headers["cont-yn"] == "N":
                return R({"thema_grp": [{"thema_grp_cd": "100", "thema_nm": "PCB(인쇄회로기판)", "stk_num": "2"}],
                          "return_code": 0}, {"cont-yn": "Y", "next-key": "t1"})
            return R({"thema_grp": [{"thema_grp_cd": "200", "thema_nm": "보안_인터넷", "stk_num": "1"}],
                      "return_code": 0})
        if api == "ka90002":
            mem = {"100": [("A356860", "티엘비"), ("222800", "심텍")], "200": [("042510", "라온시큐어")]}
            return R({"flu_rt": "+1.0", "thema_comp_stk": [{"stk_cd": c, "stk_nm": n} for c, n in
                                                           mem[json["thema_grp_cd"]]], "return_code": 0})
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
    assert src.theme_map() == {"PCB(인쇄회로기판)": [("356860", "티엘비"), ("222800", "심텍")],
                               "보안_인터넷": [("042510", "라온시큐어")]}


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
            rows[2].update(qty=999)                             # 거래량 미달 (1천 주 미만)
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
    t0 = dt.datetime(2026, 11, 10, 9, 5)      # 09:00 값은 기준으로 안 쓰므로 09:05 부터
    res = []
    for k in range(18):
        tf.begin(["A", "B"])
        res.append(w.tick(t0 + dt.timedelta(minutes=5 * k), final=(k == 17)))
    assert not res[0]["signals"]
    assert [(s["code"], s["kind"]) for s in res[1]["signals"]] == [("B", "주목")]
    buy = next(s for s in res[2]["signals"] if s["kind"] == "매수")
    assert (buy["rank_prev"], buy["rank_now"]) == (2, 1) and buy["qty"] == int(2_000_000 // 5400)
    o = dict(con.execute("SELECT * FROM watch_outcomes").fetchone())
    assert o["time"] == "09:15" and o["entry"] == 5400
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
    cfg = Config.load(None, {"data_dir": str(tmp_path), "sources": {"prices": "demo"},
                             "watch": {"groups": {"expected": {"enabled": True}}}})   # 예상체결 방식도 켜서 시험
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
    assert grps == {"live", "expected", "history"}


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


def test_watch_day2_fixes(tmp_path, monkeypatch):
    """11-12 실사용에서 찾은 문제: 장전 기준값(대금 0) 비교 제외, 같은 금액 매수, ETF 제외, 실제 종가, 익절·손절 규칙"""
    import pandas as pd
    from stock100 import watch
    cfg, con = _watch_env(tmp_path, monkeypatch)
    assert watch.is_etf("KODEX 반도체레버리지") and watch.is_etf("TIGER 2차전지") and not watch.is_etf("한진칼우")

    uni = pd.DataFrame({"code": ["A", "B", "C"], "name": ["에이", "비", "씨"], "rank": None, "reason": ""})
    path = [{"A": (10000, 0.0, 0.0), "B": (5000, 0.0, 0.0), "C": (3000, 0.0, 0.0)},          # 09:00 장 시작 전
            {"A": (11000, 10.0, 50e8), "B": (5200, 4.0, 40e8), "C": (3000, 0.0, 1e8)},       # 09:05 갭 → 신호 없어야
            {"A": (11100, 11.0, 52e8), "B": (5400, 8.0, 53e8), "C": (3000, 0.0, 1e8)},       # 09:10 B: +13억 +4%p, 2위→1위
            {"A": (11100, 11.0, 53e8), "B": (5600, 12.0, 54e8), "C": (3000, 0.0, 1e8)},      # 09:15 B +3.7%
            {"A": (11100, 11.0, 54e8), "B": (5100, 2.0, 55e8), "C": (3000, 0.0, 1e8)}]       # 09:20

    class Feed:
        def quotes(self, codes):
            t = path.pop(0)
            return pd.DataFrame([{"code": c, "name": "", "price": p, "pct": r, "value": v, "strength": 100}
                                 for c, (p, r, v) in t.items()])

        def market_rank(self, n=300):
            return {}

    tf = watch.TickFeed(Feed())
    w = watch.Watcher(cfg, con, tf, uni, dt.date(2026, 11, 12), "history")
    t0 = dt.datetime(2026, 11, 12, 9, 0)
    res = []
    for k in range(5):
        tf.begin(["A", "B", "C"])
        res.append(w.tick(t0 + dt.timedelta(minutes=5 * k)))
    assert res[1]["signals"] == []                         # 09:05: 시초가 갭은 5분 변화가 아님
    buy = next(s for s in res[2]["signals"] if s["kind"] == "매수")
    assert buy["code"] == "B" and buy["qty"] == int(10_000_000 * 0.2 // 5400)    # 시작 보유금 기준 같은 금액
    sim = watch.simulate_exit(con, cfg, dt.date(2026, 11, 12))
    ret, why, at = sim[("2026-11-12", "history", "09:10", "B")]
    assert (ret, why, at) == (3.7, "익절", "09:15")

    class Src:
        def ohlcv(self, code, s, e):
            return pd.DataFrame({"close": [5940.0]}, index=pd.DatetimeIndex(["2026-11-12"]))

    con.execute("UPDATE watch_outcomes SET rclose=-1.0")
    assert watch.fill_close(con, Src(), dt.date(2026, 11, 12)) == 1
    assert con.execute("SELECT rclose FROM watch_outcomes").fetchone()[0] == 10.0   # 5400→5940 실제 종가로 덮어씀


def test_watch_live_top100(tmp_path, monkeypatch):
    """① 거래대금상위: 5분마다 시장 거래대금 순위 조회, 101~200위에서 올라온 종목도 비교, 신호는 100위 안만,
    ETF 제외, 산 종목은 순위 밖으로 밀려도 시세를 따로 받아 성과 기록"""
    import pandas as pd
    from stock100 import watch
    cfg, con = _watch_env(tmp_path, monkeypatch, {"watch": {"groups": {"live": {"top_n": 3, "fetch_n": 6,
                                                                                   "exclude_etf": True}}}})

    def board(rows):      # (code, name, price, pct, value억) — 리스트 순서 = 거래대금 순위
        return pd.DataFrame([{"code": c, "name": n, "price": p, "pct": r, "value": v * 1e8, "rank": i + 1}
                             for i, (c, n, p, r, v) in enumerate(rows)])

    boards = [
        board([("E", "KODEX 레버리지", 1, 0, 900), ("A", "에이", 1000, 1.0, 500), ("B", "비", 2000, 2.0, 300),
               ("C", "씨", 3000, 1.0, 200), ("D", "디", 4000, 0.5, 100)]),
        # D: 5위 → 3위 (+150억, +3.0%p) → 지금 3위 안 + 순위 상승 → 매수 / C: 4위(밖) → 신호 없음
        board([("E", "KODEX 레버리지", 1, 0, 950), ("A", "에이", 1010, 2.0, 520), ("D", "디", 4120, 3.5, 250),
               ("B", "비", 2000, 2.0, 310), ("C", "씨", 3100, 4.0, 240)]),
        # D 가 순위에서 사라짐 → 따로 받은 시세로 성과 기록
        board([("E", "KODEX 레버리지", 1, 0, 990), ("A", "에이", 1020, 2.5, 540), ("B", "비", 2000, 2.0, 320)]),
    ]

    class Feed:
        def live_top(self, n):
            return boards.pop(0)

        def quotes(self, codes):
            return pd.DataFrame([{"code": c, "name": "디", "price": 4300, "pct": 8.0, "value": 300e8, "strength": 100}
                                 for c in codes if c == "D"], columns=["code", "name", "price", "pct", "value", "strength"])

        def market_rank(self, n=300):
            return {}

    tf = watch.TickFeed(Feed(), live_n=6)
    uni = pd.DataFrame(columns=watch.UNI_COLS)
    w = watch.Watcher(cfg, con, tf, uni, dt.date(2026, 11, 17), "live")
    t0 = dt.datetime(2026, 11, 17, 9, 30)
    tf.begin([])
    r0 = w.tick(t0)
    assert "E" not in set(r0["rows"]["code"])                                   # ETF 제외
    assert list(w.uni["code"]) == ["A", "B"]                                     # 시장 순위 3위 안 (ETF 1위)
    tf.begin(w.bought_codes())
    r1 = w.tick(t0 + dt.timedelta(minutes=5))
    kinds = {(s["code"], s["kind"]) for s in r1["signals"]}
    assert ("D", "매수") in kinds and not any(c == "C" for c, _ in kinds)
    buy = next(s for s in r1["signals"] if s["kind"] == "매수")
    assert (buy["rank_prev"], buy["rank_now"]) == (5, 3)
    tf.begin(w.bought_codes())
    w.tick(t0 + dt.timedelta(minutes=10))
    row = con.execute("SELECT price FROM watch_ticks2 WHERE grp='live' AND code='D' AND time='09:40'").fetchone()
    assert row and row[0] == 4300                                                # 밀려나도 시세 기록
    assert con.execute("SELECT COUNT(*) FROM watch_universe WHERE grp='live'").fetchone()[0] == 2


def test_watch_live_rule_rank_up_and_5min_return(tmp_path, monkeypatch):
    """① 기본 규칙: ETF 포함 100개 전부, 매수 = 5분 동안 거래대금 순위 상승 + 5분 전 가격 대비 +1.5% 이상
    (5억 조건 없음, 전일대비 등락률 변화가 아니라 5분 상승률)"""
    import pandas as pd
    from stock100 import watch
    cfg, con = _watch_env(tmp_path, monkeypatch, {"watch": {"groups": {"live": {"top_n": 4, "fetch_n": 6}}}})

    def board(rows):
        return pd.DataFrame([{"code": c, "name": n, "price": p, "pct": r, "value": v * 1e8, "rank": i + 1}
                             for i, (c, n, p, r, v) in enumerate(rows)])

    boards = [
        board([("E", "KODEX 레버리지", 10000, 1.0, 900), ("A", "에이", 10000, 20.0, 500),
               ("B", "비", 10000, 1.0, 300), ("C", "씨", 10000, 1.0, 200), ("D", "디", 10000, 1.0, 100)]),
        board([("E", "KODEX 레버리지", 10200, 3.0, 905),   # ETF: 순위 그대로 → 주목만
               ("B", "비", 10160, 2.6, 302),                 # +1.6%, 3위→2위, 대금 +2억(5억 안 돼도) → 매수
               ("A", "에이", 10140, 21.7, 501),             # 등락률 +1.7%p 지만 5분 상승률 +1.4% → 신호 없음
               ("D", "디", 10100, 2.0, 280),                 # 5위→4위, +1.0% → 상승률 미달
               ("C", "씨", 10300, 4.0, 210)]),               # +3%, 4위→5위(100위 밖) → 신호 없음
    ]

    class Feed:
        def live_top(self, n):
            return boards.pop(0)

        def quotes(self, codes):
            return pd.DataFrame(columns=["code", "name", "price", "pct", "value", "strength"])

        def market_rank(self, n=300):
            return {}

    tf = watch.TickFeed(Feed(), live_n=6)
    w = watch.Watcher(cfg, con, tf, pd.DataFrame(columns=watch.UNI_COLS), dt.date(2026, 11, 17), "live")
    t0 = dt.datetime(2026, 11, 17, 10, 0)
    tf.begin([])
    r0 = w.tick(t0)
    assert "E" in set(r0["rows"]["code"]) and r0["n_watch"] == 4                   # ETF 포함
    tf.begin([])
    r1 = w.tick(t0 + dt.timedelta(minutes=5))
    kinds = {(s["code"], s["kind"]) for s in r1["signals"]}
    assert kinds == {("B", "주목"), ("B", "매수")}        # ETF(E)는 +2% 올라도 신호 제외 (순위에는 포함)
    b = next(s for s in r1["signals"] if s["kind"] == "매수")
    assert b["d_ret"] == 1.6 and (b["rank_prev"], b["rank_now"]) == (3, 2)
    assert con.execute("SELECT d_ret FROM watch_ticks2 WHERE code='A' AND time='10:05'").fetchone()[0] == 1.4


def test_watch_tags_and_etf_signal_exclusion(tmp_path, monkeypatch):
    """11-18 분석 반영: ETF·ETN 은 순위엔 포함·신호 제외, 테마동반(같은 테마 2종목 이상 15분 내 신호), 급락반등(-10% 이하)"""
    import pandas as pd
    from stock100 import watch
    cfg, con = _watch_env(tmp_path, monkeypatch, {"watch": {"groups": {"live": {"top_n": 6, "fetch_n": 8}}}})

    def board(rows):
        return pd.DataFrame([{"code": c, "name": n, "price": p, "pct": r, "value": v * 1e8, "rank": i + 1}
                             for i, (c, n, p, r, v) in enumerate(rows)])

    base = [("E", "KODEX 200선물인버스2X", 10000, -6.0, 900), ("L", "에이엘로봇", 10000, -17.5, 800),
            ("R", "레인보우로보틱스", 10000, 11.0, 300), ("T", "로보티즈", 10000, 7.0, 290),
            ("S", "삼현", 10000, 4.0, 280), ("X", "기타", 10000, 1.0, 270)]
    nxt = [("E", "KODEX 200선물인버스2X", 10200, -4.0, 1000), ("L", "에이엘로봇", 10300, -15.0, 950),
           ("T", "로보티즈", 10200, 9.0, 340), ("R", "레인보우로보틱스", 10200, 13.0, 330),
           ("S", "삼현", 10200, 6.0, 300), ("X", "기타", 10000, 1.0, 271)]
    boards = [board(base), board(nxt)]

    class Feed:
        def live_top(self, n):
            return boards.pop(0)

        def quotes(self, codes):
            return pd.DataFrame(columns=["code", "name", "price", "pct", "value", "strength"])

        def market_rank(self, n=300):
            return {}

        def stock_themes(self, code):
            return {"R": ["로봇", "휴머노이드"], "T": ["로봇"], "S": ["로봇", "원전"], "L": ["로봇"],
                    "X": ["건설"]}.get(code, [])

    tf = watch.TickFeed(Feed(), live_n=8, con=con, cfg=cfg)
    w = watch.Watcher(cfg, con, tf, pd.DataFrame(columns=watch.UNI_COLS), dt.date(2026, 11, 18), "live")
    t0 = dt.datetime(2026, 11, 18, 9, 15)
    tf.begin([])
    w.tick(t0)
    tf.begin([])
    r = w.tick(t0 + dt.timedelta(minutes=5))
    by = {}
    for s in r["signals"]:
        by.setdefault(s["code"], s)
    assert "E" not in by                                            # ETF 는 신호 제외
    assert int(r["rows"].set_index("code").at["E", "mk_rank"]) == 1   # 순위에는 포함
    assert "급락반등" in by["L"]["tags"] and "테마동반(로봇" in by["L"]["tags"]
    assert "테마동반(로봇" in by["R"]["tags"] and "급락반등" not in by["R"]["tags"]
    assert "X" not in by
    o = dict(con.execute("SELECT tags FROM watch_outcomes WHERE code='T'").fetchone())
    assert "테마동반" in o["tags"]
    assert con.execute("SELECT COUNT(*) FROM stock_themes").fetchone()[0] >= 4    # 테마 캐시
    p = watch.write_excel(cfg, con, dt.date(2026, 11, 18), {"live": w.uni}, tmp_path / "w.xlsx")
    import openpyxl
    rows = list(openpyxl.load_workbook(p)["태그별성과"].iter_rows(min_row=2, max_row=7, values_only=True))
    rows = list(openpyxl.load_workbook(p)["태그별성과"].iter_rows(min_row=2, max_row=13, values_only=True))
    assert [x[2] for x in rows] == ["테마동반", "급락반등", "상대강도", "약한상승", "주도주눌림", "하락중반등",
                                    "전일급등", "전일급등 아님", "시장강세", "시장보통", "시장약세", "태그 없음"]
    assert rows[0][3] == 1   # 매수는 로보티즈 1건


def test_evaluate_path_and_exit_methods(tmp_path, monkeypatch):
    """신호 후 최고·최저·먼저 온 것·구간별 최고/최저, 매도 방법(고정/고점-3%/고점-5%/절반+추적/종가) — 1분봉 기준"""
    import pandas as pd
    from stock100 import evaluate, watch
    cfg, con = _watch_env(tmp_path, monkeypatch)
    con.execute("INSERT INTO watch_outcomes (date, grp, time, code, name, entry, qty, rmax, tags) "
                "VALUES ('2026-11-19','live','09:15','A','에이',10000,10,0,'테마동반(로봇: 비)')")
    con.execute("INSERT INTO watch_outcomes (date, grp, time, code, name, entry, qty, rmax, tags) "
                "VALUES ('2026-11-19','live','09:30','B','비',5000,10,0,'')")
    for t, h, l, c in (("09:15", 10000, 9000, 9900),                  # 신호가 난 분 → 제외
                       ("09:16", 10100, 9950, 10050), ("09:17", 10400, 10050, 10350),
                       ("09:18", 10900, 10350, 10800), ("09:19", 11000, 10600, 10650),
                       ("09:20", 10700, 10300, 10350), ("09:21", 10400, 9500, 9600)):
        con.execute("INSERT INTO minute_bars VALUES ('2026-11-19','A',?,?,?,?,?,0)", (t, c, h, l, c))
    for t, p in (("09:35", 4900), ("09:40", 4800)):                 # B 는 1분봉 없음 → 5분 시세
        con.execute("INSERT INTO watch_ticks2 (date, time, grp, code, name, price) VALUES ('2026-11-19',?,'live','B','비',?)",
                    (t, p))
    con.commit()
    rs = {r["code"]: r for r in evaluate.evaluate(con, cfg, dt.date(2026, 11, 19))}
    a = rs["A"]
    assert (a["source"], a["up"], a["up_time"], a["up_min"]) == ("1분봉", 10.0, "09:19", 4)
    assert (a["down"], a["down_time"], a["first"]) == (-5.0, "09:21", "상승 먼저")
    assert a["up10"] == 10.0 and a["dn10"] == -5.0
    st = a["strat"]
    assert st["fixed"][:2] == (3.0, "익절")
    assert st["trail3"][:2] == (6.7, "추적 매도")          # 고점 11,000 의 -3% = 10,670
    assert st["trail5"][:2] == (4.5, "추적 매도")          # 고점 11,000 의 -5% = 10,450
    assert st["half"][0] == 3.75                           # (+3% + 4.5%) / 2
    assert rs["B"]["source"] == "5분 시세" and rs["B"]["down"] == -4.0 and rs["B"]["first"] == "하락 먼저"
    sm = evaluate.summarize(list(rs.values()), cfg)
    assert sm["n"] == 2 and sm["first_up"] == 50.0 and sm["best"]

    class Src:
        def minute_bars(self, code, date):
            return pd.DataFrame([{"time": "09:31", "open": 5000, "high": 5300, "low": 4990, "close": 5250, "volume": 1}])

    assert watch.fetch_minute_bars(con, Src(), dt.date(2026, 11, 19), quiet=True) == 2
    rb = {r["code"]: r for r in evaluate.evaluate(con, cfg, dt.date(2026, 11, 19))}["B"]
    assert rb["source"] == "1분봉" and rb["up"] == 6.0 and rb["strat"]["fixed"][:2] == (3.0, "익절")
    con.execute("INSERT INTO watch_ticks2 (date, time, grp, code, name, price) VALUES ('2026-11-19','09:30','live','B','비',5000)")
    p = watch.report(cfg, con, Src(), dt.date(2026, 11, 19), quiet=True)
    import openpyxl
    wb = openpyxl.load_workbook(p)
    hdr = [c.value for c in wb["신호성과"][1]]
    assert "최고 상승(%)" in hdr and "최대 하락(%)" in hdr and "먼저 온 것" in hdr and "고점-5%(%)" in hdr
    assert [c.value for c in wb["태그별성과"][1]][:4] == ["그룹", "기간", "구분", "매수 신호"]


def test_watch_skips_session_start_baseline_and_affiliates(tmp_path, monkeypatch):
    """11-20 반영: ① 09:00 값(대금이 조금 있어도)은 기준으로 안 씀 → 09:05 시초가 갭 신호 없음, 09:10 부터 비교
    ② 테마동반은 계열사(앞 3글자 같음)를 1개로 세고 기본 3종목 이상"""
    import pandas as pd
    from stock100 import watch
    assert watch.is_affiliate("한국화장품", "한국화장품제조") and watch.is_affiliate("에코프로", "에코프로비엠")
    assert not watch.is_affiliate("한국전력", "한국가스공사") and not watch.is_affiliate("삼성전자", "삼성SDI")
    cfg, con = _watch_env(tmp_path, monkeypatch, {"watch": {"groups": {"live": {"top_n": 6, "fetch_n": 8}}}})

    def board(rows):
        return pd.DataFrame([{"code": c, "name": n, "price": p, "pct": r, "value": v * 1e8, "rank": i + 1}
                             for i, (c, n, p, r, v) in enumerate(rows)])

    boards = [
        # 09:00: 전일 종가 그대로(등락 0) 인데 대금이 조금 잡혀 있음
        board([("H", "한국화장품", 6460, 0.0, 3), ("M", "한국화장품제조", 12700, 0.0, 2),
               ("A", "에이", 10000, 0.0, 1), ("B", "비", 10000, 0.0, 1)]),
        # 09:05: 갭상승 — 비교하면 +9.9% 지만 신호가 나면 안 됨
        board([("H", "한국화장품", 7100, 9.91, 96), ("M", "한국화장품제조", 13000, 2.36, 25),
               ("A", "에이", 10000, 0.0, 20), ("B", "비", 10000, 0.0, 10)]),
        # 09:10: H·M 둘 다 +2% 이상, 순위 상승 → 매수. 같은 '화장품' 테마지만 계열사라 테마동반 아님
        board([("M", "한국화장품제조", 13320, 4.88, 80), ("H", "한국화장품", 7250, 12.2, 161),
               ("A", "에이", 10000, 0.0, 21), ("B", "비", 10000, 0.0, 11)]),
    ]

    class Feed:
        def live_top(self, n):
            return boards.pop(0)

        def quotes(self, codes):
            return pd.DataFrame(columns=["code", "name", "price", "pct", "value", "strength"])

        def market_rank(self, n=300):
            return {}

        def stock_themes(self, code):
            return ["화장품"] if code in ("H", "M") else []

    tf = watch.TickFeed(Feed(), live_n=8, con=con, cfg=cfg)
    w = watch.Watcher(cfg, con, tf, pd.DataFrame(columns=watch.UNI_COLS), dt.date(2026, 11, 20), "live")
    t0 = dt.datetime(2026, 11, 20, 9, 0)
    res = []
    for k in range(3):
        tf.begin([])
        res.append(w.tick(t0 + dt.timedelta(minutes=5 * k)))
    assert res[1]["signals"] == []                                          # 09:05 갭 신호 없음
    d05 = con.execute("SELECT d_ret FROM watch_ticks2 WHERE code='H' AND time='09:05'").fetchone()[0]
    assert d05 is None                                                      # 09:00 과 비교하지 않음
    kinds = {(s["code"], s["kind"]) for s in res[2]["signals"]}
    assert ("M", "매수") in kinds
    assert all("테마동반" not in (s["tags"] or "") for s in res[2]["signals"])   # 계열사끼리는 테마동반 아님


def test_watch_relative_strength_new_listing_and_confirm_exit(tmp_path, monkeypatch):
    """11-20 분석 반영: 상대강도 표시(신호 때 +4~15%), 상장 첫날 종목 신호 제외, '10분 -1% 확인 후 고점-3%' 매도 방법"""
    import pandas as pd
    from stock100 import evaluate, watch
    cfg, con = _watch_env(tmp_path, monkeypatch, {"watch": {"groups": {"live": {"top_n": 6, "fetch_n": 8}}}})

    def board(rows):
        return pd.DataFrame([{"code": c, "name": n, "price": p, "pct": r, "value": v * 1e8, "rank": i + 1}
                             for i, (c, n, p, r, v) in enumerate(rows)])

    boards = [board([("F1", "가", 10000, 0.0, 950), ("N", "하나에어로다이내믹스", 9500, 66.7, 900),
                     ("F2", "나", 10000, 0.0, 800), ("P", "새상장", 10000, 10.0, 500),
                     ("F3", "다", 10000, 0.0, 400), ("R", "한진칼", 10000, 6.0, 300)]),
              # N 2→1위 +5.6%, P 4→3위 +3%, R 6→5위 +2% — 모두 순위 상승 + 5분 +1.5% 이상
              board([("N", "하나에어로다이내믹스", 10030, 75.96, 1100), ("F1", "가", 10000, 0.0, 960),
                     ("P", "새상장", 10300, 13.3, 820), ("F2", "나", 10000, 0.0, 810),
                     ("R", "한진칼", 10200, 8.1, 600), ("F3", "다", 10000, 0.0, 410)])]

    class Feed:
        def live_top(self, n):
            return boards.pop(0)

        def quotes(self, codes):
            return pd.DataFrame(columns=["code", "name", "price", "pct", "value", "strength"])

        def market_rank(self, n=300):
            return {}

        def new_listings(self, date):
            return {"P"}                       # 상장일이 오늘 (±30% 안이어도 제외)

    tf = watch.TickFeed(Feed(), live_n=8, con=con, cfg=cfg)
    w = watch.Watcher(cfg, con, tf, pd.DataFrame(columns=watch.UNI_COLS), dt.date(2026, 11, 20), "live")
    t0 = dt.datetime(2026, 11, 20, 10, 30)
    for k in range(2):
        tf.begin([])
        r = w.tick(t0 + dt.timedelta(minutes=5 * k))
    codes = {s["code"] for s in r["signals"]}
    assert "N" not in codes and "P" not in codes                 # 상장 첫날 제외 (±30% 초과 / 상장일 오늘)
    by = {s["code"]: s for s in r["signals"] if s["kind"] == "매수"}
    assert "상대강도" in by["R"]["tags"]                         # +8.1% → 상대강도

    x = evaluate.exit_cfg(cfg)
    drop = [("10:36", 10010, 9880, 9900), ("10:37", 10500, 9900, 10400)]      # 6분 뒤 -1.2% → -1% 손절
    assert evaluate.run_strategies(10000, drop, x, True, signal_time="10:35")["confirm"][:2] == (-1.0, "10분 확인 손절")
    hold = [("10:36", 10100, 9950, 10050), ("10:44", 10600, 10000, 10550),     # 10분 안엔 -0.5% 까지만
            ("10:50", 11000, 10500, 10900), ("10:55", 10900, 10600, 10650)]   # 고점 11,000 -3% = 10,670
    assert evaluate.run_strategies(10000, hold, x, True, signal_time="10:35")["confirm"][:2] == (6.7, "추적 매도")
    late = [("10:36", 10100, 10000, 10050), ("10:50", 10100, 9880, 9900)]     # 15분 뒤 -1.2% → 확인 구간 지남
    assert evaluate.run_strategies(10000, late, x, True, signal_time="10:35")["confirm"][1] != "10분 확인 손절"
    assert ("confirm", "10분 -1% 확인 후 고점-3%") in evaluate.strategies(cfg)


def test_watch_weak_rise_and_leader_pullback_tags(tmp_path, monkeypatch):
    """6일치 분석 반영: 약한상승(신호 때 0~4%) / 주도주눌림(최근 급등 이력 종목이 신호 때 전일 대비 하락 중)"""
    import pandas as pd
    from stock100 import watch
    cfg, con = _watch_env(tmp_path, monkeypatch, {"watch": {"groups": {"live": {"top_n": 6, "fetch_n": 8}}}})

    def board(rows):
        return pd.DataFrame([{"code": c, "name": n, "price": p, "pct": r, "value": v * 1e8, "rank": i + 1}
                             for i, (c, n, p, r, v) in enumerate(rows)])

    boards = [board([("F1", "가", 10000, 0.0, 900), ("W", "약한종목", 10000, 0.5, 800), ("F2", "나", 10000, 0.0, 700),
                     ("L", "한진칼우", 10000, -5.0, 600), ("F3", "다", 10000, 0.0, 500), ("D", "하락대형주", 10000, -4.0, 400)]),
              board([("W", "약한종목", 10200, 2.5, 950), ("F1", "가", 10000, 0.0, 910), ("L", "한진칼우", 10200, -3.1, 760),
                     ("F2", "나", 10000, 0.0, 710), ("D", "하락대형주", 10200, -2.1, 520), ("F3", "다", 10000, 0.0, 510)])]

    class Feed:
        def live_top(self, n):
            return boards.pop(0)

        def quotes(self, codes):
            return pd.DataFrame(columns=["code", "name", "price", "pct", "value", "strength"])

        def market_rank(self, n=300):
            return {}

    tf = watch.TickFeed(Feed(), live_n=8, con=con, cfg=cfg)
    tf.leaders = {"L"}                                  # 최근 급등 이력(②) 종목
    w = watch.Watcher(cfg, con, tf, pd.DataFrame(columns=watch.UNI_COLS), dt.date(2026, 11, 25), "live")
    t0 = dt.datetime(2026, 11, 25, 9, 30)
    for k in range(2):
        tf.begin([])
        r = w.tick(t0 + dt.timedelta(minutes=5 * k))
    by = {s["code"]: s for s in r["signals"] if s["kind"] == "매수"}
    assert "약한상승" in by["W"]["tags"]                  # +2.5%
    assert "주도주눌림" in by["L"]["tags"]                # 최근 급등 이력 + -3.1%
    assert "주도주눌림" not in (by["D"]["tags"] or "")     # 하락 중이지만 급등 이력 없음
    assert "약한상승" not in (by["L"]["tags"] or "")


def test_watch_prev_surge_breadth_and_falling_tags(tmp_path, monkeypatch):
    """11-26·27 분석 반영: 전일급등(전 거래일 100위 안 +15%↑) / 시장강세·약세(상위 100 중 상승 비율) /
    하락중반등(신호 때 하락 중) — 신호 때 붙고, 지난 기록에는 retag 로 다시 계산해 붙는다"""
    import openpyxl
    import pandas as pd
    from stock100 import watch
    cfg, con = _watch_env(tmp_path, monkeypatch, {"watch": {"groups": {"live": {"top_n": 10, "fetch_n": 12}},
                                                            "tags": {"market_min_stocks": 5}}})
    con.executemany("INSERT INTO value_rank_daily VALUES (?,?,?,?,?,?,?)",
                    [("2026-11-26", "A", "아리바이오랩", 5, 1e11, 23.3, 10000),    # 전일 +23% → 전일급등
                     ("2026-11-26", "B", "비", 8, 1e11, 9.0, 10000),              # +9% → 아님
                     ("2026-11-26", "C", "씨", 140, 1e10, 29.9, 10000)])          # 100위 밖 → 아님
    con.commit()

    def board(rows):
        return pd.DataFrame([{"code": c, "name": n, "price": p, "pct": r, "value": v * 1e8, "rank": i + 1}
                             for i, (c, n, p, r, v) in enumerate(rows)])

    fill = [(f"F{i}", f"보합{i}", 10000, 1.0, 500 - i * 10) for i in range(6)]       # 오르는 종목들
    b0 = fill + [("K", "KODEX 인버스", 10000, -1.0, 650), ("A", "아리바이오랩", 10000, 5.0, 300),
                 ("B", "비", 10000, 2.0, 200), ("D", "동양고속", 10000, -6.0, 100)]          # 8·9·10위
    b1 = [("A", "아리바이오랩", 10200, 7.1, 1000), ("B", "비", 10200, 4.0, 900),             # 1·2·3위로
          ("D", "동양고속", 10200, -4.1, 820), ("K", "KODEX 인버스", 10000, -1.0, 651)] + fill
    boards = [board(b0), board(b1)]

    class Feed:
        def live_top(self, n):
            return boards.pop(0)

        def quotes(self, codes):
            return pd.DataFrame(columns=["code", "name", "price", "pct", "value", "strength"])

        def market_rank(self, n=300):
            return {}

    tf = watch.TickFeed(Feed(), live_n=12, con=con, cfg=cfg)
    tf.prev_surge = watch.prev_surge_map(con, cfg, dt.date(2026, 11, 26))
    assert tf.prev_surge == {"A": 23.3}
    w = watch.Watcher(cfg, con, tf, pd.DataFrame(columns=watch.UNI_COLS), dt.date(2026, 11, 27), "live")
    t0 = dt.datetime(2026, 11, 27, 9, 30)
    for k in range(2):
        tf.begin([])
        r = w.tick(t0 + dt.timedelta(minutes=5 * k))
    assert tf.breadth() == 88.9                 # ETF 제외 9종목 중 8종목 상승
    by = {s["code"]: s for s in r["signals"] if s["kind"] == "매수"}
    assert "전일급등(+23%)" in by["A"]["tags"] and "시장강세(상승 89%)" in by["A"]["tags"]
    assert "전일급등" not in (by["B"]["tags"] or "")
    assert "하락중반등" in by["D"]["tags"] and "하락중반등" not in by["A"]["tags"]
    o = con.execute("SELECT tags, breadth FROM watch_outcomes WHERE code='A'").fetchone()
    assert o["breadth"] == 88.9 and "전일급등" in o["tags"]

    # 이 기능이 생기기 전 기록처럼 표시를 지우고 다시 계산 → 같은 결과, 두 번 돌려도 그대로
    before = {x["code"]: x["tags"] for x in con.execute("SELECT code, tags FROM watch_outcomes")}
    con.execute("UPDATE watch_outcomes SET tags='테마동반(로봇: 가·나)', breadth=NULL WHERE code='A'")
    con.execute("UPDATE watch_signals2 SET breadth=NULL, tags='' WHERE code='D'")
    con.commit()
    assert watch.retag(con, cfg) >= 2
    o = con.execute("SELECT tags, breadth FROM watch_outcomes WHERE code='A'").fetchone()
    assert o["tags"] == "테마동반(로봇: 가·나),전일급등(+23%),시장강세(상승 89%)" and o["breadth"] == 88.9
    assert all("하락중반등" in x["tags"] for x in con.execute("SELECT tags FROM watch_signals2 WHERE code='D'"))
    assert watch.retag(con, cfg) == 0
    assert con.execute("SELECT tags FROM watch_outcomes WHERE code='D'").fetchone()["tags"] == before["D"]

    p = watch.write_excel(cfg, con, dt.date(2026, 11, 27), {"live": w.uni}, tmp_path / "w.xlsx")
    wb = openpyxl.load_workbook(p)
    head = [c.value for c in wb["신호성과"][1]]
    assert head[7] == "시장 상승비율(%)"
    cats = {x[2]: x[3] for x in wb["태그별성과"].iter_rows(min_row=2, values_only=True) if x and x[1] == "오늘"}
    assert cats["전일급등"] == 1 and cats["전일급등 아님"] == 2 and cats["하락중반등"] == 1
    assert cats["시장강세"] == 3 and cats["시장보통"] == 0

    assert watch.market_breadth(board(b1[:3]), 10, 5) is None      # 종목이 너무 적으면 계산 안 함
    assert watch.extra_tags({}, -0.5, None, 35.0) == ["하락중반등", "시장약세(상승 35%)"]


def test_theme_map_fallback_and_retag_theme(tmp_path, monkeypatch):
    """11-27: 종목별 테마 조회가 빈 목록(티엘비·심텍·라온시큐어…) → 테마동반을 놓침.
    전체 테마 구성 종목으로 다시 계산 / 빈 캐시는 키워드로 대신 / 같은 동료 수면 더 구체적인 테마"""
    from stock100 import watch
    cfg, con = _watch_env(tmp_path, monkeypatch)
    # 전체 목록이 없을 때: 캐시된 빈 목록이면 종목명 키워드로 대신 (예전엔 7일 동안 빈 목록)
    con.execute("INSERT INTO stock_themes VALUES ('000001', '[]', ?)", (dt.date.today().isoformat(),))
    assert "로봇" in watch.ThemeBook(con, None, cfg).get("000001", "에이엘로봇")

    class Feed:
        def theme_map(self):
            return {"PCB(인쇄회로기판)": [("356860", "티엘비"), ("007810", "코리아써키트"), ("222800", "심텍")],
                    "스마트폰_삼성전자관련주": [("356860", "티엘비"), ("007810", "코리아써키트"), ("222800", "심텍"),
                                          ("000010", "가"), ("000020", "나")],
                    "보안_인터넷": [("042510", "라온시큐어"), ("053800", "안랩"), ("263860", "지니언스")]}

    assert watch.ensure_theme_map(con, Feed(), quiet=True) == 11
    assert watch.ensure_theme_map(con, Feed(), quiet=True) == 0          # 7일 안에는 다시 받지 않음
    book = watch.ThemeBook(con, None, cfg)
    assert book.get("356860") == ["PCB(인쇄회로기판)", "스마트폰_삼성전자관련주"] and book.size("PCB(인쇄회로기판)") == 3

    d = "2026-11-27"
    for t, code, name, kind, pct in (("09:10", "042510", "라온시큐어", "매수", 10.08), ("09:10", "053800", "안랩", "매수", 5.65),
                                     ("09:10", "263860", "지니언스", "매수", 4.58),
                                     ("09:25", "356860", "티엘비", "매수", -4.95), ("09:25", "007810", "코리아써키트", "주목", -1.62),
                                     ("09:25", "222800", "심텍", "주목", -2.63),
                                     ("09:45", "007810", "코리아써키트", "매수", 0.13)):
        con.execute("INSERT INTO watch_signals2 (date, time, grp, code, name, kind, price, pct, tags) "
                    "VALUES (?,?,?,?,?,?,?,?,?)", (d, t, "live", code, name, kind, 10000, pct, "상대강도" if pct > 4 else ""))
        if kind == "매수":
            con.execute("INSERT INTO watch_outcomes (date, grp, time, code, name, entry, qty, rmax, tags) "
                        "VALUES (?,?,?,?,?,10000,10,0,?)", (d, "live", t, code, name, "상대강도" if pct > 4 else ""))
    con.commit()
    watch.retag(con, cfg)
    tag = {r["code"] + r["time"]: r["tags"] for r in con.execute("SELECT code, time, tags FROM watch_outcomes")}
    assert tag["04251009:10"].startswith("테마동반(보안_인터넷: 안랩·지니언스),상대강도")
    assert tag["35686009:25"].startswith("테마동반(PCB(인쇄회로기판): 코리아써키트·심텍)")   # 더 구체적인 테마
    assert "하락중반등" in tag["35686009:25"]
    assert "테마동반" not in tag["00781009:45"]          # 20분 전(09:25) 티엘비·심텍은 15분 밖
    assert watch.retag(con, cfg) == 0                     # 다시 돌려도 그대로
