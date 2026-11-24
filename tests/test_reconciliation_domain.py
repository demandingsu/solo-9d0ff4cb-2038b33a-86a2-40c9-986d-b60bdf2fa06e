"""对账 DP 对暴力枚举的一致性测试。

暴力枚举每个节目的候选组合，筛出合法完整排法（不撞停播段、相邻不叠），
按半开窗口裁切求每个节目的窗口内在播秒数并求和；再与领域层的
``count_matching_arrangements`` / ``recon_query_second`` 逐窗口、逐秒比对。
"""

from __future__ import annotations

import itertools

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


def _valid_arrangements(model: ArrangementModel):
    """枚举全部合法完整排法：返回 [(候选下标组合, 各节目 (start,end) 列表), ...]。"""
    out = []
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
        out.append((combo, intervals))
    return out


def _brute_recon(model: ArrangementModel, recon: Reconciliation, t: int):
    """返回 (匹配总秒数的排法数, 这些排法里播到 t 的节目流水号, 是否可能空档)。"""
    matched = 0
    on_air: set[int] = set()
    gap = False
    for combo, intervals in _valid_arrangements(model):
        total = 0
        for i, (s, e) in enumerate(intervals):
            total += clipped_length(s, e - s, recon.start, recon.end)
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


MODELS = [
    # 回拨双候选：P1(05:30/06:30), P2(05:45/06:45)，各 900 秒
    ArrangementModel("X", (
        Program(1, 900, (1730611800, 1730615400)),
        Program(2, 900, (1730612700, 1730616300)),
    ), ()),
    # 三节目、各两候选、600 秒
    ArrangementModel("X", (
        Program(1, 600, (0, 3600)),
        Program(2, 600, (600, 4200)),
        Program(3, 600, (1200, 4800)),
    ), ()),
    # 含停播段
    ArrangementModel("X", (
        Program(1, 900, (100, 200)),
        Program(2, 900, (150, 300)),
        Program(3, 900, (260, 400)),
    ), (Blackout(180, 260),)),
    # 单节目单候选
    ArrangementModel("X", (Program(7, 600, (1000,)),), ()),
    # 多停播段
    ArrangementModel("X", (
        Program(1, 300, (0, 1000)),
        Program(2, 300, (200, 1100)),
    ), (Blackout(500, 900), Blackout(1050, 1400))),
]


def _all_window_totals(model: ArrangementModel, ws: int, we: int) -> set[int]:
    totals = set()
    for _combo, intervals in _valid_arrangements(model):
        total = sum(
            clipped_length(s, e - s, ws, we) for s, e in intervals
        )
        totals.add(total)
    return totals


@pytest.mark.parametrize("model", MODELS)
def test_recon_matches_brute_force(model):
    lo = min(s for p in model.programs for s in p.starts) - 500
    hi = max(s + p.duration for p in model.programs for s in p.starts) + 500
    # 取若干窗口：长度 3600、1000、以及跨在节目边界上的短窗
    windows = []
    for length in (3600, 1000, 100):
        for ws in range(lo, hi, max(1, length // 2)):
            windows.append((ws, ws + length))
    # 再补一个恰好框住全部候选的窗口
    windows.append((lo, lo + 3600))

    seen_targets = set()
    for ws, we in windows:
        achievable = _all_window_totals(model, ws, we)
        # 对每个可达总秒数，以及若干不可达但在上下限内的值都核对
        window_len = we - ws
        candidates = set(achievable)
        for v in achievable:
            for delta in (1, -1, 2, 50):
                nv = v + delta
                if 0 <= nv <= window_len:
                    candidates.add(nv)
        candidates.add(0)
        for target in sorted(candidates):
            seen_targets.add(target)
            recon = Reconciliation(start=ws, end=we, on_air_seconds=target)
            dp_count = count_matching_arrangements(model, recon)
            # 用一个 t 触发暴力（计数与 t 无关，循环内统一比对）
            brute_count, _, _ = _brute_recon(model, recon, lo)
            assert dp_count == brute_count, (model, ws, we, target)
            assert (dp_count > 0) == (target in achievable)

            # 逐秒比对 on_air / may_be_gap（取窗口内外若干代表秒）
            for t in range(ws - 50, we + 50, max(1, window_len // 7)):
                exp_count, exp_air, exp_gap = _brute_recon(model, recon, t)
                res = recon_query_second(model, recon, t)
                assert res["arrangement_count"] == exp_count
                assert res["on_air"] == exp_air
                assert res["may_be_gap"] == exp_gap
    assert seen_targets  # 防御：确实跑过若干目标值


def test_unreachable_total_inside_bounds_has_no_match():
    """可达上下限内却取不到的总秒数：匹配数必须为 0。"""
    # 单节目 900 秒，候选 0 / 1000；窗口 [0, 2000)。
    # 完整排法两种：贡献 900（取 0）或 900（取 1000）-> 可达集合 {900}，
    # 0 与 900 都在 [0,2000] 内，但 500 取不到。
    model = ArrangementModel("X", (Program(1, 900, (0, 1000)),), ())
    recon = Reconciliation(start=0, end=2000, on_air_seconds=500)
    assert count_matching_arrangements(model, recon) == 0
    res = recon_query_second(model, recon, 0)
    assert res["arrangement_count"] == 0 and res["on_air"] == []
    # 900 可达
    assert count_matching_arrangements(
        model, Reconciliation(0, 2000, 900)
    ) == 2


def test_clipped_length_half_open():
    # 节目 [100, 700)，窗口 [300, 500) -> 200 秒
    assert clipped_length(100, 600, 300, 500) == 200
    # 窗口完全包含节目
    assert clipped_length(100, 600, 0, 2000) == 600
    # 窗口与节目不相交
    assert clipped_length(100, 600, 700, 800) == 0
    assert clipped_length(100, 600, 800, 900) == 0
    # 端点相接（半开）不计秒
    assert clipped_length(100, 600, 700, 700) == 0
    # 仅左/右相交（半开端点相接不计，真正相交才计）
    assert clipped_length(100, 600, 0, 100) == 0
    assert clipped_length(100, 600, 700, 800) == 0
    assert clipped_length(100, 600, 0, 400) == 300
    assert clipped_length(100, 600, 400, 1000) == 300
    assert clipped_length(100, 600, 600, 700) == 100


def test_empty_programs_model():
    model = ArrangementModel("X", (), ())
    assert count_matching_arrangements(
        model, Reconciliation(0, 100, 0)
    ) == 1
    assert count_matching_arrangements(
        model, Reconciliation(0, 100, 1)
    ) == 0
