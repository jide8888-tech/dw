from __future__ import annotations

import copy
import datetime as dt
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULTS = {
    "mode": "full",
    "challenge_start": None,
    "data_dir": "data",
    "markets": ["KOSPI", "KOSDAQ"],
    "filters": {"upper_limit_pct": 29.5, "volume_min": 10_000_000, "value_min_eok": 100, "hot_pct_min": 10.0},
    "themes": {},
    "excel": True,
    "focus_sectors": {},
    "sources": {"prices": "auto", "news": ["google", "naver"]},
    "keys": {"dart": "", "naver_client_id": "", "naver_client_secret": "",
             "krx_id": "", "krx_pw": "", "kiwoom_appkey": "", "kiwoom_secretkey": ""},
    "kiwoom": {"mock": False},
    "watch": {"interval_min": 5, "start": "09:00", "end": "15:30",
              "lookback_days": 5, "top_n": 100, "min_pct": 20.0, "extra_codes": [],
              "value_delta_eok": 5.0, "pct_delta": 1.5,
              "rank_basis": "watchlist", "rank_up_min": 1, "buy_rule": "both",
              "cash": 10_000_000, "buy_ratio": 0.2, "beep": True},
    "morning": {"queries": ["특징주", "상한가", "공급계약", "수주"], "min_score": 2},
    "track": {"horizons": [1, 3, 5], "hit_pct": 3.0},
    "paper": {"enabled": True, "take_profit_pct": 3.0, "stop_loss_pct": 3.0, "max_per_day": 1},
}

ENV_KEYS = {
    "dart": "DART_API_KEY",
    "naver_client_id": "NAVER_CLIENT_ID",
    "naver_client_secret": "NAVER_CLIENT_SECRET",
    "krx_id": "KRX_ID",
    "krx_pw": "KRX_PW",
    "kiwoom_appkey": "KIWOOM_APPKEY",
    "kiwoom_secretkey": "KIWOOM_SECRETKEY",
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class Config:
    raw: dict
    root: Path
    today: dt.date = field(default_factory=dt.date.today)

    @classmethod
    def load(cls, path: str | os.PathLike | None = None, overrides: dict | None = None) -> "Config":
        raw: dict = {}
        root = Path.cwd()
        if path is None:
            for cand in ("config.yaml", "config.example.yaml"):
                if Path(cand).exists():
                    path = cand
                    break
        if path is not None:
            p = Path(path)
            root = p.resolve().parent
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        merged = _merge(DEFAULTS, raw)
        merged = _merge(merged, overrides or {})
        for k, env in ENV_KEYS.items():
            if os.getenv(env):
                merged["keys"][k] = os.environ[env]
        # pykrx 는 KRX_ID / KRX_PW 환경변수로 로그인한다
        if merged["keys"].get("krx_id") and not os.getenv("KRX_ID"):
            os.environ["KRX_ID"] = merged["keys"]["krx_id"]
            os.environ["KRX_PW"] = merged["keys"].get("krx_pw", "")
        return cls(raw=merged, root=root)

    def __getitem__(self, key):
        return self.raw[key]

    def get(self, key, default=None):
        return self.raw.get(key, default)

    @property
    def data_dir(self) -> Path:
        d = Path(self.raw["data_dir"])
        if not d.is_absolute():
            d = self.root / d
        d.mkdir(parents=True, exist_ok=True)
        return d

    def sub(self, name: str) -> Path:
        p = self.data_dir / name
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def key(self) -> dict:
        return self.raw["keys"]
