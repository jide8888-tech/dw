from __future__ import annotations

import datetime as dt
import re
import time

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_session = requests.Session()
_session.headers.update({"User-Agent": UA})


def http_get(url: str, *, params=None, headers=None, encoding=None, retries=3,
             timeout=15, binary=False):
    last = None
    for i in range(retries):
        try:
            r = _session.get(url, params=params, headers=headers, timeout=timeout)
            r.raise_for_status()
            if binary:
                return r.content
            if encoding:
                r.encoding = encoding
            return r.text
        except requests.RequestException as e:  # 네트워크 일시 오류 재시도
            last = e
            time.sleep(1.5 * (i + 1))
    raise last  # type: ignore[misc]


def to_date(s) -> dt.date:
    if isinstance(s, dt.datetime):
        return s.date()
    if isinstance(s, dt.date):
        return s
    s = str(s).replace("-", "").replace(".", "")
    return dt.datetime.strptime(s[:8], "%Y%m%d").date()


def ymd(d) -> str:
    return to_date(d).strftime("%Y%m%d")


def iso(d) -> str:
    return to_date(d).isoformat()


def strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    for a, b in (("&quot;", '"'), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&#39;", "'"), ("&apos;", "'"), ("&nbsp;", " ")):
        s = s.replace(a, b)
    return s.strip()


def fmt_num(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "-"


def fmt_eok(won) -> str:
    """원 → '3,003억' 형식"""
    try:
        return f"{int(won) / 1e8:,.0f}억"
    except (TypeError, ValueError):
        return "-"
