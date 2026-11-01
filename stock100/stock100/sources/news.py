"""뉴스 소스: 구글 뉴스 RSS (키 불필요) + 네이버 검색 API (키 필요)."""
from __future__ import annotations

import datetime as dt
import email.utils
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import quote

from ..util import http_get, strip_tags, to_date


@dataclass
class Article:
    title: str
    link: str
    date: str      # YYYY-MM-DD
    source: str = ""


def _rfc822(s: str) -> str:
    try:
        return email.utils.parsedate_to_datetime(s).date().isoformat()
    except Exception:
        return ""


def _google_rss(url: str) -> list[Article]:
    xml = http_get(url)
    root = ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    out = []
    for it in root.iter("item"):
        title = strip_tags(it.findtext("title") or "")
        src = it.findtext("source") or ""
        if src and title.endswith(" - " + src):
            title = title[: -len(src) - 3]
        out.append(Article(title, it.findtext("link") or "", _rfc822(it.findtext("pubDate") or ""), src))
    return out


class NewsClient:
    def __init__(self, cfg):
        self.kinds = cfg["sources"]["news"]
        self.nid = cfg.key.get("naver_client_id")
        self.nsecret = cfg.key.get("naver_client_secret")

    # ----- 종목 관련 기사 (복습)
    def search(self, query: str, around=None, days_before: int = 3, limit: int = 20) -> list[Article]:
        arts: list[Article] = []
        if "google" in self.kinds:
            q = f'"{query}"'
            if around is not None:
                d = to_date(around)
                q += f" after:{(d - dt.timedelta(days=days_before)).isoformat()}" \
                     f" before:{(d + dt.timedelta(days=2)).isoformat()}"
            else:
                q += " when:7d"
            url = f"https://news.google.com/rss/search?q={quote(q)}&hl=ko&gl=KR&ceid=KR:ko"
            try:
                arts += _google_rss(url)
            except Exception as e:
                print(f"[warn] 구글 뉴스 실패: {query}: {e}")
        # 네이버 검색 API 는 최신순만 지원 → 최근 7일 이내 날짜일 때만 사용
        recent = around is None or to_date(around) >= dt.date.today() - dt.timedelta(days=7)
        if "naver" in self.kinds and recent:
            arts += self._naver(query, limit)
        return _dedupe(arts)[:limit]

    def _naver(self, query: str, display: int = 20) -> list[Article]:
        if not (self.nid and self.nsecret):
            return []
        try:
            txt = http_get("https://openapi.naver.com/v1/search/news.json",
                           params={"query": query, "display": display, "sort": "date"},
                           headers={"X-Naver-Client-Id": self.nid,
                                    "X-Naver-Client-Secret": self.nsecret})
            items = json.loads(txt).get("items", [])
        except Exception as e:
            print(f"[warn] 네이버 뉴스 실패: {query}: {e}")
            return []
        return [Article(strip_tags(i["title"]), i.get("originallink") or i["link"],
                        _rfc822(i.get("pubDate", "")), "naver") for i in items]

    # ----- 새벽 헤드라인 (예습)
    def headlines(self, queries: list[str]) -> list[Article]:
        arts: list[Article] = []
        if "google" in self.kinds:
            for url in ("https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=ko&gl=KR&ceid=KR:ko",
                        "https://news.google.com/rss?hl=ko&gl=KR&ceid=KR:ko"):
                try:
                    arts += _google_rss(url)
                except Exception as e:
                    print(f"[warn] 구글 헤드라인 실패: {e}")
            for q in queries:
                try:
                    arts += _google_rss(f"https://news.google.com/rss/search?q={quote(q + ' when:1d')}"
                                        f"&hl=ko&gl=KR&ceid=KR:ko")
                except Exception as e:
                    print(f"[warn] 구글 검색 실패: {q}: {e}")
        if "naver" in self.kinds:
            for q in queries:
                arts += self._naver(q, 50)
        return _dedupe(arts)


def _dedupe(arts):
    seen, out = set(), []
    for a in arts:
        k = a.title[:40]
        if k in seen or not a.title:
            continue
        seen.add(k)
        out.append(a)
    return out


def make_news(cfg):
    if cfg["sources"]["prices"] == "demo":
        from .demo import DemoNews
        return DemoNews()
    return NewsClient(cfg)
