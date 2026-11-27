"""장중 5분 감시 — 기존 HTS 실시간 연동 엑셀(5분 매크로)을 키움 REST API 로 옮긴 것.

감시 종목을 고르는 두 방식을 같은 날 동시에 돌려 비교한다.
  ① 거래대금상위: 5분마다 그 시각의 전체 시장 거래대금 상위 100 을 조회 (ETF·ETN 제외).
                 101~200위도 함께 받아 두어, 100위 안으로 새로 들어온 종목도 5분 변화를 잴 수 있다.
                 순위는 전체 시장 거래대금 순위 (예: 34위 → 30위)
  ② 거래대금이력: 최근 10거래일 안에 거래대금 100위 이내 + 등락률 15% 이상이었던 종목 (+ extra_codes)
  (예전 ① 예상체결 방식은 groups.expected.enabled: true 로 다시 켤 수 있다)
5분마다 그룹마다:
  🔴 주목 : 5분 거래대금 증가 ≥ 5억 AND 5분 등락률 변화 ≥ +1.5%p
  ▲ 순위 : 그룹 안 거래대금 순위가 위로 (예: 34위 → 30위)
  매수   : buy_rule=both 이면 주목 + 순위 상승, rank_only 면 순위 상승만 → 그룹 보유금의 20% 제안
매수 신호 뒤 30분·1시간·종가 수익률과 최고 수익률을 기록해 두 방식의 성과를 비교한다.
실제 주문은 하지 않는다.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
import time as _time
from collections import Counter

import numpy as np
import pandas as pd

from .util import iso, to_date

GROUPS = ["live", "expected", "history"]
UNI_COLS = ["code", "name", "rank", "reason"]


def gcfg(cfg, g) -> dict:
    return cfg["watch"]["groups"][g]


def enabled(cfg) -> list[str]:
    return [g for g in GROUPS if gcfg(cfg, g).get("enabled", True)]


def label(cfg, g) -> str:
    return gcfg(cfg, g).get("label", g)


def _at(date, hhmm) -> dt.datetime:
    h, m = map(int, str(hhmm).split(":"))
    return dt.datetime.combine(to_date(date), dt.time(h, m))


def _f(x):
    try:
        x = float(x)
        return None if math.isnan(x) else x
    except (TypeError, ValueError):
        return None


def _int(x):
    x = _f(x)
    return None if x is None else int(x)


def save_universe(con, date, grp, df: pd.DataFrame, picked_at: str):
    con.execute("DELETE FROM watch_universe WHERE date=? AND grp=?", (iso(date), grp))
    for r in df.itertuples():
        con.execute("INSERT OR REPLACE INTO watch_universe VALUES (?,?,?,?,?,?,?)",
                    (iso(date), grp, r.code, r.name, _int(r.rank), r.reason, picked_at))
    con.commit()


def load_universe(con, date, grp) -> pd.DataFrame:
    rows = con.execute("SELECT code, name, rank, reason FROM watch_universe WHERE date=? AND grp=? ORDER BY rank",
                       (iso(date), grp)).fetchall()
    return pd.DataFrame([dict(r) for r in rows], columns=UNI_COLS)


# ------------------------------------------------------------------ ② 거래대금 이력
def record_day(con, src, date, n=100) -> int:
    """그날 거래대금 상위 n 저장 (evening 에서 매일 호출 → ② 그룹의 재료)"""
    df = src.value_top(date, n)
    d = iso(date)
    for r in df.itertuples():
        con.execute("INSERT OR REPLACE INTO value_rank_daily VALUES (?,?,?,?,?,?,?)",
                    (d, r.code, r.name, int(r.rank), float(r.value or 0), float(r.change_pct or 0),
                     float(r.close or 0)))
    con.commit()
    return len(df)


def lookback_dates(src, today, n) -> list[dt.date]:
    days = src.trading_days(to_date(today) - dt.timedelta(days=1), n)
    return [d for d in days if d < to_date(today)][-n:]


def ensure_history(cfg, con, src, today, quiet=False):
    h = gcfg(cfg, "history")
    for d in lookback_dates(src, today, h["lookback_days"]):
        if con.execute("SELECT 1 FROM value_rank_daily WHERE date=? LIMIT 1", (iso(d),)).fetchone():
            continue
        try:
            k = record_day(con, src, d, h["top_n"])
            if not quiet:
                print(f"[감시] {d} 거래대금 상위 {k}종목 저장")
        except Exception as e:
            if not quiet:
                print(f"[warn] {d} 거래대금 순위를 가져오지 못함 ({e.__class__.__name__}). "
                      f"과거 날짜는 KRX 로그인이 필요하고, 매일 evening 을 돌리면 자동으로 쌓입니다.")


_ETF = re.compile(r"^(KODEX|TIGER|KBSTAR|RISE|ACE|SOL|HANARO|ARIRANG|KOSEF|PLUS|TIMEFOLIO|KOACT|WOORI|BNK|FOCUS|"
                  r"TREX|UNICORN|VITA|KIWOOM|1Q|WON|TRUSTON|ITF|HK|에셋플러스|마이티|히어로즈|파워)\b|ETN|ETF|레버리지|인버스",
                  re.IGNORECASE)


def is_affiliate(a: str, b: str) -> bool:
    """같은 그룹 계열사로 볼 이름: 앞 3글자 이상이 같음 (한국화장품·한국화장품제조, 한진칼·한진칼우, 에코프로·에코프로비엠).
    '한국전력'·'한국가스공사', '삼성전자'·'삼성SDI' 처럼 앞 2글자만 같은 경우는 다른 회사로 본다."""
    a, b = str(a or "").replace(" ", ""), str(b or "").replace(" ", "")
    k = 0
    while k < min(len(a), len(b)) and a[k] == b[k]:
        k += 1
    return k >= 3


def is_etf(name: str) -> bool:
    return bool(_ETF.search(str(name or "")))


# ------------------------------------------------------------------ 신호 표시: 하락중반등 · 전일급등 · 시장강세/약세
NEW_TAGS = ("하락중반등", "전일급등", "시장강세", "시장약세")     # 저장된 기록에 다시 계산해 붙이는 표시


def market_breadth(df: pd.DataFrame, top_n=100, min_n=20):
    """거래대금 상위 top_n 종목(ETF·ETN 제외) 중 지금 전일 대비 오르고 있는 종목 비율(%). 종목이 min_n 보다 적으면 None"""
    if df is None or df.empty or "pct" not in df:
        return None
    rk = "rank" if "rank" in df else "mk_rank"
    d = df[pd.to_numeric(df[rk], errors="coerce").fillna(10 ** 6) <= top_n]
    if d.empty:
        return None
    d = d[~d["name"].map(is_etf).astype(bool)]
    pct = pd.to_numeric(d["pct"], errors="coerce").dropna()
    if len(pct) < max(1, int(min_n)):
        return None
    return round(float((pct > 0).mean() * 100), 1)


def prev_surge_map(con, cfg, prev_date) -> dict[str, float]:
    """전 거래일 거래대금 상위 100위 안에서 +15% 이상 오른 종목 {코드: 등락률}"""
    if not prev_date:
        return {}
    tg = cfg["watch"].get("tags", {})
    return {r["code"]: float(r["change_pct"]) for r in con.execute(
        "SELECT code, change_pct FROM value_rank_daily WHERE date=? AND rank<=? AND change_pct>=?",
        (iso(prev_date), int(tg.get("prev_surge_top_n", 100)), float(tg.get("prev_surge_pct", 15.0))))}


def extra_tags(tg: dict, pct, surge, breadth) -> list[str]:
    """하락중반등 = 신호 때 전일 대비 하락 중 / 전일급등 = 전 거래일 +15%↑ / 시장강세·약세 = 상위 100 중 상승 종목 비율"""
    t = []
    if pct is not None and pct < 0:
        t.append("하락중반등")
    if surge is not None:
        t.append(f"전일급등({surge:+.0f}%)")
    if breadth is not None:
        if breadth >= float(tg.get("market_strong_pct", 70.0)):
            t.append(f"시장강세(상승 {breadth:.0f}%)")
        elif breadth <= float(tg.get("market_weak_pct", 40.0)):
            t.append(f"시장약세(상승 {breadth:.0f}%)")
    return t


def retag(con, cfg, date=None) -> int:
    """이미 쌓인 신호에 하락중반등·전일급등·시장강세/약세를 다시 계산해 붙인다 (DB 만 사용, 네트워크 없음).
    시장 상승비율은 그 시각 ① 거래대금상위 스냅샷으로 계산하므로 이 기능이 생기기 전 기록에도 붙는다. 바뀐 행 수"""
    tg = cfg["watch"].get("tags", {})
    top_n = int(gcfg(cfg, "live").get("top_n", 100))
    min_n = int(tg.get("market_min_stocks", 20))
    days = sorted({r[0] for r in con.execute("SELECT DISTINCT date FROM value_rank_daily UNION "
                                             "SELECT DISTINCT date FROM watch_ticks2")})
    cond, args = ("WHERE date=?", (iso(date),)) if date else ("", ())
    surge_by: dict = {}
    br_by: dict = {}
    n = 0
    for table in ("watch_outcomes", "watch_signals2"):
        for r in con.execute(f"SELECT rowid AS rid, * FROM {table} {cond}", args).fetchall():
            d, hm = r["date"], r["time"]
            if d not in surge_by:       # 전 거래일 = 기록이 있는 날 중 바로 앞 날
                surge_by[d] = prev_surge_map(con, cfg, max((x for x in days if x < d), default=None))
            if (d, hm) not in br_by:
                ticks = pd.DataFrame([dict(x) for x in con.execute(
                    "SELECT name, pct, mk_rank FROM watch_ticks2 WHERE date=? AND time=? AND grp='live'", (d, hm))])
                br_by[(d, hm)] = market_breadth(ticks, top_n, min_n) if len(ticks) else None
            br = r["breadth"] if r["breadth"] is not None else br_by[(d, hm)]
            if "pct" in r.keys():
                pct = r["pct"]
            else:
                x = con.execute("SELECT pct FROM watch_ticks2 WHERE date=? AND grp=? AND time=? AND code=?",
                                (d, r["grp"], hm, r["code"])).fetchone()
                pct = x["pct"] if x else None
            keep = [x for x in (r["tags"] or "").split(",") if x and not x.startswith(NEW_TAGS)]
            tags = ",".join(keep + extra_tags(tg, _f(pct), surge_by[d].get(r["code"]), br))
            if tags != (r["tags"] or "") or br != r["breadth"]:
                con.execute(f"UPDATE {table} SET tags=?, breadth=? WHERE rowid=?", (tags, br, r["rid"]))
                n += 1
    con.commit()
    return n


def history_universe(cfg, con, src, today) -> pd.DataFrame:
    h = gcfg(cfg, "history")
    days = lookback_dates(src, today, h["lookback_days"])
    recs = []
    if days:
        for r in con.execute(
                """SELECT code, MAX(name) name, MAX(date) last_date, MIN(rank) best_rank, MAX(change_pct) max_pct
                   FROM value_rank_daily WHERE date>=? AND date<? AND rank<=? AND change_pct>=?
                   GROUP BY code ORDER BY last_date DESC, max_pct DESC""",
                (iso(days[0]), iso(today), h["top_n"], h["min_pct"])):
            if h.get("exclude_etf", True) and is_etf(r["name"]):
                continue
            recs.append({"code": r["code"], "name": r["name"], "rank": None,
                         "reason": f"{r['last_date']} 거래대금 {r['best_rank']}위, {r['max_pct']:+.1f}%"})
    have = {r["code"] for r in recs}
    for c in (str(c).strip().zfill(6) for c in (cfg["watch"].get("extra_codes") or []) if str(c).strip()):
        if c in have:
            continue
        have.add(c)
        nm = con.execute("SELECT name FROM stocks WHERE code=? UNION ALL "
                         "SELECT name FROM value_rank_daily WHERE code=? LIMIT 1", (c, c)).fetchone()
        recs.append({"code": c, "name": nm[0] if nm else "", "rank": None, "reason": "직접 추가(extra_codes)"})
    return pd.DataFrame(recs, columns=UNI_COLS)


# ------------------------------------------------------------------ ① 예상체결
_PREF = re.compile(r"(\d?우[A-C]?|우선주)$")


def is_pref_or_spac(code: str, name: str) -> bool:
    """우선주: 보통주 코드는 끝자리가 0 (005930 / 우선주 005935, 18064K) 또는 이름 끝 '우', '우B'. 스팩: 이름에 '스팩'."""
    code, name = str(code), str(name or "")
    return (code[-1:] != "0") or bool(_PREF.search(name)) or ("스팩" in name)


def pick_expected(cfg, con, feed, today, tag: str, quiet=False) -> pd.DataFrame:
    e = gcfg(cfg, "expected")
    raw = feed.expected_top(e["top_n"])
    df = raw.copy()
    if e.get("exclude_pref_spac", True):
        df = df[[not is_pref_or_spac(c, n) for c, n in zip(df["code"], df["name"])]]
    df = df[(df["qty"] >= e.get("min_qty", 0)) & (df["price"] >= e.get("min_price", 0))]
    df = df.head(e["top_n"]).reset_index(drop=True)
    df["rank"] = range(1, len(df) + 1)
    df["reason"] = [f"{tag} 예상 {p:+.2f}% ({q:,.0f}주)" for p, q in zip(df["pct"], df["qty"])]
    now = dt.datetime.now().strftime("%H:%M")
    save_universe(con, today, "expected", df[UNI_COLS], f"{tag} {now}")
    if not quiet:
        print(f"[감시] ①예상체결 {tag} 선정: {len(df)}종목 (원본 {len(raw)}개에서 우선주·스팩·소량·동전주 제외)")
    return df[UNI_COLS]


# ------------------------------------------------------------------ 종목별 테마
class ThemeBook:
    """종목이 속한 테마. 키움 '종목별 테마 조회'(ka90001) 결과를 DB 에 7일 캐시하고,
    조회가 안 되면 종목명 키워드(테마 사전)와 최근 테마 분석(theme_daily) 기록으로 대신한다."""

    def __init__(self, con, feed, cfg=None):
        self.con, self.feed, self.cfg = con, feed, cfg
        self.mem: dict[str, list[str]] = {}

    def _fallback(self, code, name) -> list[str]:
        from .theme import DEFAULT_THEMES
        d = dict(DEFAULT_THEMES)
        if self.cfg is not None:
            d.update(self.cfg.get("themes") or {})
        out = [t for t, kws in d.items() if any(k and k in str(name) for k in kws)]
        out += [r[0] for r in self.con.execute(
            "SELECT DISTINCT theme FROM theme_daily WHERE code=? AND theme NOT LIKE '#%' AND theme<>'개별 이슈'",
            (code,))]
        if "로봇" in str(name) and "로봇" not in out:
            out.append("로봇")
        return list(dict.fromkeys(out))

    def get(self, code, name="") -> list[str]:
        if code in self.mem:
            return self.mem[code]
        row = self.con.execute("SELECT themes, updated FROM stock_themes WHERE code=?", (code,)).fetchone()
        fresh = row and row["updated"] >= (dt.date.today() - dt.timedelta(days=7)).isoformat()
        if fresh:
            th = json.loads(row["themes"])
        else:
            th = None
            if hasattr(self.feed, "stock_themes"):
                try:
                    th = self.feed.stock_themes(code)
                    self.con.execute("INSERT OR REPLACE INTO stock_themes VALUES (?,?,?)",
                                     (code, json.dumps(th, ensure_ascii=False), dt.date.today().isoformat()))
                except Exception as e:
                    print(f"[warn] 테마 조회 실패 {name}({code}): {e.__class__.__name__}")
            if not th:
                th = self._fallback(code, name)
        self.mem[code] = th
        return th


# ------------------------------------------------------------------ 한 회차에 한 번만 조회
class TickFeed:
    """두 그룹이 같은 시세를 쓰도록 회차마다 합집합을 한 번만 조회한다."""

    def __init__(self, feed, live_n: int = 0, con=None, cfg=None):
        self.feed = feed
        self.live_n = live_n
        self.book = ThemeBook(con, feed, cfg) if con is not None else None
        self.cfg = cfg
        self._q = pd.DataFrame()
        self._live = pd.DataFrame()
        self._mk: dict = {}
        self._br = None
        self.leaders: set = set()                 # 주도주눌림용: 최근 급등 이력 종목
        self.prev_surge: dict[str, float] = {}    # 전일급등용: 전 거래일 +15%↑ 종목

    def begin(self, codes):
        codes = sorted(set(codes))
        self._q = self.feed.quotes(codes) if codes else pd.DataFrame()
        self._live = pd.DataFrame()
        self._br = None
        try:
            if self.live_n:     # ① 거래대금상위: 시장 순위 목록 자체가 시세 (한 번의 조회로 순위도 같이 얻음)
                self._live = self.feed.live_top(self.live_n)
                self._mk = dict(zip(self._live["code"], self._live["rank"]))
                tg = self.cfg["watch"].get("tags", {}) if self.cfg else {}
                top_n = gcfg(self.cfg, "live").get("top_n", 100) if self.cfg else 100
                self._br = market_breadth(self._live, top_n, tg.get("market_min_stocks", 20))
            else:
                self._mk = self.feed.market_rank(300)
        except Exception as e:
            print(f"[warn] 시장 거래대금 순위 조회 실패: {e}")
            self._mk = {}

    def live(self):
        return self._live.copy()

    def breadth(self):
        """이번 회차 거래대금 상위 100(ETF 제외) 중 상승 종목 비율(%) — ① 그룹을 켰을 때만"""
        return self._br

    def new_listings(self, date) -> set:
        d = iso(date)
        if getattr(self, "_new_day", None) != d:
            self._new_day, self._new = d, set()
            if hasattr(self.feed, "new_listings"):
                try:
                    self._new = set(self.feed.new_listings(date))
                except Exception as e:
                    print(f"[warn] 신규상장 목록 조회 실패: {e.__class__.__name__}")
        return self._new

    def themes(self, code, name=""):
        return self.book.get(code, name) if self.book else []

    def quotes(self, codes):
        if self._q.empty:
            return self._q
        return self._q[self._q["code"].isin(set(codes))].copy()

    def market_rank(self, n=300):
        return self._mk


# ------------------------------------------------------------------ 그룹별 감시
class Watcher:
    def __init__(self, cfg, con, feed, uni: pd.DataFrame, date, grp: str, now: dt.datetime | None = None):
        self.cfg, self.con, self.feed, self.uni, self.grp = cfg, con, feed, uni, grp
        self.w = cfg["watch"]
        self.date = iso(date)
        self.codes = list(uni["code"])
        self.names = dict(zip(uni["code"], uni["name"]))
        self.live = grp == "live"
        self.prev: pd.DataFrame | None = None
        self.prev_time: str | None = None
        self.start_hm = _at(self.date, self.w.get("start", "09:00")).strftime("%H:%M")
        last = con.execute("SELECT cash_left FROM watch_signals2 WHERE date=? AND grp=? AND kind='매수' "
                           "ORDER BY time DESC LIMIT 1", (self.date, grp)).fetchone()
        self.cash = float(last["cash_left"]) if last else float(self.w["cash"])
        self.bought = {r["code"] for r in con.execute(
            "SELECT DISTINCT code FROM watch_signals2 WHERE date=? AND grp=? AND kind='매수'", (self.date, grp))}
        t = con.execute("SELECT MAX(time) FROM watch_ticks2 WHERE date=? AND grp=?", (self.date, grp)).fetchone()[0]
        step = dt.timedelta(minutes=self.w["interval_min"])
        if t and now is not None and _at(self.date, t) < now - step * 2:
            t = None   # 오래 꺼져 있었으면 그 사이 변화가 한 번에 잡히지 않도록 새 기준값부터 시작
        if t:      # 같은 날 재시작: 마지막 스냅샷부터 이어서 비교
            rows = con.execute("SELECT code, price, pct, value, strength, wl_rank, mk_rank FROM watch_ticks2 "
                               "WHERE date=? AND grp=? AND time=?", (self.date, grp, t)).fetchall()
            self.prev = pd.DataFrame([dict(r) for r in rows]).set_index("code")
            self.prev_time = t

    def tick(self, now: dt.datetime, final=False) -> dict:
        w = self.w
        hm = now.strftime("%H:%M")
        top_n = gcfg(self.cfg, "live").get("top_n", 100) if self.live else None
        if self.live:
            q = self._live_quotes()
        else:
            q = self.feed.quotes(self.codes)
        if q.empty:
            return {"grp": self.grp, "time": hm, "rows": q, "signals": []}
        q = q.drop_duplicates("code").set_index("code")
        q["name"] = [n or self.names.get(c, c) for c, n in zip(q.index, q["name"])]
        if not self.live:
            blank = self.uni["name"].fillna("") == ""
            if blank.any():
                self.uni.loc[blank, "name"] = self.uni.loc[blank, "code"].map(q["name"]).fillna("")
                self.names.update({c: n for c, n in zip(self.uni["code"], self.uni["name"]) if n})
        q["wl_rank"] = q["value"].rank(ascending=False, method="first").astype(int)
        mk = self.feed.market_rank(300)
        q["mk_rank"] = [mk.get(c) for c in q.index]
        basis = "mk_rank" if (self.live or w["rank_basis"] == "market") else "wl_rank"

        gc = gcfg(self.cfg, self.grp)
        vd = float(gc.get("value_delta_eok", w["value_delta_eok"]))      # 그룹별로 기준을 다르게 쓸 수 있음
        pdel = float(gc.get("pct_delta", w["pct_delta"]))
        mode = gc.get("pct_mode", "pp")     # pp = 전일대비 등락률의 5분 변화(%p) / ret = 5분 전 가격 대비 상승률(%)
        for col in ("d_value", "d_pct", "d_ret", "d_strength", "per_won", "rank_prev", "rank_up"):
            q[col] = np.nan
        q["red"] = False
        q["buy"] = False
        # 장 시작 시각(09:00)에 찍은 값은 기준으로 쓰지 않는다: 그때 값은 전일 종가·장전 단일가라서
        # 09:05 비교가 '5분 변화'가 아니라 시초가 갭 전체가 된다 (11-20 한국화장품 09:05 +9.91% = 전일 대비 그대로).
        # 09:05 값을 기준으로 09:10 부터 비교한다.
        if self.prev is not None and self.prev_time != self.start_hm:
            p = self.prev.reindex(q.index)
            # 09:00 기준값이 장 시작 전(거래대금 0)이면 비교하지 않는다 → 시초가 갭 전체가 '5분 변화'로 잡히는 것 방지
            p = p.where(p["value"].fillna(0) > 0)
            q["d_value"] = q["value"] - p["value"]
            q["d_pct"] = (q["pct"] - p["pct"]).round(2)
            q["d_ret"] = ((q["price"] / p["price"].where(p["price"] > 0) - 1) * 100).round(2)
            q["d_strength"] = (q["strength"] - p["strength"]).round(2)
            dprice = q["price"] - p["price"]
            q["per_won"] = np.where(dprice.abs() > 0, q["d_value"] / dprice.where(dprice != 0), np.nan)
            q["rank_prev"] = p[basis]
            q["rank_up"] = q["rank_prev"] - q[basis]
            move = q["d_ret"] if mode == "ret" else q["d_pct"]
            red = (q["d_value"] >= vd * 1e8) & (move >= pdel)
            rank_ok = q["rank_up"] >= w["rank_up_min"]
            buy = (red & rank_ok) if w["buy_rule"] == "both" else rank_ok
            if w.get("exclude_new_listing", True):
                # 상장 첫날 종목은 신호 제외: 상장일이 오늘이거나, 등락률이 ±30% 를 넘음(첫날만 가격제한폭이 다름)
                new = set(self.feed.new_listings(self.date)) if hasattr(self.feed, "new_listings") else set()
                first_day = q.index.isin(list(new)) | (q["pct"].abs() > 30)
                red, buy = red & ~first_day, buy & ~first_day
            if self.live:       # 신호는 지금 100위 안에 있는 종목만 (101~200위는 다음 회차 비교용)
                inside = q["mk_rank"].fillna(10 ** 6) <= top_n
                if gc.get("exclude_etf_signals", True):     # ETF·ETN 은 순위 계산에만 쓰고 신호는 내지 않음
                    inside &= ~q["name"].map(is_etf)
                red, buy = red & inside, buy & inside
            q["red"] = red.fillna(False).astype(bool)
            q["buy"] = buy.fillna(False).astype(bool)

        signals = []
        br = self.feed.breadth() if hasattr(self.feed, "breadth") else None    # 시장 상승비율(%)
        tags = self._tags(now, q, br)
        for c, r in q.iterrows():
            kinds = (["주목"] if r.red else []) + ([("매수" if c not in self.bought else "재신호")] if r.buy else [])
            tg = ",".join(tags.get(c, []))
            for k in kinds:
                qty = amount = 0
                if k == "매수" and r.price > 0:
                    base = float(w["cash"]) if w.get("sizing", "fixed") == "fixed" else self.cash
                    qty = int(math.floor(base * w["buy_ratio"] / r.price))
                    amount = qty * r.price
                    self.cash -= amount
                    self.bought.add(c)
                    self.con.execute("INSERT OR REPLACE INTO watch_outcomes (date, grp, time, code, name, entry, qty, "
                                     "r30, r60, rclose, rmax, tags, breadth) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                     (self.date, self.grp, hm, c, r["name"], r.price, qty, None, None, None, 0.0, tg,
                                      br))
                sig = {"grp": self.grp, "time": hm, "code": c, "name": r["name"], "kind": k, "price": r.price,
                       "pct": r.pct, "d_value": r.d_value, "d_pct": r.d_pct, "d_ret": r.d_ret,
                       "mode": mode, "rank_prev": _int(r.rank_prev),
                       "rank_now": _int(r[basis]), "qty": qty, "amount": amount, "cash_left": self.cash,
                       "tags": tg, "breadth": br}
                signals.append(sig)
                self.con.execute("INSERT OR REPLACE INTO watch_signals2 (date, time, grp, code, name, kind, price, pct, "
                                 "d_value, d_pct, rank_prev, rank_now, qty, amount, cash_left, d_ret, tags, breadth) "
                                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (self.date, hm, self.grp, c, sig["name"], k, r.price, r.pct, _f(r.d_value),
                                  _f(r.d_pct), sig["rank_prev"], sig["rank_now"], qty, amount,
                                  self.cash if k == "매수" else None, _f(r.d_ret), tg, br))
        for c, r in q.iterrows():
            self.con.execute("INSERT OR REPLACE INTO watch_ticks2 (date, time, grp, code, name, price, pct, value, "
                             "strength, wl_rank, mk_rank, d_value, d_pct, d_strength, per_won, rank_prev, rank_up, "
                             "red, buy, d_ret) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (self.date, hm, self.grp, c, r["name"], r.price, r.pct, r.value, _f(r.strength),
                              int(r.wl_rank), _int(r.mk_rank), _f(r.d_value), _f(r.d_pct), _f(r.d_strength),
                              _f(r.per_won), _int(r.rank_prev), _int(r.rank_up), int(r.red), int(r.buy), _f(r.d_ret)))
        self._outcomes(now, q["price"], final)
        if self.live:           # 엑셀 '감시대상'에 이 시각의 100위 목록을 남김
            cur = q[q["mk_rank"].fillna(10 ** 6) <= top_n].sort_values("mk_rank")
            self.uni = pd.DataFrame({"code": list(cur.index), "name": list(cur["name"]),
                                     "rank": [int(x) for x in cur["mk_rank"]],
                                     "reason": [f"{hm} 거래대금 {int(r)}위, {p:+.2f}%" for r, p in
                                                zip(cur["mk_rank"], cur["pct"])]})
            save_universe(self.con, self.date, "live", self.uni, hm)
        self.con.commit()
        self.prev = q[["price", "pct", "value", "strength", "wl_rank", "mk_rank"]].copy()
        self.prev_time = hm
        n_watch = int((q["mk_rank"].fillna(10 ** 6) <= top_n).sum()) if self.live else len(q)
        return {"grp": self.grp, "time": hm, "rows": q.reset_index(), "signals": signals, "basis": basis,
                "n_watch": n_watch}

    def _tags(self, now, q, br=None) -> dict[str, list[str]]:
        """신호 종목 표시: 테마동반(같은 테마 종목이 최근 15분 안에 함께 신호) / 급락반등(전일 대비 -10% 이하) /
        상대강도(신호 때 전일 대비 +4~+15%: 시장과 상관없이 이미 강한 종목) /
        약한상승(신호 때 0~+4%) / 주도주눌림(최근 급등 이력 종목이 신호 때 전일 대비 하락 중) /
        하락중반등(신호 때 전일 대비 하락 중) / 전일급등(전 거래일 +15%↑) / 시장강세·시장약세(상위 100 중 상승 비율)"""
        leaders = set(getattr(self.feed, "leaders", set()) or set())
        surge = getattr(self.feed, "prev_surge", None) or {}
        tg = self.w.get("tags", {})
        crash = float(tg.get("crash_pct", -10.0))
        need = int(tg.get("theme_min_stocks", 3))
        since = (now - dt.timedelta(minutes=int(tg.get("theme_window_min", 15)))).strftime("%H:%M")
        sig = [c for c in q.index if q.at[c, "red"] or q.at[c, "buy"]]
        out: dict[str, list[str]] = {}
        if not sig:
            return out
        recent = {r["code"]: r["name"] for r in self.con.execute(
            "SELECT DISTINCT code, name FROM watch_signals2 WHERE date=? AND time>=? AND time<?",
            (self.date, since, now.strftime("%H:%M")))}
        recent.update({c: q.at[c, "name"] for c in sig})
        recent = {c: n for c, n in recent.items() if not is_etf(n)}
        themes_of = (lambda c, n: self.feed.themes(c, n)) if hasattr(self.feed, "themes") else (lambda c, n: [])
        th = {c: set(themes_of(c, n)) for c, n in recent.items()}
        for c in sig:
            t: list[str] = []
            mine = th.get(c, set())
            cnt: Counter = Counter()
            mates_by: dict[str, list[str]] = {}
            for x in mine:
                kept: list[str] = []          # 같은 테마 동료 — 나와 계열사이거나, 이미 센 동료와 계열사면 1개로 봄
                for o, n in recent.items():
                    if o == c or x not in th.get(o, set()) or is_affiliate(n, recent[c]):
                        continue
                    if any(is_affiliate(n, k) for k in kept):
                        continue
                    kept.append(n)
                if kept:
                    cnt[x] = len(kept)
                    mates_by[x] = kept
            if cnt:
                best, n = cnt.most_common(1)[0]
                if n + 1 >= need:
                    t.append(f"테마동반({best}: {'·'.join(mates_by[best][:3])})")
            pct = _f(q.at[c, "pct"]) or 0
            if pct <= crash:
                t.append("급락반등")
            if float(tg.get("rs_min_pct", 4.0)) <= pct <= float(tg.get("rs_max_pct", 15.0)):
                t.append("상대강도")
            if float(tg.get("weak_min_pct", 0.0)) <= pct < float(tg.get("weak_max_pct", 4.0)):
                t.append("약한상승")            # 지수 따라 조금 오르던 종목 — 6일치에서 매일 약했던 구간
            if pct < 0 and c in leaders:
                t.append("주도주눌림")          # 최근 급등 이력(②) 종목이 오늘 밀려 있다가 다시 거래대금이 붙음
            # 11-26·27 분석: 전날 크게 오른 종목은 다음 날 실패 / 하락 중 신호는 크게 오름 / 시장 분위기가 결과를 좌우
            t += extra_tags(tg, _f(q.at[c, "pct"]), surge.get(c), br)
            if t:
                out[c] = t
        return out

    def _live_quotes(self) -> pd.DataFrame:
        live = self.feed.live()
        if live.empty:
            return live
        if gcfg(self.cfg, "live").get("exclude_etf", True):
            live = live[~live["name"].map(is_etf)]
        live = live.assign(strength=np.nan)[["code", "name", "price", "pct", "value", "strength"]]
        # 산 종목이 200위 밖으로 밀려도 성과(30분·1시간·규칙 청산)를 계속 잴 수 있게 시세를 따로 붙임
        missing = [c for c in self.bought if c not in set(live["code"])]
        if missing:
            extra = self.feed.quotes(missing)
            if not extra.empty:
                live = pd.concat([live, extra[live.columns]], ignore_index=True)
        return live

    def bought_codes(self):
        return list(self.bought)

    def _outcomes(self, now, prices: pd.Series, final: bool):
        """매수 신호 이후 30분·1시간·종가 수익률, 최고 수익률 갱신"""
        for o in self.con.execute("SELECT * FROM watch_outcomes WHERE date=? AND grp=?", (self.date, self.grp)).fetchall():
            px = prices.get(o["code"])
            if px is None or not o["entry"]:
                continue
            ret = round((px / o["entry"] - 1) * 100, 2)
            st = _at(self.date, o["time"])
            upd = {"rmax": max(o["rmax"] or 0.0, ret)}
            if o["r30"] is None and now >= st + dt.timedelta(minutes=30):
                upd["r30"] = ret
            if o["r60"] is None and now >= st + dt.timedelta(minutes=60):
                upd["r60"] = ret
            if final:
                upd["rclose"] = ret
            sets = ", ".join(f"{k}=?" for k in upd)
            self.con.execute(f"UPDATE watch_outcomes SET {sets} WHERE date=? AND grp=? AND time=? AND code=?",
                             (*upd.values(), self.date, self.grp, o["time"], o["code"]))


def fill_close(con, src, date) -> int:
    """저녁에 실제 종가로 종가 수익률을 덮어쓴다 (15:20~15:30 동시호가 동안은 장중 시세가 멈춰 있어
    감시 마지막 값은 15:20 무렵 가격이다. 감시를 중간에 껐을 때도 여기서 채워진다)"""
    n = 0
    for o in con.execute("SELECT * FROM watch_outcomes WHERE date=?", (iso(date),)).fetchall():
        try:
            df = src.ohlcv(o["code"], date, date)
        except Exception:
            continue
        if len(df) and o["entry"]:
            con.execute("UPDATE watch_outcomes SET rclose=? WHERE date=? AND grp=? AND time=? AND code=?",
                        (round((float(df["close"].iloc[-1]) / o["entry"] - 1) * 100, 2),
                         o["date"], o["grp"], o["time"], o["code"]))
            n += 1
    con.commit()
    return n


# ------------------------------------------------------------------ 출력
def print_tick(cfg, res):
    w = cfg["watch"]
    rows = res["rows"]
    n_red = int(rows["red"].sum()) if len(rows) else 0
    print(f"[{res['time']}] {label(cfg, res['grp'])} {res.get('n_watch', len(rows))}종목 · 🔴 주목 {n_red} · 매수 "
          f"{sum(s['kind'] == '매수' for s in res['signals'])}")
    for s in res["signals"]:
        if s["kind"] == "주목":
            continue
        rk = f"{s['rank_prev']}→{s['rank_now']}위" if s["rank_prev"] else f"{s['rank_now']}위"
        msg = (f"   ▶ {s['kind']} {s['name']}({s['code']}) {s['price']:,.0f}원 {s['pct']:+.2f}% · "
               f"5분 대금 {(_f(s['d_value']) or 0) / 1e8:+.1f}억 · "
               + (f"5분 상승 {(_f(s.get('d_ret')) or 0):+.2f}%" if s.get("mode") == "ret"
                  else f"등락 {(_f(s['d_pct']) or 0):+.2f}%p") + f" · {rk}")
        if s.get("tags"):
            msg += "  [" + "] [".join(s["tags"].split(",")) + "]"
        if s["kind"] == "매수":
            msg += (f"\n     → 보유금 {w['buy_ratio'] * 100:.0f}% 매수 제안: {s['qty']:,}주 ({s['amount']:,.0f}원)")
        print(msg)


def beep():
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except Exception:
        print("\a", end="")


def simulate_exit(con, cfg, date=None) -> dict:
    """고정 익절·손절(+tp/-sl/end 정리) 결과만 {(date, grp, time, code): (수익률, 사유, 시각)} 로 (이전 호환)"""
    from .evaluate import evaluate
    return {(r["date"], r["grp"], r["time"], r["code"]): r["strat"]["fixed"]
            for r in evaluate(con, cfg, date) if r["strat"].get("fixed")}


def fetch_minute_bars(con, src, date, quiet=False) -> int:
    """그날 매수 신호가 난 종목의 1분봉을 받아 둔다 (장 마감 후). 키움이 없으면 0"""
    k = getattr(src, "kiwoom", None) or (src if hasattr(src, "minute_bars") else None)
    if k is None or not hasattr(k, "minute_bars"):
        return 0
    d = iso(date)
    n = 0
    for (code,) in con.execute("SELECT DISTINCT code FROM watch_outcomes WHERE date=?", (d,)).fetchall():
        if con.execute("SELECT COUNT(*) FROM minute_bars WHERE date=? AND code=?", (d, code)).fetchone()[0] >= 300:
            continue
        try:
            df = k.minute_bars(code, date)
        except Exception as e:
            if not quiet:
                print(f"[warn] 1분봉 실패 {code}: {e.__class__.__name__}")
            continue
        for r in df.itertuples():
            con.execute("INSERT OR REPLACE INTO minute_bars VALUES (?,?,?,?,?,?,?,?)",
                        (d, code, r.time, r.open, r.high, r.low, r.close, int(r.volume)))
        n += 1 if len(df) else 0
    con.commit()
    return n


def report(cfg, con, src, date, quiet=False):
    """장 마감 후: 1분봉 받기 → 신호 후 움직임·매도 방법 재계산 → 그날 감시 엑셀 다시 만들기"""
    n = fetch_minute_bars(con, src, date, quiet=quiet)
    unis = {g: load_universe(con, date, g) for g in GROUPS
            if con.execute("SELECT 1 FROM watch_ticks2 WHERE date=? AND grp=? LIMIT 1", (iso(date), g)).fetchone()}
    if not unis:
        if not quiet:
            print(f"[감시] {iso(date)} 감시 기록이 없습니다.")
        return None
    k = retag(con, cfg)         # 지난 기록 전체에 하락중반등·전일급등·시장강세/약세 (누적 '태그별성과' 용)
    if k and not quiet:
        print(f"[감시] 신호 {k}건에 하락중반등·전일급등·시장강세/약세 표시를 다시 계산했습니다.")
    path = write_excel(cfg, con, date, unis, cfg.sub("watch") / f"{iso(date)}_실시간감시.xlsx")
    if not quiet:
        print(f"[감시] 1분봉 {n}종목 반영, 신호 후 움직임·매도 방법 비교 갱신 → {path}")
    return path


def compare_rows(con, date=None):
    """그룹별 성과 요약. date=None 이면 누적"""
    cond, args = ("WHERE date=?", (iso(date),)) if date else ("", ())
    out = {}
    for r in con.execute(f"""SELECT grp, COUNT(*) n, AVG(r30) a30, AVG(r60) a60, AVG(rclose) ac, AVG(rmax) amax,
                                    SUM(CASE WHEN rclose>0 THEN 1 ELSE 0 END) win, COUNT(rclose) nc,
                                    SUM(qty*entry*rclose/100.0) pnl, COUNT(DISTINCT date) days
                             FROM watch_outcomes {cond} GROUP BY grp""", args):
        out[r["grp"]] = dict(r)
    sig = {r["grp"]: r["n"] for r in con.execute(
        f"SELECT grp, COUNT(*) n FROM watch_signals2 {cond} {'AND' if cond else 'WHERE'} kind='주목' GROUP BY grp",
        args)}
    return out, sig


def _has(k):
    return lambda r: k in (r.get("tags") or "")


def _mid_market(r):
    t = r.get("tags") or ""
    return r.get("breadth") is not None and "시장강세" not in t and "시장약세" not in t


# 엑셀 '태그별성과'의 구분 (한 신호가 여러 구분에 들어갈 수 있음)
TAG_CATS = [("테마동반", _has("테마동반")), ("급락반등", _has("급락반등")), ("상대강도", _has("상대강도")),
            ("약한상승", _has("약한상승")), ("주도주눌림", _has("주도주눌림")), ("하락중반등", _has("하락중반등")),
            ("전일급등", _has("전일급등")), ("전일급등 아님", lambda r: "전일급등" not in (r.get("tags") or "")),
            ("시장강세", _has("시장강세")), ("시장보통", _mid_market), ("시장약세", _has("시장약세")),
            ("태그 없음", lambda r: not r.get("tags"))]


def write_excel(cfg, con, date, unis: dict, path):
    from openpyxl import Workbook
    from openpyxl.formatting.rule import ColorScaleRule
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    from .excel import _sheet

    w = cfg["watch"]
    d = iso(date)
    RED = PatternFill("solid", fgColor="FFC7CE")
    BUY = PatternFill("solid", fgColor="FF7C80")
    basis = "mk_rank" if w["rank_basis"] == "market" else "wl_rank"
    wb = Workbook()
    wb.remove(wb.active)

    # 성과비교
    from .evaluate import evaluate, strategies, summarize
    ex = w.get("exit", {})
    strats = strategies(cfg)
    used = {r[0] for r in con.execute("SELECT DISTINCT grp FROM watch_signals2")}
    shown = [g for g in GROUPS if g in unis or g in used]      # 꺼 둔 그룹은 기록이 없으면 숨김
    evals = {"오늘": evaluate(con, cfg, date), "누적": evaluate(con, cfg, None)}
    ws = _sheet(wb, "성과비교", ["그룹", "기간", "감시 종목 수", "주목 신호", "매수 신호", "평균 최고 상승(%)",
                             "평균 최대 하락(%)", "상승 먼저(%)", "최고까지 평균(분)"] + [f"{n}(%)" for _, n in strats]
                + ["가장 좋은 방법", "일수"],
                [16, 8, 11, 9, 9, 13, 13, 11, 13] + [14] * len(strats) + [22, 6])
    for period, dd in (("오늘", date), ("누적", None)):
        out, sig = compare_rows(con, dd)
        for g in shown:
            sm = summarize([r for r in evals[period] if r["grp"] == g], cfg)
            ws.append([label(cfg, g), period, len(unis.get(g, [])) if period == "오늘" else None, sig.get(g, 0),
                       sm["n"], sm["up"], sm["down"], sm["first_up"], sm["up_min"]] + [sm[k] for k, _ in strats]
                      + [sm["best"], out.get(g, {}).get("days")])
    ws.append([])
    for line in (
            "평균 최고 상승 / 최대 하락 = 매수 신호 가격 대비, 신호 뒤 15:20 까지 가장 높았던 / 낮았던 가격 (1분봉이 있으면 1분봉 고가·저가).",
            f"상승 먼저 = +{ex.get('take_profit_pct', 3.0)}% 와 -{ex.get('stop_loss_pct', 3.0)}% 중 +쪽에 먼저 닿은 비율.",
            "매도 방법: 고정 = 익절/손절 / 고점-N% = 신호 뒤 최고가에서 N% 빠지면 매도(처음엔 신호가 -N%) / "
            "절반 = 목표에서 절반 팔고 나머지는 고점 대비 추적 / 종가 = 끝까지 보유 (저녁에 실제 종가).",
            "1분봉은 장 마감 후 evening(또는 watch --report)에서 받습니다. 그 전에는 5분 시세로 계산합니다."):
        ws.append([line])

    # 그룹별 실시간
    for g in GROUPS:
        if g not in unis:
            continue
        ws = _sheet(wb, f"실시간_{label(cfg, g)}",
                    ["순위", "이전순위", "순위변동", "시장순위", "종목명", "코드", "현재가", "등락률", "거래대금(억)",
                     "5분 대금증가(억)", "5분 등락률변화(%p)", "5분 상승률(%)", "가격1원당 유입(만원)", "체결강도",
                     "체결강도변화", "신호"],
                    [6, 8, 8, 8, 16, 9, 11, 9, 11, 13, 14, 11, 14, 9, 11, 8])
        t = con.execute("SELECT MAX(time) FROM watch_ticks2 WHERE date=? AND grp=?", (d, g)).fetchone()[0]
        if not t:
            continue
        for r in con.execute(f"SELECT * FROM watch_ticks2 WHERE date=? AND grp=? AND time=? "
                             f"ORDER BY {basis} IS NULL, {basis}", (d, g, t)):
            up = r["rank_up"]
            ws.append([r[basis], r["rank_prev"], (f"▲{up}" if up and up > 0 else (f"▼{-up}" if up else "")),
                       r["mk_rank"], r["name"], r["code"], r["price"], r["pct"], round((r["value"] or 0) / 1e8, 1),
                       None if r["d_value"] is None else round(r["d_value"] / 1e8, 2), r["d_pct"], r["d_ret"],
                       None if r["per_won"] is None else round(r["per_won"] / 1e4, 1), r["strength"],
                       r["d_strength"], "매수" if r["buy"] else ("주목" if r["red"] else "")])
            if r["red"] or r["buy"]:
                for c in ws[ws.max_row]:
                    c.fill = BUY if r["buy"] else RED
                if r["buy"]:
                    ws.cell(ws.max_row, 5).font = Font(bold=True)
        ws.cell(1, 17, f"기준 시각 {d} {t}")

    # 신호 성과 (신호 뒤 움직임)
    ws = _sheet(wb, "신호성과", ["그룹", "신호시각", "종목명", "코드", "신호가격", "제안수량", "태그",
                             "시장 상승비율(%)", "최고 상승(%)", "최고 시각", "최고까지(분)", "최대 하락(%)", "최저 시각", "먼저 온 것",
                             "10분 최고", "10분 최저", "30분 최고", "30분 최저", "60분 최고", "60분 최저"]
                + [f"{n}(%)" for _, n in strats] + ["데이터"],
                [16, 8, 16, 9, 11, 9, 34, 12, 11, 9, 11, 11, 9, 11, 9, 9, 9, 9, 9, 9] + [14] * len(strats) + [9])
    for r in evals["오늘"]:
        ws.append([label(cfg, r["grp"]), r["time"], r["name"], r["code"], r["entry"], r["qty"], r["tags"],
                   r.get("breadth"), r.get("up"), r.get("up_time"), r.get("up_min"), r.get("down"), r.get("down_time"), r.get("first"),
                   r.get("up10"), r.get("dn10"), r.get("up30"), r.get("dn30"), r.get("up60"), r.get("dn60")]
                  + [(r["strat"].get(k) or (None,))[0] for k, _ in strats] + [r["source"]])

    # 태그별 · 매도 방법별 성과
    ws = _sheet(wb, "태그별성과", ["그룹", "기간", "구분", "매수 신호", "평균 최고 상승(%)", "평균 최대 하락(%)",
                              "상승 먼저(%)", "최고까지 평균(분)"] + [f"{n}(%)" for _, n in strats]
                + [f"{n} 승률(%)" for _, n in strats] + ["가장 좋은 방법"],
                [16, 8, 12, 9, 13, 13, 11, 13] + [14] * len(strats) + [14] * len(strats) + [22])
    for period in ("오늘", "누적"):
        for g in shown:
            og = [r for r in evals[period] if r["grp"] == g]
            for lbl, pick in TAG_CATS:
                sm = summarize([r for r in og if pick(r)], cfg)
                ws.append([label(cfg, g), period, lbl, sm["n"], sm["up"], sm["down"], sm["first_up"], sm["up_min"]]
                          + [sm[k] for k, _ in strats] + [sm[k + "_win"] for k, _ in strats] + [sm["best"]])
    ws.append([])
    ws.append([f"테마동반 = 최근 {w.get('tags', {}).get('theme_window_min', 15)}분 안에 같은 테마(키움 테마 기준) 종목이 함께 "
               f"주목/매수 신호 · 급락반등 = 신호 때 전일 대비 {w.get('tags', {}).get('crash_pct', -10.0)}% 이하 · "
               f"상대강도 = 신호 때 전일 대비 +{w.get('tags', {}).get('rs_min_pct', 4.0)}~+{w.get('tags', {}).get('rs_max_pct', 15.0)}% · "
               f"약한상승 = 신호 때 +{w.get('tags', {}).get('weak_min_pct', 0.0)}~+{w.get('tags', {}).get('weak_max_pct', 4.0)}% 미만 · "
               "주도주눌림 = 최근 급등 이력(② 거래대금이력) 종목이 신호 때 전일 대비 하락 중 · "
               "하락중반등 = 신호 때 전일 대비 하락 중인 모든 종목 · "
               f"전일급등 = 전 거래일 거래대금 {w.get('tags', {}).get('prev_surge_top_n', 100)}위 안에서 "
               f"+{w.get('tags', {}).get('prev_surge_pct', 15.0)}% 이상 오른 종목 (전일급등 아님 = 그 밖의 전부) · "
               f"시장강세 / 시장약세 = 신호 시각 거래대금 상위 100(ETF 제외) 중 오르는 종목이 "
               f"{w.get('tags', {}).get('market_strong_pct', 70.0)}% 이상 / {w.get('tags', {}).get('market_weak_pct', 40.0)}% 이하, "
               "시장보통 = 그 사이. "
               "한 신호가 여러 태그를 가질 수 있음. 승률 = 그 방법으로 팔았을 때 플러스인 비율."])

    # 신호기록
    ws = _sheet(wb, "신호기록", ["그룹", "시각", "구분", "종목명", "코드", "현재가", "등락률", "5분 대금증가(억)",
                             "5분 등락률변화(%p)", "5분 상승률(%)", "이전순위", "현재순위", "제안수량", "제안금액",
                             "그룹 남은 보유금", "태그", "시장 상승비율(%)"],
                [16, 7, 7, 16, 9, 11, 9, 13, 14, 11, 8, 8, 10, 13, 14, 40, 12])
    for r in con.execute("SELECT * FROM watch_signals2 WHERE date=? ORDER BY time, grp, kind", (d,)):
        ws.append([label(cfg, r["grp"]), r["time"], r["kind"], r["name"], r["code"], r["price"], r["pct"],
                   None if r["d_value"] is None else round(r["d_value"] / 1e8, 2), r["d_pct"], r["d_ret"],
                   r["rank_prev"], r["rank_now"], r["qty"] or None, r["amount"] or None, r["cash_left"], r["tags"],
                   r["breadth"]])
        fill = BUY if r["kind"] == "매수" else (RED if r["kind"] == "주목" else None)
        if fill:
            for c in ws[ws.max_row]:
                c.fill = fill

    # 5분 대금 흐름
    times = [r[0] for r in con.execute("SELECT DISTINCT time FROM watch_ticks2 WHERE date=? ORDER BY time", (d,))]
    ws = _sheet(wb, "5분대금흐름(억)", ["그룹", "종목명"] + times, [14, 16] + [6] * len(times))
    grid = {(r["grp"], r["name"], r["time"]): r["d_value"] for r in
            con.execute("SELECT grp, name, time, d_value FROM watch_ticks2 WHERE date=?", (d,))}
    for g in GROUPS:
        for n in [r[0] for r in con.execute("SELECT name FROM watch_ticks2 WHERE date=? AND grp=? GROUP BY code "
                                            "ORDER BY MAX(value) DESC", (d, g))]:
            ws.append([label(cfg, g), n] + [None if grid.get((g, n, x)) is None else round(grid[(g, n, x)] / 1e8, 1)
                                            for x in times])
    if ws.max_row > 1 and times:
        ws.conditional_formatting.add(f"C2:{get_column_letter(len(times) + 2)}{ws.max_row}", ColorScaleRule(
            start_type="num", start_value=0, start_color="FFFFFF",
            end_type="num", end_value=max(w["value_delta_eok"] * 2, 1), end_color="F8696B"))

    # 감시 대상
    ws = _sheet(wb, "감시대상", ["그룹", "순위", "종목명", "코드", "선정 사유"], [16, 6, 16, 9, 44])
    for g in GROUPS:
        for r in unis.get(g, pd.DataFrame(columns=UNI_COLS)).itertuples():
            ws.append([label(cfg, g), r.rank, r.name, r.code, r.reason])

    # 기준
    e, h = gcfg(cfg, "expected"), gcfg(cfg, "history")
    ws = _sheet(wb, "기준", ["항목", "값"], [26, 80])
    lv = gcfg(cfg, "live")
    for row in ((label(cfg, "live"), f"{w['interval_min']}분마다 전체 시장 거래대금 상위 {lv.get('top_n', 100)}위 조회"
                 f"{' (ETF·ETN 제외)' if lv.get('exclude_etf', True) else ''}, 순위 = 전체 시장 거래대금 순위"),
                (label(cfg, "expected") + ("" if e.get("enabled") else " (사용 안 함)"),
                 f"{'/'.join(e['pick_times'])} 예상체결 등락률 상위 {e['top_n']} "
                 f"(우선주·스팩·ETF·ETN 제외, 예상체결량 {e['min_qty']:,}주↑, {e['min_price']:,}원↑)"),
                (label(cfg, "history"), f"최근 {h['lookback_days']}거래일 거래대금 {h['top_n']}위 이내 + "
                 f"등락률 {h['min_pct']}%↑" + (" + extra_codes" if w.get("extra_codes") else "")),
                *[(f"{label(cfg, g)} 주목·매수", _rule_text(cfg, g, basis)) for g in GROUPS if g in unis],
                ("매수 제안", f"그룹별 보유금 {w['cash']:,}원에서 남은 금액의 {w['buy_ratio'] * 100:.0f}%. "
                           f"같은 종목은 하루 1번 '매수', 이후 '재신호'"),
                ("주의", "실제 주문은 하지 않습니다. 수량·손익은 모의 계산입니다.")):
        ws.append(list(row))

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + "~tmp.xlsx")
    wb.save(tmp)
    try:
        tmp.replace(path)
        return path
    except PermissionError:
        alt = path.with_name(path.stem + "_최신.xlsx")
        try:
            tmp.replace(alt)
        except PermissionError:
            print(f"[warn] 엑셀이 열려 있어 저장하지 못했습니다: {path.name} (닫으면 다음 회차에 저장)")
            return None
        print(f"[info] {path.name} 이 열려 있어 {alt.name} 로 저장했습니다.")
        return alt


def _rule_text(cfg, g, basis) -> str:
    w, gc = cfg["watch"], gcfg(cfg, g)
    vd = float(gc.get("value_delta_eok", w["value_delta_eok"]))
    pdel = float(gc.get("pct_delta", w["pct_delta"]))
    move = (f"{w['interval_min']}분 전 가격 대비 +{pdel}% 이상 상승" if gc.get("pct_mode", "pp") == "ret"
            else f"{w['interval_min']}분 등락률 변화 +{pdel}%p 이상")
    cond = (f"{w['interval_min']}분 거래대금 +{vd}억 이상 AND " if vd > 0 else "") + move
    rank = "전체 시장 거래대금 순위" if (g == "live" or basis == "mk_rank") else "그룹 안 거래대금 순위"
    return (f"주목 = {cond} / 매수 = " + ("주목 + " if w["buy_rule"] == "both" else "")
            + f"{rank} {w['rank_up_min']}계단↑ 상승")


def _r(x, n=2):
    x = _f(x)
    return None if x is None else round(x, n)


# ------------------------------------------------------------------ 실행
def _feed(app, demo):
    if demo:
        return DemoFeed()
    k = getattr(app.src, "kiwoom", None) or (app.src if hasattr(app.src, "quotes") else None)
    if k is None:
        raise RuntimeError("5분 감시는 키움 REST API 가 필요합니다. config.yaml 의 keys.kiwoom_appkey / "
                           "kiwoom_secretkey 를 입력하세요.")
    return k


def _show(cfg, g, uni):
    print(f"[감시] {label(cfg, g)} {len(uni)}종목")
    for r in uni.head(15).itertuples():
        print(f"   - {r.name}({r.code}) {r.reason}")
    if len(uni) > 15:
        print(f"   ... 외 {len(uni) - 15}종목 (엑셀 '감시대상' 시트)")


def run(app, once=False, demo=False, list_only=False, clock=None, sleep=_time.sleep):
    cfg, con, src = app.cfg, app.con, app.src
    w = cfg["watch"]
    clock = clock or dt.datetime.now
    now = clock()
    today = now.date()
    groups = enabled(cfg)
    feed = _feed(app, demo)
    start, end = _at(today, w["start"]), _at(today, w["end"])
    unis: dict[str, pd.DataFrame] = {}

    if "history" in groups:
        if demo:
            _demo_history(cfg, con, src, today)
        else:
            ensure_history(cfg, con, src, today)
        unis["history"] = history_universe(cfg, con, src, today)
        if not list_only and not unis["history"].empty:
            save_universe(con, today, "history", unis["history"], now.strftime("%H:%M"))
        _show(cfg, "history", unis["history"])

    if "live" in groups:
        lv = gcfg(cfg, "live")
        unis["live"] = load_universe(con, today, "live")
        print(f"[감시] {label(cfg, 'live')}: 5분마다 전체 시장 거래대금 상위 {lv.get('top_n', 100)}위를 조회합니다"
              f"{' (ETF·ETN 제외)' if lv.get('exclude_etf', True) else ''}.")
        if list_only:
            try:
                tf0 = TickFeed(feed, live_n=lv.get("fetch_n", 200))
                tf0.begin([])
                cur = tf0.live()
                if lv.get("exclude_etf", True):
                    cur = cur[~cur["name"].map(is_etf)]
                cur = cur[cur["rank"] <= lv.get("top_n", 100)]
                unis["live"] = pd.DataFrame({"code": cur["code"], "name": cur["name"], "rank": cur["rank"],
                                             "reason": [f"지금 거래대금 {int(r)}위, {p:+.2f}%"
                                                        for r, p in zip(cur["rank"], cur["pct"])]})
                _show(cfg, "live", unis["live"])
            except Exception as ex:
                print(f"[warn] 거래대금 상위 조회 실패: {ex}")

    if "expected" in groups:
        e = gcfg(cfg, "expected")
        times = [_at(today, t) for t in e["pick_times"]]
        uni = load_universe(con, today, "expected")
        if demo:
            for i, t in enumerate(times):
                uni = pick_expected(cfg, con, feed, today, f"{i + 1}차" if i < len(times) - 1 else "확정")
        elif list_only or once or now >= start:
            if uni.empty:
                if now >= start:
                    print("[warn] 장 시작 후라 '예상체결' 순위가 아닌 값이 올 수 있습니다. 08:55~08:59 에 켜 두세요.")
                try:
                    uni = pick_expected(cfg, con, feed, today, "확정" if now >= times[-1] else "1차")
                except Exception as ex:
                    print(f"[warn] ①예상체결 선정 실패: {ex}")
        else:                                   # 장 시작 전: 08:55 1차 → 08:59 확정
            for i, t in enumerate(times):
                now = clock()
                if now < t:
                    print(f"[감시] ①예상체결 {t:%H:%M} 선정 대기...")
                    sleep(max(1.0, (t - now).total_seconds()))
                elif i < len(times) - 1 and clock() >= times[i + 1]:
                    continue                    # 늦게 켜서 1차 시각을 지났으면 바로 다음 단계로
                try:
                    uni = pick_expected(cfg, con, feed, today, "확정" if i == len(times) - 1 else f"{i + 1}차")
                except Exception as ex:
                    print(f"[warn] ①예상체결 선정 실패: {ex}")
        unis["expected"] = uni
        _show(cfg, "expected", uni)

    if list_only:
        return unis
    unis = {g: u for g, u in unis.items() if g == "live" or not u.empty}
    if not unis:
        print("감시할 종목이 없습니다. ①은 08:55~08:59 에 켜 두어야 하고, ②는 KRX 로그인 또는 며칠간의 evening 기록이 "
              "필요합니다. 급하면 config 의 watch.extra_codes 에 종목코드를 넣으세요.")
        return unis
    tf = TickFeed(feed, live_n=gcfg(cfg, "live").get("fetch_n", 200) if "live" in unis else 0, con=con, cfg=cfg)
    # '주도주눌림' 표시용: 최근 급등 이력 종목 (직접 추가한 종목은 제외)
    hu = unis.get("history")
    tf.leaders = set(hu.loc[hu["reason"] != "직접 추가(extra_codes)", "code"]) if hu is not None and len(hu) else set()
    # '전일급등' 표시용: 전 거래일 거래대금 100위 안 +15%↑ 종목 (evening 이 저장한 기록, 없으면 한 번 받아 봄)
    try:
        prev = lookback_dates(src, today, 1)
    except Exception:
        prev = []
    if prev:
        if not demo and not con.execute("SELECT 1 FROM value_rank_daily WHERE date=? LIMIT 1", (iso(prev[-1]),)).fetchone():
            try:
                record_day(con, src, prev[-1], 100)
            except Exception:
                pass
        tf.prev_surge = prev_surge_map(con, cfg, prev[-1])
        tg = w.get("tags", {})
        print(f"[감시] 전일({iso(prev[-1])}) 거래대금 {tg.get('prev_surge_top_n', 100)}위 안 "
              f"+{tg.get('prev_surge_pct', 15.0):g}%↑ {len(tf.prev_surge)}종목 → 신호에 '전일급등' 표시")
    watchers = {g: Watcher(cfg, con, tf, u, today, g, now=clock()) for g, u in unis.items()}
    fixed = sorted({c for g, u in unis.items() if g != "live" for c in u["code"]})
    path = cfg.sub("watch") / f"{iso(today)}_실시간감시.xlsx"
    step = dt.timedelta(minutes=w["interval_min"])

    def one(t, final=False):
        extra = watchers["live"].bought_codes() if "live" in watchers else []
        tf.begin(fixed + extra)
        results = [wt.tick(t, final=final) for wt in watchers.values()]
        return results

    if demo:
        t = start
        while t <= end:
            for res in one(t, final=(t + step > end)):
                if res["signals"]:
                    print_tick(cfg, res)
            t += step
        print(f"  → {write_excel(cfg, con, today, {g: wt.uni for g, wt in watchers.items()}, path)}")
        return unis

    first = True
    while True:
        now = clock()
        if not once and now < start:
            print(f"[감시] 장 시작({w['start']}) 대기 중...")
            sleep(max(1.0, (start - now).total_seconds()))
            continue
        if not once and now > end + dt.timedelta(minutes=1):
            print(f"[감시] 장 종료({w['end']}). 오늘 결과: {path}")
            break
        off = (now - start) % step
        if not once and dt.timedelta(seconds=30) < off and first:
            # 장중에 켠 경우: 12:09 → 12:10 처럼 1분짜리 비교가 생기지 않도록 다음 정각(12:10)에 기준값을 잡는다
            nxt = now - off + step
            print(f"[감시] 5분 간격을 맞추기 위해 {nxt:%H:%M} 부터 시작합니다.")
            first = False
            sleep(max(1.0, (nxt - now).total_seconds()))
            continue
        first = False
        try:
            results = one(now, final=(now + step > end + dt.timedelta(minutes=1)))
            for res in results:
                print_tick(cfg, res)
            if w.get("beep") and any(s["kind"] == "매수" for res in results for s in res["signals"]):
                beep()
            write_excel(cfg, con, today, {g: wt.uni for g, wt in watchers.items()}, path)
        except Exception as ex:
            print(f"[warn] {now:%H:%M} 감시 실패: {ex.__class__.__name__}: {str(ex)[:200]}")
        if once:
            print(f"  → {path}")
            break
        nxt = start + step * (int((now - start) / step) + 1)
        sleep(max(1.0, (nxt - clock()).total_seconds()))
    return unis


# ------------------------------------------------------------------ 데모
class DemoFeed:
    """가짜 시세: 대부분 잔잔하고, 가끔 대금이 몰리며 가격이 뛰는 종목이 나온다."""

    def __init__(self, seed=7):
        self.rng = np.random.default_rng(seed)
        self.state: dict[str, dict] = {}
        self.pool = [(f"9{i:04d}0", f"데모{i:03d}") for i in range(160)] + \
                    [("900015", "데모우"), ("900025", "데모스팩1호")]

    def _st(self, code, name=""):
        if code not in self.state:
            self.state[code] = {"name": name or code, "pct": 0.0, "value": 0.0, "strength": 100.0,
                                "base": 5000.0 + (hash(code) % 20) * 1000}
        return self.state[code]

    def expected_top(self, n=100):
        rows = []
        for i, (c, nm) in enumerate(self.pool):
            pct = round(float(self.rng.uniform(1, 25)), 2)
            rows.append({"code": c, "name": nm, "price": 1500.0 if i != 3 else 800.0, "pct": pct,
                         "qty": 50000.0 if i != 5 else 3000.0})
            self._st(c, nm)["pct"] = pct
        df = pd.DataFrame(rows).sort_values("pct", ascending=False).reset_index(drop=True)
        df["rank"] = range(1, len(df) + 1)
        return df

    def quotes(self, codes):
        out = []
        for c in codes:
            s = self._st(c)
            burst = self.rng.random() < 0.05
            s["value"] += self.rng.lognormal(19.2, 0.9) + (self.rng.uniform(6, 20) * 1e8 if burst else 0)
            s["pct"] = float(np.clip(s["pct"] + self.rng.normal(-0.02, 0.45) + (self.rng.uniform(1.5, 3.5) if burst
                                                                                  else 0), -29.9, 29.9))
            s["strength"] = float(np.clip(s["strength"] + self.rng.normal(0, 8) + (20 if burst else 0), 30, 300))
            out.append({"code": c, "name": s["name"], "price": round(s["base"] * (1 + s["pct"] / 100), -1),
                        "pct": round(s["pct"], 2), "value": s["value"], "strength": round(s["strength"], 2)})
        return pd.DataFrame(out, columns=["code", "name", "price", "pct", "value", "strength"])

    def market_rank(self, n=300):
        order = sorted(self.state, key=lambda c: -self.state[c]["value"])
        return {c: i + 1 for i, c in enumerate(order[:n])}

    def live_top(self, n=200):
        for c, nm in self.pool:
            self._st(c, nm)
        df = self.quotes([c for c, _ in self.pool]).sort_values("value", ascending=False).head(n)
        df = df.reset_index(drop=True)
        df["rank"] = range(1, len(df) + 1)
        return df


def _demo_history(cfg, con, src, today):
    h = gcfg(cfg, "history")
    for d in lookback_dates(src, today, h["lookback_days"]):
        df = src.value_top(d, h["top_n"])
        for r in df.itertuples():
            con.execute("INSERT OR REPLACE INTO value_rank_daily VALUES (?,?,?,?,?,?,?)",
                        (iso(d), r.code, r.name, int(r.rank), float(r.value), 25.0, float(r.close)))
    con.commit()
