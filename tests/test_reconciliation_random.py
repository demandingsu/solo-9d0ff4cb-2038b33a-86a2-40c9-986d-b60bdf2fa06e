"""随机化对抗：随机生成节目/候选/停播段/窗口，DP 与暴力枚举逐窗口逐秒比对。"""

from __future__ import annotations

import itertools
import random

import pytest

import app.domain as D
from app.domain import (
    ArrangementModel,
    Blackout,
    Program,
    Reconciliation,
    clipped_length,
    count_matching_arrangements,
    recon_query_second,
)


def _brute(model: ArrangementModel, recon: Reconciliation, t: int):
    matched = 0
    on_air: set[int] = set()
    gap = False
    ranges = [range(len(p.starts)) for p in model.programs]
    for combo in itertools.product(*ranges):
        intervals = []
        ok = True
        for i, j in enumerate(combo):
            s = model.programs[i].starts[j]
            e = s + model.programs[i].duration
            if any(D._overlaps(s, e, b.start, b.end) for b in model.blackouts):
                ok = False
                break
            intervals.append((s, e))
        if not ok:
            continue
        if any(intervals[k][1] > intervals[k + 1][0] for k in range(len(intervals) - 1)):
            continue
        total = sum(clipped_length(s, e - s, recon.start, recon.end) for s, e in intervals)
        if total != recon.on_air_seconds:
            continue
        matched += 1
        covered = [
            model.programs[i].seq
            for i, (s, e) in enumerate(intervals)
            if s <= t < e
        ]
        if not covered:
            gap = True
        on_air.update(covered)
    return matched, sorted(on_air), gap


def _random_model(rng: random.Random) -> ArrangementModel:
    n = rng.randint(1, 5)
    programs = []
    cursor = rng.randint(0, 100)
    for i in range(n):
        duration = rng.randint(10, 200)
        # 1~2 个候选，候选间留回拨式间隔
        starts = [cursor]
        if rng.random() < 0.6:
            starts.append(cursor + rng.randint(0, 300))
        programs.append(Program(seq=i + 1, duration=duration, starts=tuple(sorted(set(starts)))))
        cursor += rng.randint(0, 250)
    blackouts = []
    for _ in range(rng.randint(0, 2)):
        bs = rng.randint(0, cursor)
        blackouts.append(Blackout(bs, bs + rng.randint(10, 150)))
    return ArrangementModel("R", tuple(programs), tuple(sorted(blackouts, key=lambda b: b.start)))


@pytest.mark.parametrize("seed", range(60))
def test_random_models_match_brute(seed):
    rng = random.Random(seed)
    model = _random_model(rng)
    lo = min(s for p in model.programs for s in p.starts) - 100
    hi = max(s + p.duration for p in model.programs for s in p.starts) + 100
    for _ in range(6):
        ws = rng.randint(lo, hi)
        length = rng.randint(1, 3600)
        we = ws + length
        # 收集可达总秒数
        achievable = set()
        ranges = [range(len(p.starts)) for p in model.programs]
        for combo in itertools.product(*ranges):
            intervals = []
            ok = True
            for i, j in enumerate(combo):
                s = model.programs[i].starts[j]
                e = s + model.programs[i].duration
                if any(D._overlaps(s, e, b.start, b.end) for b in model.blackouts):
                    ok = False
                    break
                intervals.append((s, e))
            if not ok:
                continue
            if any(intervals[k][1] > intervals[k + 1][0] for k in range(len(intervals) - 1)):
                continue
            achievable.add(
                sum(clipped_length(s, e - s, ws, we) for s, e in intervals)
            )
        targets = set(achievable)
        for v in list(achievable):
            for d in (1, -1, 3, 17):
                if 0 <= v + d <= length:
                    targets.add(v + d)
        for target in targets:
            recon = Reconciliation(ws, we, target)
            dp = count_matching_arrangements(model, recon)
            bc, _, _ = _brute(model, recon, ws)
            assert dp == bc
            assert (dp > 0) == (target in achievable)
            for t in range(ws - 20, we + 20, max(1, length // 5)):
                exp = _brute(model, recon, t)
                got = recon_query_second(model, recon, t)
                assert got["arrangement_count"] == exp[0]
                assert got["on_air"] == exp[1]
                assert got["may_be_gap"] == exp[2]
