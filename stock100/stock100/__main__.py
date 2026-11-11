"""사용법:
  python -m stock100 morning            # 새벽 4시: 뉴스 예습 → 시그널
  python -m stock100 evening            # 장 마감 후: 상한가/천만주 복습 → 추적 → 모의매매 → 리포트
  python -m stock100 backfill --days 100   # 과거 100 거래일 복습을 한 번에 (지식 DB 구축)
  python -m stock100 theme              # 거래대금 상위 + 고등락률 종목 테마 분석 → 엑셀
  python -m stock100 watch              # 장중 5분 감시 (09:00~15:30, 키움 필요)
  python -m stock100 kiwoom             # 키움 REST API 연결 점검
  python -m stock100 update             # GitHub 최신 코드로 업데이트 (config.yaml·data 유지)
  python -m stock100 status             # 현재 단계와 누적 성과
  python -m stock100 demo               # 네트워크 없이 가짜 데이터로 전체 흐름 시연
"""
from __future__ import annotations

import sys

if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "update":
    # 업데이트는 다른 모듈(판다스 등)을 불러오기 전에 처리 → 패키지가 바뀌어도 업데이트는 항상 동작
    from .update import main as _update_main
    sys.exit(_update_main(sys.argv[2:]))

import argparse
import datetime as dt

from . import challenge, excel, notes, preview, review, theme, track, watch
from .config import Config
from .db import connect
from .sources.dart import Dart, NoDart
from .sources.news import make_news
from .sources.prices import make_source
from .util import iso, to_date


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.con = connect(cfg.data_dir / "kb.sqlite")
        self.src = make_source(cfg)
        self.news = make_news(cfg)
        demo = cfg["sources"]["prices"] == "demo"
        self.dart = Dart(cfg.key["dart"], cfg.data_dir) if cfg.key.get("dart") and not demo else NoDart()

    def morning(self, date, quiet=False):
        st = challenge.current(self.cfg, date)
        if "preview" not in st.features:
            print(f"[{iso(date)}] {st.name}: 예습 단계 전입니다 — {st.desc}")
            return None
        heads, sigs = preview.run(self.cfg, self.con, self.src, self.news, date, quiet=quiet)
        path = notes.morning_report(self.cfg, st, iso(date), heads, sigs)
        if not quiet:
            print(f"  → {path}")
        return path

    def evening(self, date, quiet=False):
        st = challenge.current(self.cfg, date)
        if "review" not in st.features:
            print(f"[{iso(date)}] {st.name}: {st.desc}")
            return None
        rv = review.run(self.cfg, self.con, self.src, self.news, self.dart, date, quiet=quiet)
        tr = track.update_signals(self.cfg, self.con, self.src, date, quiet=quiet) if "track" in st.features else None
        pp = track.paper_trade(self.cfg, self.con, self.src, st, date, quiet=quiet) if "paper" in st.features else None
        for r in self.con.execute("SELECT DISTINCT code FROM signals WHERE date=?", (iso(date),)):
            notes.write_stock_note(self.cfg, self.con, r["code"])
        path = notes.evening_report(self.cfg, self.con, st, rv, tr, pp)
        if not quiet:
            print(f"  → {path}")
        if self.cfg.get("excel", True):
            self.theme_excel(rv, quiet=quiet)
        try:   # 5분 감시 ②그룹 재료: 오늘 거래대금 상위 100 저장 + 오늘 매수 신호의 종가 수익률 채우기
            watch.record_day(self.con, self.src, date, watch.gcfg(self.cfg, "history")["top_n"])
            watch.fill_close(self.con, self.src, date)
        except Exception as e:
            if not quiet:
                print(f"[info] 거래대금 상위 저장 생략 ({e.__class__.__name__})")
        return path

    def theme_excel(self, rv, quiet=False):
        res = theme.build(self.cfg, self.con, rv)
        path = excel.write(self.cfg, res, self.cfg.sub("excel") / f"{rv['date']}_테마분석.xlsx")
        if not quiet:
            print(f"[{rv['date']}] 테마 분석: 대상 {len(res['items'])}종목 → 테마 {len(res['themes'])}개")
            for t in res["themes"][:5]:
                print(f"   - {t['theme']} [{t['status']}] {t['n']}종목, 대장 {t['leader']['name']} "
                      f"{t['leader']['change_pct']:+.1f}%")
            print(f"  → {path}")
        return path

    def theme_only(self, date):
        rv = review.run(self.cfg, self.con, self.src, self.news, self.dart, date)
        return self.theme_excel(rv)

    def backfill(self, days, end):
        dates = self.src.trading_days(end, days)
        print(f"과거 {len(dates)} 거래일 복습: {dates[0]} ~ {dates[-1]}")
        for i, d in enumerate(dates, 1):
            try:
                review.run(self.cfg, self.con, self.src, self.news, self.dart, d,
                           with_profile=False, with_chart=False, quiet=True)
            except Exception as e:
                print(f"[warn] {d}: {e}")
            if i % 10 == 0 or i == len(dates):
                n = self.con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                print(f"  {i}/{len(dates)} 완료 · 누적 이벤트 {n}")
        print("종목별 공시/차트/노트 생성 중...")
        for r in self.con.execute("SELECT code, name FROM stocks").fetchall():
            review._profile(self.con, self.dart, r["code"])
            review._chart(self.cfg, self.con, self.src, r["code"], r["name"], end)
            notes.write_stock_note(self.cfg, self.con, r["code"])
        print(f"완료 → {self.cfg.data_dir}")

    def status(self):
        st = challenge.current(self.cfg)
        q = lambda s: self.con.execute(s).fetchone()[0]  # noqa: E731
        print(f"단계: {st.name} (Day {st.day}) — {st.desc}")
        print(f"자동 수행: {', '.join(sorted(st.features)) or '-'}")
        print(f"종목 {q('SELECT COUNT(*) FROM stocks')} · 이벤트 {q('SELECT COUNT(*) FROM events')} · "
              f"반복 종목 {q('SELECT COUNT(*) FROM (SELECT code FROM events GROUP BY code HAVING COUNT(*)>1)')}")
        n = q("SELECT COUNT(*) FROM signals WHERE status='closed'")
        h = q("SELECT COALESCE(SUM(hit),0) FROM signals WHERE status='closed'")
        print(f"시그널 {q('SELECT COUNT(*) FROM signals')} (종료 {n}, 적중 {h}, "
              f"적중률 {h / n * 100 if n else 0:.0f}%)")
        print(f"모의매매 {q('SELECT COUNT(*) FROM paper_trades')}회 · "
              f"누적손익 {q('SELECT COALESCE(SUM(pnl),0) FROM paper_trades'):,.0f}원")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="stock100", description="주식 공부 100일 챌린지 자동화")
    ap.add_argument("--config", help="설정 파일 (기본: config.yaml)")
    sp = ap.add_subparsers(dest="cmd", required=True)
    for c in ("morning", "evening"):
        p = sp.add_parser(c)
        p.add_argument("--date", help="YYYYMMDD (기본: 오늘)")
    p = sp.add_parser("backfill")
    p.add_argument("--days", type=int, default=100)
    p.add_argument("--end", help="YYYYMMDD (기본: 오늘)")
    p = sp.add_parser("theme", help="거래대금 상위 + 고등락률 종목 테마 분석 → 엑셀")
    p.add_argument("--date", help="YYYYMMDD (기본: 오늘)")
    p.add_argument("--value", type=float, help="거래대금 기준 (억 원)")
    p.add_argument("--pct", type=float, help="등락률 기준 (%%)")
    sp.add_parser("kiwoom", help="키움 REST API 연결 점검")
    p = sp.add_parser("watch", help="장중 5분 감시 (거래대금·등락률·순위 상승 → 매수 제안)")
    p.add_argument("--once", action="store_true", help="한 번만 조회하고 종료")
    p.add_argument("--list", action="store_true", help="오늘 감시 대상만 출력")
    p.add_argument("--demo", action="store_true", help="가짜 데이터로 하루치 감시 재생")
    p = sp.add_parser("update", help="GitHub 최신 코드로 업데이트 (config.yaml·data 유지)")
    p.add_argument("--check", action="store_true", help="새 버전이 있는지만 확인")
    p.add_argument("--no-pip", action="store_true")
    sp.add_parser("status")
    p = sp.add_parser("demo")
    p.add_argument("--days", type=int, default=60)
    a = ap.parse_args(argv)

    if a.cmd == "update":          # --config 를 앞에 붙여 실행한 경우
        from .update import main as update_main
        return update_main((["--check"] if a.check else []) + (["--no-pip"] if a.no_pip else []))

    if a.cmd == "watch" and a.demo:
        cfg = Config.load(a.config, {"sources": {"prices": "demo"}, "data_dir": "data_demo"})
        watch.run(App(cfg), demo=True)
        return 0

    if a.cmd == "demo":
        cfg = Config.load(a.config, {"sources": {"prices": "demo"}, "data_dir": "data_demo", "mode": "full",
                                     "filters": {"volume_min": 3_000_000, "value_min_eok": 5, "hot_pct_min": 5}})
        app = App(cfg)
        days = app.src.trading_days(dt.date.today(), a.days)
        print(f"[데모] {days[0]} ~ {days[-1]} {len(days)} 거래일을 매일 새벽/저녁 루틴으로 재생합니다.")
        for d in days:
            app.morning(d, quiet=True)
            app.evening(d, quiet=True)
        app.status()
        print(f"결과 폴더: {cfg.data_dir}")
        return 0

    over = {}
    if getattr(a, "value", None) is not None or getattr(a, "pct", None) is not None:
        over["filters"] = {k: v for k, v in (("value_min_eok", a.value), ("hot_pct_min", a.pct)) if v is not None}
    cfg = Config.load(a.config, over)
    app = App(cfg)
    try:
        _dispatch(app, a)
    except Exception as e:  # 네트워크/로그인 오류를 짧게 안내 (자세한 내용은 STOCK100_DEBUG=1)
        import os
        if os.getenv("STOCK100_DEBUG"):
            raise
        print(f"[오류] {e.__class__.__name__}: {str(e)[:300]}")
        print("  인터넷 연결, KRX_ID/KRX_PW, API 키를 확인하세요. 상세: STOCK100_DEBUG=1")
        return 1
    return 0


def _dispatch(app, a):
    if a.cmd == "morning":
        app.morning(to_date(a.date) if a.date else dt.date.today())
    elif a.cmd == "evening":
        app.evening(to_date(a.date) if a.date else dt.date.today())
    elif a.cmd == "backfill":
        app.backfill(a.days, to_date(a.end) if a.end else dt.date.today())
    elif a.cmd == "theme":
        app.theme_only(to_date(a.date) if a.date else dt.date.today())
    elif a.cmd == "watch":
        watch.run(app, once=a.once, list_only=a.list)
    elif a.cmd == "kiwoom":
        from .sources.kiwoom import check
        check(app.cfg)
    elif a.cmd == "status":
        app.status()


if __name__ == "__main__":
    sys.exit(main())
