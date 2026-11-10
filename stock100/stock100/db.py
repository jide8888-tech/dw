from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS stocks (
    code TEXT PRIMARY KEY, name TEXT, market TEXT, sector TEXT,
    marcap INTEGER, major_holder_pct REAL, homepage TEXT,
    first_seen TEXT, last_seen TEXT, updated TEXT
);
CREATE TABLE IF NOT EXISTS events (            -- 상한가 / 천만주 기록 (복습)
    date TEXT, code TEXT, type TEXT, close REAL, change_pct REAL,
    volume INTEGER, value INTEGER, marcap INTEGER, reason TEXT, keywords TEXT,
    focus TEXT, PRIMARY KEY (date, code, type)
);
CREATE TABLE IF NOT EXISTS news (
    code TEXT, date TEXT, title TEXT, link TEXT, source TEXT,
    PRIMARY KEY (code, link)
);
CREATE TABLE IF NOT EXISTS keywords (
    code TEXT, keyword TEXT, count INTEGER, PRIMARY KEY (code, keyword)
);
CREATE TABLE IF NOT EXISTS relations (          -- 기사에 함께 등장한 종목 (예: 에스엠랩 ↔ DSC인베스트먼트)
    code_a TEXT, code_b TEXT, count INTEGER, PRIMARY KEY (code_a, code_b)
);
CREATE TABLE IF NOT EXISTS disclosures (
    code TEXT, date TEXT, title TEXT, rcept_no TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS financials (
    code TEXT, year INTEGER, revenue INTEGER, op_income INTEGER, net_income INTEGER,
    PRIMARY KEY (code, year)
);
CREATE TABLE IF NOT EXISTS headlines (          -- 새벽 예습에서 읽은 뉴스
    date TEXT, title TEXT, link TEXT, source TEXT, PRIMARY KEY (date, link)
);
CREATE TABLE IF NOT EXISTS signals (            -- 지식 → 시그널 → 추정
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT, code TEXT, score REAL, basis TEXT, headline TEXT, link TEXT,
    estimate TEXT, base_close REAL, ret_json TEXT, max_ret REAL,
    hit INTEGER, status TEXT DEFAULT 'open',
    UNIQUE (date, code)
);
CREATE TABLE IF NOT EXISTS theme_daily (        -- 날짜별 테마 구성 (테마 형성·확산 추적)
    date TEXT, theme TEXT, code TEXT, change_pct REAL, value INTEGER, is_leader INTEGER,
    PRIMARY KEY (date, theme, code)
);
CREATE TABLE IF NOT EXISTS value_rank_daily (   -- 날짜별 거래대금 상위 100 (5분 감시 대상 선정용)
    date TEXT, code TEXT, name TEXT, rank INTEGER, value REAL, change_pct REAL, close REAL,
    PRIMARY KEY (date, code)
);
CREATE TABLE IF NOT EXISTS watch_ticks (        -- 5분 감시 스냅샷
    date TEXT, time TEXT, code TEXT, name TEXT, price REAL, pct REAL, value REAL, strength REAL,
    wl_rank INTEGER, mk_rank INTEGER, d_value REAL, d_pct REAL, d_strength REAL, per_won REAL,
    rank_prev INTEGER, rank_up INTEGER, red INTEGER, buy INTEGER,
    PRIMARY KEY (date, time, code)
);
CREATE TABLE IF NOT EXISTS watch_signals (
    date TEXT, time TEXT, code TEXT, name TEXT, kind TEXT, price REAL, pct REAL, d_value REAL, d_pct REAL,
    rank_prev INTEGER, rank_now INTEGER, qty INTEGER, amount REAL, cash_left REAL,
    PRIMARY KEY (date, time, code, kind)
);
CREATE TABLE IF NOT EXISTS watch_universe (     -- 그룹별 그날 감시 종목
    date TEXT, grp TEXT, code TEXT, name TEXT, rank INTEGER, reason TEXT, picked_at TEXT,
    PRIMARY KEY (date, grp, code)
);
CREATE TABLE IF NOT EXISTS watch_ticks2 (
    date TEXT, time TEXT, grp TEXT, code TEXT, name TEXT, price REAL, pct REAL, value REAL, strength REAL,
    wl_rank INTEGER, mk_rank INTEGER, d_value REAL, d_pct REAL, d_strength REAL, per_won REAL,
    rank_prev INTEGER, rank_up INTEGER, red INTEGER, buy INTEGER,
    PRIMARY KEY (date, time, grp, code)
);
CREATE TABLE IF NOT EXISTS watch_signals2 (
    date TEXT, time TEXT, grp TEXT, code TEXT, name TEXT, kind TEXT, price REAL, pct REAL, d_value REAL,
    d_pct REAL, rank_prev INTEGER, rank_now INTEGER, qty INTEGER, amount REAL, cash_left REAL,
    PRIMARY KEY (date, time, grp, code, kind)
);
CREATE TABLE IF NOT EXISTS watch_outcomes (     -- 매수 신호 이후 성과 (그룹 비교용)
    date TEXT, grp TEXT, time TEXT, code TEXT, name TEXT, entry REAL, qty INTEGER,
    r30 REAL, r60 REAL, rclose REAL, rmax REAL,
    PRIMARY KEY (date, grp, time, code)
);
CREATE TABLE IF NOT EXISTS paper_trades (       -- Day 71~ 소액 매매 (모의)
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT, code TEXT, stage TEXT, qty INTEGER, entry REAL, exit REAL,
    exit_reason TEXT, pnl REAL, pnl_pct REAL, signal_id INTEGER,
    UNIQUE (date, code)
);
"""


def connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con
