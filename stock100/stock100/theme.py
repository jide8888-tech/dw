"""그날 거래대금 상위 + 고등락률 종목을 뉴스로 묶어 테마를 만들고, 테마의 형성·확산을 추적한다."""
from __future__ import annotations

from collections import Counter, defaultdict

from .util import iso

# 기본 테마 사전 (config.yaml 의 themes 로 추가/덮어쓰기 가능)
DEFAULT_THEMES = {
    "반도체": ["반도체", "HBM", "파운드리", "웨이퍼", "패키징", "D램", "낸드", "메모리", "CXL", "유리기판"],
    "AI": ["AI", "인공지능", "챗GPT", "데이터센터", "GPU", "엔비디아", "LLM", "온디바이스"],
    "2차전지": ["2차전지", "이차전지", "배터리", "양극재", "음극재", "전해질", "리튬", "전고체", "분리막"],
    "로봇": ["로봇", "휴머노이드", "협동로봇", "자율주행로봇"],
    "바이오": ["바이오", "신약", "임상", "기술이전", "FDA", "항암", "치료제", "비만", "ADC", "백신", "제약"],
    "원전": ["원전", "원자력", "SMR", "소형모듈원자로", "우라늄"],
    "방산": ["방산", "방위산업", "K9", "미사일", "전차", "무기 수출", "국방"],
    "조선": ["조선", "LNG선", "선박 수주", "해운", "컨테이너선"],
    "전력·전선": ["전력", "변압기", "전선", "송전", "전력망", "ESS"],
    "우주항공": ["우주", "위성", "발사체", "항공", "UAM", "드론"],
    "게임": ["게임", "신작", "퍼블리싱", "MMORPG", "넥슨", "크래프톤"],
    "엔터·미디어": ["엔터", "아이돌", "콘서트", "K팝", "드라마", "웹툰", "OTT", "콘텐츠"],
    "화장품·소비재": ["화장품", "K뷰티", "뷰티", "면세", "중국 소비", "음식료", "K푸드", "라면"],
    "자동차": ["자동차", "전기차", "자율주행", "완성차", "車"],
    "건설·인프라": ["건설", "재건", "인프라", "SOC", "부동산", "재개발"],
    "정책·정치": ["대선", "총선", "정책", "정부", "규제 완화", "관세", "탄핵", "남북", "대북"],
    "금융·증권": ["증권", "은행", "금리", "밸류업", "배당", "STO", "토큰증권", "가상자산", "비트코인"],
    "에너지·화학": ["유가", "석유", "가스", "수소", "태양광", "풍력", "정유", "화학"],
    "초전도체·신소재": ["초전도체", "그래핀", "신소재", "맥신"],
    "의료기기": ["의료기기", "진단", "임플란트", "미용기기"],
}


def theme_dict(cfg) -> dict:
    d = {k: list(v) for k, v in DEFAULT_THEMES.items()}
    for k, v in (cfg.get("themes") or {}).items():
        d[k] = list(v or [])
    for k, v in (cfg["focus_sectors"] or {}).items():   # 주력 분야도 테마로 인식
        d.setdefault(k, list(v or []))
    return d


def assign(items: list[dict], themes: dict) -> None:
    """item['theme'], item['themes_all'] 채움. 1) 테마 사전 2) 같은 날 공통 키워드 묶음 3) 개별 이슈"""
    for it in items:
        text = " ".join(a["title"] for a in it.get("articles", [])) + " " + " ".join(it.get("keywords", []))
        hits = []
        for order, (name, kws) in enumerate(themes.items()):
            n = sum(text.count(k) for k in kws if k)
            if n:
                hits.append((-n, order, name))   # 매칭 횟수 많은 순, 동점이면 사전 순서
        hits.sort()
        it["themes_all"] = [h[2] for h in hits]
        it["theme"] = hits[0][2] if hits else None
    # 사전에 없는 테마: 오늘 2종목 이상이 공유하는 키워드로 묶는다 (새로 생기는 테마 포착)
    kw_count = Counter(k for it in items for k in set(it.get("keywords", [])))
    for it in items:
        if it["theme"]:
            continue
        shared = [(kw_count[k], k) for k in it.get("keywords", []) if kw_count[k] >= 2]
        if shared:
            it["theme"] = "#" + max(shared)[1]
            it["themes_all"] = [it["theme"]]
        else:
            it["theme"] = "개별 이슈"
            it["themes_all"] = []


def build(cfg, con, review_res: dict) -> dict:
    d = review_res["date"]
    items = [it for it in review_res["items"] if it.get("is_hot")]
    assign(items, theme_dict(cfg))

    groups: dict[str, list] = defaultdict(list)
    for it in items:
        groups[it["theme"]].append(it)

    con.execute("DELETE FROM theme_daily WHERE date=?", (d,))
    themes = []
    for name, its in groups.items():
        its.sort(key=lambda x: (-x["change_pct"], -(x["value"] or 0)))
        leader = its[0]
        for it in its:
            con.execute("INSERT OR REPLACE INTO theme_daily VALUES (?,?,?,?,?,?)",
                        (d, name, it["code"], it["change_pct"], it["value"], int(it is leader)))
        kws = Counter(k for it in its for k in it["keywords"])
        themes.append({
            "theme": name, "items": its, "n": len(its), "leader": leader,
            "value_sum": sum(it["value"] or 0 for it in its),
            "avg_pct": sum(it["change_pct"] for it in its) / len(its),
            "keywords": [k for k, _ in kws.most_common(6)],
            "news": leader["reason"],
            "news_link": next((a["link"] for a in leader.get("articles", []) if a["title"] == leader["reason"]), ""),
        })
    con.commit()
    for t in themes:
        t.update(_history(con, d, t["theme"], t["n"]))
    # 테마 우선순위: 종목 수 → 거래대금 합계 ('개별 이슈'는 맨 뒤)
    themes.sort(key=lambda t: (t["theme"] == "개별 이슈", -t["n"], -t["value_sum"]))
    return {"date": d, "themes": themes, "items": items, "history": history_table(con, d)}


def _history(con, d, theme, n_today, lookback=20):
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM theme_daily WHERE date<? ORDER BY date DESC LIMIT ?", (d, lookback))]
    if theme == "개별 이슈":
        return {"status": "-", "streak": 1, "appear": 0, "prev_n": 0}
    present = {r[0]: r[1] for r in con.execute(
        "SELECT date, COUNT(*) FROM theme_daily WHERE theme=? AND date<? GROUP BY date", (theme, d))}
    streak = 1
    for x in dates:
        if x in present:
            streak += 1
        else:
            break
    prev_n = present.get(dates[0], 0) if dates else 0
    appear = sum(1 for x in dates if x in present)
    if not appear:
        status = "신규 형성"
    elif prev_n == 0:
        status = "재부각"          # 쉬었다가 다시 등장 — 책: 시장은 반복된다
    elif n_today > prev_n:
        status = "확산"
    elif n_today < prev_n:
        status = "축소"
    else:
        status = "지속"
    return {"status": status, "streak": streak, "appear": appear, "prev_n": prev_n}


def history_table(con, d, lookback=20):
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM theme_daily WHERE date<=? ORDER BY date DESC LIMIT ?", (d, lookback))][::-1]
    if not dates:
        return {"dates": [], "rows": []}
    rows = defaultdict(dict)
    q = f"SELECT date, theme, COUNT(*) n FROM theme_daily WHERE date IN ({','.join('?' * len(dates))}) " \
        f"AND theme<>'개별 이슈' GROUP BY date, theme"
    for r in con.execute(q, dates):
        rows[r["theme"]][r["date"]] = r["n"]
    out = sorted(rows.items(), key=lambda kv: (-sum(kv[1].values()), kv[0]))
    return {"dates": dates, "rows": [(t, [v.get(x, 0) for x in dates]) for t, v in out]}
