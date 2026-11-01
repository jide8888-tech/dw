"""기사 제목에서 상한가/거래량 급증 '사유'와 키워드를 뽑는다."""
from __future__ import annotations

import re
from collections import Counter

STOP = set("""
특징주 상한가 급등 급락 상승 하락 강세 약세 주가 종목 오늘 증시 코스닥 코스피 거래량 장중 마감
개장 투자자 외국인 기관 개인 매수 매도 시장 기대 기대감 관련주 수혜 수혜주 테마 테마주 부각 소식
상한 하한가 속보 단독 종합 뉴스 기자 이유 전일 대비 상승세 하락세 연속 신고가 신저가 돌파 급증 영향 가운데
전망 분석 이번 지난 올해 내년 관련 대한 위해 통해 오전 오후 기록 발표 공개 추진 예정 가능성
주식 주주 증권 보고서 리포트 이슈 확대 우려 반등 폭등 랠리 지속 계속 등락 거래 회사 기업 대표
""".split())
PARTICLES = ("으로", "에서", "에게", "까지", "부터", "보다", "처럼", "은", "는", "이", "가", "을",
             "를", "에", "의", "로", "와", "과", "도", "만")


def tokens(text: str) -> list[str]:
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)|<[^>]*>", " ", text)
    out = []
    for w in re.findall(r"[가-힣A-Za-z0-9]{2,}", text):
        if w in STOP:
            continue
        for p in PARTICLES:
            if w.endswith(p) and len(w) - len(p) >= 2:
                w = w[: -len(p)]
                break
        if w in STOP or w.isdigit() or re.fullmatch(r"\d+(%|억|만|원|배)?", w):
            continue
        out.append(w)
    return out


def keywords(titles: list[str], exclude: set[str] = frozenset(), top: int = 8) -> list[tuple[str, int]]:
    c = Counter()
    for t in titles:
        c.update(set(tokens(t)))   # 기사당 1회
    for e in exclude:
        c.pop(e, None)
    return [(k, n) for k, n in c.most_common(top * 2) if len(k) >= 2][:top]


def reason(name: str, titles: list[str]) -> str:
    """가장 설명력 있는 제목 1개를 사유로 고른다."""
    if not titles:
        return "(관련 기사 없음 — 직접 확인 필요)"
    def score(t):
        s = 0
        s += 3 if "특징주" in t else 0
        s += 2 if name in t else 0
        s += 1 if re.search(r"계약|수주|개발|승인|인수|합병|최초|정책|협력|공급|임상|출시|지정", t) else 0
        return s
    return max(titles, key=score)


def focus_tags(text: str, focus: dict[str, list[str]]) -> list[str]:
    return [sector for sector, kws in (focus or {}).items()
            if any(k and k in text for k in kws)]
