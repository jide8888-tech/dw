"""테마 분석 결과를 엑셀(.xlsx)로 저장."""
from __future__ import annotations

from openpyxl import Workbook
from openpyxl.formatting.rule import ColorScaleRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

HEAD_FILL = PatternFill("solid", fgColor="1F3864")
HEAD_FONT = Font(bold=True, color="FFFFFF")
STATUS_FILL = {"신규 형성": "FCE4D6", "확산": "F8CBAD", "재부각": "FFF2CC", "지속": "E2EFDA", "축소": "DDEBF7"}
LINK_FONT = Font(color="0563C1", underline="single")


def _sheet(wb, title, headers, widths):
    ws = wb.create_sheet(title)
    ws.append(headers)
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
        c = ws.cell(row=1, column=i)
        c.fill, c.font = HEAD_FILL, HEAD_FONT
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.freeze_panes = "A2"
    return ws


def _link(cell, url):
    if url and url.startswith("http"):
        cell.hyperlink = url
        cell.font = LINK_FONT


def _finish(ws, pct_cols=(), eok_cols=(), int_cols=()):
    last = ws.max_row
    if last < 2:
        return
    ws.auto_filter.ref = ws.dimensions
    for col in pct_cols:
        L = get_column_letter(col)
        for r in range(2, last + 1):
            ws[f"{L}{r}"].number_format = '+0.00"%";-0.00"%"'
        ws.conditional_formatting.add(f"{L}2:{L}{last}", ColorScaleRule(
            start_type="num", start_value=0, start_color="FFFFFF",
            end_type="num", end_value=30, end_color="F8696B"))
    for col in eok_cols:
        L = get_column_letter(col)
        for r in range(2, last + 1):
            ws[f"{L}{r}"].number_format = '#,##0"억"'
    for col in int_cols:
        L = get_column_letter(col)
        for r in range(2, last + 1):
            ws[f"{L}{r}"].number_format = "#,##0"


def eok(won):
    return round((won or 0) / 1e8)


def write(cfg, res: dict, path):
    f = cfg["filters"]
    wb = Workbook()
    wb.remove(wb.active)

    # 1) 테마 요약
    ws = _sheet(wb, "테마요약",
                ["순위", "테마", "상태", "연속일", "최근20일 등장", "종목수", "전일 종목수", "거래대금 합계",
                 "평균 등락률", "대장주", "대장주 등락률", "대장주 거래대금", "핵심 키워드", "대표 뉴스(대장주)", "종목 목록"],
                [6, 16, 10, 8, 10, 8, 10, 13, 11, 14, 11, 13, 30, 60, 50])
    for i, t in enumerate(res["themes"], 1):
        L = t["leader"]
        ws.append([i, t["theme"], t["status"], t["streak"], t["appear"], t["n"], t["prev_n"], eok(t["value_sum"]),
                   t["avg_pct"], L["name"], L["change_pct"], eok(L["value"]), ", ".join(t["keywords"]),
                   t["news"], ", ".join(f"{x['name']}({x['change_pct']:+.1f}%)" for x in t["items"])])
        _link(ws.cell(row=ws.max_row, column=14), t["news_link"])
        fill = STATUS_FILL.get(t["status"])
        if fill:
            ws.cell(row=ws.max_row, column=3).fill = PatternFill("solid", fgColor=fill)
    _finish(ws, pct_cols=(9, 11), eok_cols=(8, 12))

    # 2) 종목 상세
    ws = _sheet(wb, "종목상세",
                ["테마", "관련 테마(복수)", "종목명", "코드", "시장", "등락률", "거래대금", "거래량", "종가", "시가총액",
                 "구분", "대장주", "상승 사유(대표 뉴스)", "키워드", "과거 등장 횟수", "최근 등장일", "최근 등장 사유"],
                [16, 24, 14, 9, 8, 10, 12, 14, 11, 12, 16, 7, 60, 30, 10, 12, 50])
    for t in res["themes"]:
        for it in t["items"]:
            last = it["repeat"][0] if it["repeat"] else {}
            ws.append([t["theme"], ", ".join(it["themes_all"]), it["name"], it["code"], it["market"],
                       it["change_pct"], eok(it["value"]), it["volume"], it["close"], eok(it["marcap"]),
                       it["types"], "★" if it is t["leader"] else "", it["reason"], ", ".join(it["keywords"][:6]),
                       it["repeat_n"], last.get("date", ""), last.get("reason", "")])
            link = next((a["link"] for a in it.get("articles", []) if a["title"] == it["reason"]), "")
            _link(ws.cell(row=ws.max_row, column=13), link)
    _finish(ws, pct_cols=(6,), eok_cols=(7, 10), int_cols=(8, 9))

    # 3) 뉴스
    ws = _sheet(wb, "뉴스", ["테마", "종목명", "날짜", "제목", "출처"], [16, 14, 12, 90, 16])
    for t in res["themes"]:
        for it in t["items"]:
            for a in it.get("articles", [])[:10]:
                ws.append([t["theme"], it["name"], a.get("date", ""), a["title"], a.get("source", "")])
                _link(ws.cell(row=ws.max_row, column=4), a.get("link", ""))
    _finish(ws)

    # 4) 테마 흐름 (최근 20 분석일 × 테마별 종목 수)
    h = res["history"]
    ws = _sheet(wb, "테마흐름(20일)", ["테마"] + [x[5:] for x in h["dates"]] + ["합계"],
                [16] + [7] * len(h["dates"]) + [7])
    for name, counts in h["rows"]:
        ws.append([name] + counts + [sum(counts)])
    if h["rows"] and h["dates"]:
        rng = f"B2:{get_column_letter(len(h['dates']) + 1)}{ws.max_row}"
        ws.conditional_formatting.add(rng, ColorScaleRule(
            start_type="num", start_value=0, start_color="FFFFFF",
            end_type="max", end_color="F4B183"))

    # 5) 기준
    ws = _sheet(wb, "기준", ["항목", "값"], [24, 70])
    for row in (("분석일", res["date"]),
                ("대상 조건", f"거래대금 {f.get('value_min_eok', 100):,}억 원 이상 AND 등락률 {f.get('hot_pct_min', 10)}% 이상"),
                ("대상 종목 수", len(res["items"])),
                ("테마 분류", "① 테마 사전(기본+config themes) 키워드 매칭 → ② 같은 날 2종목 이상 공유 키워드('#키워드') → ③ 개별 이슈"),
                ("상태", "신규 형성: 최근 20일 첫 등장 / 확산: 전일보다 종목 증가 / 지속 / 축소 / 재부각: 쉬었다 재등장"),
                ("주의", "사유·테마는 기사 제목 기반 자동 추정입니다. 반드시 직접 확인하세요."),
                ("거래대금", "KRX 데이터는 실제 값, 네이버 상승종목 페이지는 종가×거래량 근사치")):
        ws.append(list(row))
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path
