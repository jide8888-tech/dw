"""100일 챌린지 단계표. mode=full 이면 100일 완료 상태로 모든 기능을 켠다."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .util import to_date

STAGES = [
    # (시작일, 끝일, 이름, 설명, 자동 수행 항목)
    (1, 1, "Day 1", "후회 없이 놉니다. 공부도 매매도 하지 않습니다.", set()),
    (2, 2, "Day 2", "더 놉니다. 주식 계좌를 0으로 만듭니다(물린 종목은 손절).", set()),
    (3, 3, "Day 3", "내 이력을 적고 주식과 연결되는 주력 분야를 정합니다(config focus_sectors).", set()),
    (4, 40, "Day 4~40 복습", "상한가·천만 주 종목을 찾아 이유와 차트를 정리합니다.", {"review"}),
    (41, 50, "Day 41~50 예습", "새벽 4시 뉴스를 읽고 반복되는 종목을 찾습니다.", {"review", "preview"}),
    (51, 70, "Day 51~70 추정·관찰", "복습 지식과 새벽 뉴스를 연결해 추정하고 결과를 기록합니다.",
     {"review", "preview", "track"}),
    (71, 80, "Day 71~80 1주 매매", "딱 1주만. 욕심이 드는 순간 매도.", {"review", "preview", "track", "paper"}),
    (81, 85, "Day 81~85 10주", "10주로 늘립니다.", {"review", "preview", "track", "paper"}),
    (86, 90, "Day 86~90 100만 원", "100만 원으로 늘립니다.", {"review", "preview", "track", "paper"}),
    (91, 99, "Day 91~99 500만 원", "500만 원으로 늘립니다.", {"review", "preview", "track", "paper"}),
    (100, 10**6, "Day 100+ 완성", "500만 원 시드로 지지 않는 매매. 지식→시그널→실행→주시→변화 감지→대응→반성→반복.",
     {"review", "preview", "track", "paper"}),
]


@dataclass
class Stage:
    day: int
    name: str
    desc: str
    features: set

    def position_size(self, price: float) -> int:
        """단계별 매수 수량 (책의 1주 → 10주 → 100만 원 → 500만 원)."""
        if "paper" not in self.features or price <= 0:
            return 0
        if self.day <= 80:
            return 1
        if self.day <= 85:
            return 10
        if self.day <= 90:
            return int(1_000_000 // price)
        return int(5_000_000 // price)


def current(cfg, on=None) -> Stage:
    on = to_date(on or dt.date.today())
    if cfg["mode"] == "full" or not cfg.get("challenge_start"):
        day = 100
    else:
        day = (on - to_date(cfg["challenge_start"])).days + 1
    day = max(day, 1)
    for a, b, name, desc, feats in STAGES:
        if a <= day <= b:
            return Stage(day, name, desc, set(feats))
    return Stage(day, "준비 전", "챌린지 시작 전입니다.", set())
