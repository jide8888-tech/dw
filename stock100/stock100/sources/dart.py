"""OpenDART: 최대주주 지분, 최근 공시, 재무 추이, 회사 개요 (DART_API_KEY 필요)."""
from __future__ import annotations

import datetime as dt
import io
import json
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from ..util import http_get

BASE = "https://opendart.fss.or.kr/api"


def _int(s):
    try:
        return int(str(s).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


class Dart:
    def __init__(self, key: str, cache_dir: Path):
        self.key = key
        self.cache = cache_dir / "dart_corpcode.json"
        self._map: dict | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    def _get(self, api: str, **params):
        params["crtfc_key"] = self.key
        data = json.loads(http_get(f"{BASE}/{api}", params=params))
        if data.get("status") not in ("000", None):
            return None
        return data

    def corp_code(self, stock_code: str) -> str | None:
        if self._map is None:
            if self.cache.exists() and \
                    dt.datetime.fromtimestamp(self.cache.stat().st_mtime) > dt.datetime.now() - dt.timedelta(days=30):
                self._map = json.loads(self.cache.read_text(encoding="utf-8"))
            else:
                raw = http_get(f"{BASE}/corpCode.xml", params={"crtfc_key": self.key}, binary=True)
                with zipfile.ZipFile(io.BytesIO(raw)) as z:
                    root = ET.fromstring(z.read(z.namelist()[0]))
                self._map = {}
                for el in root.iter("list"):
                    sc = (el.findtext("stock_code") or "").strip()
                    if sc:
                        self._map[sc] = el.findtext("corp_code")
                self.cache.write_text(json.dumps(self._map), encoding="utf-8")
        return self._map.get(stock_code)

    def profile(self, stock_code: str) -> dict:
        """{sector, homepage, major_holder_pct, disclosures[], financials[]}"""
        out: dict = {"disclosures": [], "financials": []}
        if not self.enabled:
            return out
        try:
            cc = self.corp_code(stock_code)
        except Exception as e:
            print(f"[warn] DART 고유번호 조회 실패: {e}")
            return out
        if not cc:
            return out
        try:
            c = self._get("company.json", corp_code=cc) or {}
            out["sector"] = c.get("induty_code")
            out["homepage"] = c.get("hm_url")
        except Exception:
            pass
        year = dt.date.today().year
        for y in (year - 1, year - 2):
            try:
                h = self._get("hyslrSttus.json", corp_code=cc, bsns_year=str(y), reprt_code="11011")
            except Exception:
                h = None
            if h and h.get("list"):
                rows = h["list"]
                tot = [r for r in rows if (r.get("nm") or "").strip() == "계"]
                pct = [float(r.get("trmend_posesn_stock_qota_rt") or 0) for r in (tot or rows)]
                out["major_holder_pct"] = round(pct[0] if tot else sum(pct), 2)
                break
        try:
            lst = self._get("list.json", corp_code=cc, page_count="15",
                            bgn_de=(dt.date.today() - dt.timedelta(days=90)).strftime("%Y%m%d"))
            for r in (lst or {}).get("list", []):
                out["disclosures"].append({"date": r["rcept_dt"], "title": r["report_nm"].strip(),
                                           "rcept_no": r["rcept_no"]})
        except Exception:
            pass
        for y in (year - 1, year - 2, year - 3):
            try:
                f = self._get("fnlttSinglAcnt.json", corp_code=cc, bsns_year=str(y), reprt_code="11011")
            except Exception:
                f = None
            if not f:
                continue
            rows = f.get("list", [])
            fs = "CFS" if any(r.get("fs_div") == "CFS" for r in rows) else "OFS"
            vals = {}
            for r in rows:
                if r.get("fs_div") != fs:
                    continue
                nm = r.get("account_nm", "")
                if nm in ("매출액", "수익(매출액)", "영업수익"):
                    vals.setdefault("revenue", _int(r.get("thstrm_amount")))
                elif nm == "영업이익":
                    vals.setdefault("op_income", _int(r.get("thstrm_amount")))
                elif nm in ("당기순이익", "당기순이익(손실)"):
                    vals.setdefault("net_income", _int(r.get("thstrm_amount")))
            if vals:
                out["financials"].append({"year": y, **vals})
        return out


class NoDart:
    enabled = False

    def profile(self, code):
        return {"disclosures": [], "financials": []}
