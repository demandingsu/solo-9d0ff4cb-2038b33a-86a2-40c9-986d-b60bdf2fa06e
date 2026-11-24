"""对账 API 端到端测试：登记/换单/拒绝、按单过滤的查档、写事务原子退回。"""

from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    tmpdir = tempfile.mkdtemp()
    monkeypatch.setenv("RADIO_DB_PATH", os.path.join(tmpdir, "test.db"))
    import importlib

    import app.main as main

    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c


# 回拨档案：P1 本地 01:30 -> 05:30Z(1730611800)/06:30Z(1730615400)，900 秒
#           P2 本地 01:45 -> 05:45Z(1730612700)/06:45Z(1730616300)，900 秒
# 完整排法 3 种：早早(EE)、早晚(EL)、晚晚(LL)。
FALLBACK_LOG = {
    "channel_id": "ny-fm",
    "timezone": "America/New_York",
    "programs": [
        {"seq": 1, "local_start": "2024-11-03T01:30:00", "duration": 900},
        {"seq": 2, "local_start": "2024-11-03T01:45:00", "duration": 900},
    ],
}
BASE = 1730611800  # 05:30Z


def _post_log(client, payload=FALLBACK_LOG):
    r = client.post("/channels", json=payload)
    assert r.status_code == 201, r.text
    return r


# --------------------------------------------------------------------------- #
# 窗口裁切与匹配计数
# --------------------------------------------------------------------------- #
def test_recon_window_clips_seconds_and_counts(client):
    _post_log(client)
    # 窗口 [05:30Z, 06:30Z)（3600 秒）。三种合法排法窗口贡献各不相同：
    #   EE(早早): P1 早 + P2 早 = 1800
    #   EL(早晚): 仅 P1 早落在窗内 = 900（P2 晚 06:45 已在窗外）
    #   LL(晚晚): P1 晚 06:30 开播，窗口终点不含 -> 0
    # 因此监听总秒数 1800 只对应 EE 一种排法。
    r = client.put(
        "/channels/ny-fm/reconciliation",
        json={"start_utc": BASE, "end_utc": BASE + 3600, "on_air_seconds": 1800},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 2
    assert body["matched_arrangement_count"] == 1

    # 监听总秒数 500：落在窗口上下限 [0,3600] 内但三种排法都取不到 -> 409，
    # 换单失败，前一张（target=1800）对账单保留。
    r = client.put(
        "/channels/ny-fm/reconciliation",
        json={"start_utc": BASE, "end_utc": BASE + 3600, "on_air_seconds": 500},
    )
    assert r.status_code == 409
    kept = client.get("/channels/ny-fm/reconciliation").json()
    assert kept["on_air_seconds"] == 1800
    assert kept["matched_arrangement_count"] == 1


def test_recon_clips_only_seconds_inside_window(client):
    _post_log(client)
    # 区分窗口 [05:30Z, 06:00Z)（1800 秒），三种合法排法贡献各不相同：
    #   EE: P1 早[05:30,05:45) 900 + P2 早[05:45,06:00) 900 = 1800
    #   EL: 仅 P1 早 900（P2 晚 06:45 才开）= 900
    #   LL: 窗口内无节目（P1 晚 06:30 才开）= 0
    ws, we = BASE, BASE + 1800
    for target, expected in ((1800, 1), (900, 1), (0, 1)):
        r = client.put(
            "/channels/ny-fm/reconciliation",
            json={"start_utc": ws, "end_utc": we, "on_air_seconds": target},
        )
        assert r.status_code == 200, (target, r.text)
        assert r.json()["matched_arrangement_count"] == expected
    # 500 落在窗口上下限 [0,1800] 内却取不到 -> 409，旧单（target=0）保留
    r = client.put(
        "/channels/ny-fm/reconciliation",
        json={"start_utc": ws, "end_utc": we, "on_air_seconds": 500},
    )
    assert r.status_code == 409
    assert client.get("/channels/ny-fm/reconciliation").json()["on_air_seconds"] == 0


# --------------------------------------------------------------------------- #
# 需求点名：总数落在可达上下限内却无解
# --------------------------------------------------------------------------- #
def test_total_within_bounds_but_unreachable_rejected(client):
    # 单节目 900 秒，UTC 频道，开播 00:16:40 = 纪元秒 1000，节目 [1000,1900)。
    # 窗口 [0,3600) 内该节目贡献恒为 900，可达集合 {900}；窗口上下限为
    # [0,3600]，总秒数 500 落在区间内却没有任何排法能对上。
    payload = {
        "channel_id": "c-gap",
        "timezone": "UTC",
        "programs": [
            {"seq": 1, "local_start": "1970-01-01T00:16:40", "duration": 900},
        ],
    }
    client.post("/channels", json=payload)
    # 1970-01-01T00:16:40 UTC = 1000 秒；节目 [1000,1900)，窗口贡献恒为 900。
    # 可达集合 {900}；窗口长度 3600，上下限为 [0,3600]，500 落在区间内却无解。
    r = client.put(
        "/channels/c-gap/reconciliation",
        json={"start_utc": 0, "end_utc": 3600, "on_air_seconds": 500},
    )
    assert r.status_code == 409
    # 旧档未受影响、版本号不变、仍可查
    detail = client.get("/channels/c-gap").json()
    assert detail["version"] == 1 and detail["reconciliation"] is None
    q = client.get("/channels/c-gap/query", params={"utc_second": 1000}).json()
    assert q["on_air"] == [1] and q["arrangement_count"] == 1
    # 900 可登记
    ok = client.put(
        "/channels/c-gap/reconciliation",
        json={"start_utc": 0, "end_utc": 3600, "on_air_seconds": 900},
    )
    assert ok.status_code == 200 and ok.json()["matched_arrangement_count"] == 1


# --------------------------------------------------------------------------- #
# 需求点名：换单后原来被排除的候选重新成立
# --------------------------------------------------------------------------- #
def test_replace_statement_restores_excluded_candidates(client):
    _post_log(client)
    # 区分窗口 [05:30,06:00)：EE->1800、EL->900、LL->0，各对应唯一排法。
    ws, we = BASE, BASE + 1800
    r = client.put(
        "/channels/ny-fm/reconciliation",
        json={"start_utc": ws, "end_utc": we, "on_air_seconds": 1800},
    )
    assert r.status_code == 200 and r.json()["matched_arrangement_count"] == 1
    # 唯一匹配排法 EE 里，06:30Z（晚候选开播秒）无节目 -> 空档、on_air 空
    q = client.get("/channels/ny-fm/query", params={"utc_second": BASE + 3600}).json()
    assert q["arrangement_count"] == 1 and q["on_air"] == [] and q["may_be_gap"] is True

    # 换单为总秒数 0：只匹配 LL，晚候选重新成立，匹配数仍为 1 但成员已换；
    # 06:30Z 在 LL 中 P1 播出。
    r = client.put(
        "/channels/ny-fm/reconciliation",
        json={"start_utc": ws, "end_utc": we, "on_air_seconds": 0},
    )
    assert r.status_code == 200
    assert r.json()["matched_arrangement_count"] == 1
    q2 = client.get("/channels/ny-fm/query", params={"utc_second": BASE + 3600}).json()
    assert q2["arrangement_count"] == 1
    assert q2["on_air"] == [1]  # 晚候选 P1 恢复
    assert q2["may_be_gap"] is False
    # 对账单回显已是新单
    got = client.get("/channels/ny-fm/reconciliation").json()
    assert got["on_air_seconds"] == 0 and got["matched_arrangement_count"] == 1


def test_failed_replacement_keeps_old_statement(client):
    _post_log(client)
    ws, we = BASE, BASE + 3600
    client.put(
        "/channels/ny-fm/reconciliation",
        json={"start_utc": ws, "end_utc": we, "on_air_seconds": 1800},
    )
    before = client.get("/channels/ny-fm/reconciliation").json()
    # 换一个无解的总秒数（可达只有 1800）：409，旧单保留、版本不变
    r = client.put(
        "/channels/ny-fm/reconciliation",
        json={"start_utc": ws, "end_utc": we, "on_air_seconds": 1801},
    )
    assert r.status_code == 409
    after = client.get("/channels/ny-fm/reconciliation").json()
    assert after == before
    assert client.get("/channels/ny-fm").json()["version"] == 2


# --------------------------------------------------------------------------- #
# 写入（替换日志 / 更正停播段）也要核对当前单，失败整次退回
# --------------------------------------------------------------------------- #
def test_blackout_correction_must_still_satisfy_current_statement(client):
    _post_log(client)
    # 区分窗口 [05:30,06:00) 取总秒数 1800：只有 EE 一种排法对得上。
    ws, we = BASE, BASE + 1800
    r = client.put(
        "/channels/ny-fm/reconciliation",
        json={"start_utc": ws, "end_utc": we, "on_air_seconds": 1800},
    )
    assert r.status_code == 200 and r.json()["matched_arrangement_count"] == 1
    # 停播段封掉早候选 -> EE 不合法，当前单（只认 EE）无解 -> 409，整次退回
    r = client.put(
        "/channels/ny-fm/blackouts",
        json={"blackout_id": "bo-1", "start_utc": BASE, "end_utc": BASE + 1800},
    )
    assert r.status_code == 409
    detail = client.get("/channels/ny-fm").json()
    assert detail["version"] == 2 and len(detail["blackouts"]) == 0
    # 当前单仍在、仍可按单查档
    assert client.get("/channels/ny-fm/reconciliation").status_code == 200


def test_replace_log_must_still_satisfy_current_statement(client):
    _post_log(client)
    # 区分窗口取总秒数 1800：仅 EE 匹配。
    client.put(
        "/channels/ny-fm/reconciliation",
        json={"start_utc": BASE, "end_utc": BASE + 1800, "on_air_seconds": 1800},
    )
    # 换成单节目 30 秒：窗口 [05:30,06:00) 内它的两个候选分别贡献 30 / 0，
    # 都不等于 1800 -> 当前单对不上，整份替换被退回。
    conflicting = {
        "channel_id": "ny-fm",
        "timezone": "America/New_York",
        "programs": [
            {"seq": 1, "local_start": "2024-11-03T01:30:00", "duration": 30},
        ],
    }
    r = client.post("/channels", json=conflicting)
    assert r.status_code == 409
    detail = client.get("/channels/ny-fm").json()
    assert len(detail["programs"]) == 2  # 旧日志保留
    assert detail["reconciliation"] is not None  # 对账单保留


# --------------------------------------------------------------------------- #
# 查询从同一已提交版本取节目、空档、排法数三项
# --------------------------------------------------------------------------- #
def test_query_three_fields_share_one_version(client):
    _post_log(client)
    client.put(
        "/channels/ny-fm/reconciliation",
        json={"start_utc": BASE, "end_utc": BASE + 1800, "on_air_seconds": 1800},
    )
    q = client.get("/channels/ny-fm/query", params={"utc_second": BASE}).json()
    # 三项资料与对账单回显同版本（version=2），排法数已按单过滤（仅 EE）
    assert q["version"] == 2
    assert q["arrangement_count"] == 1
    assert q["reconciliation"] == {
        "start_utc": BASE,
        "end_utc": BASE + 1800,
        "on_air_seconds": 1800,
        "matched_arrangement_count": 1,
    }
    # /solve 与 /channels/{id} 也报同一个过滤后的排法数
    assert client.get("/channels/ny-fm/solve").json()["arrangement_count"] == 1
    assert (
        client.get("/channels/ny-fm").json()["reconciliation"]["matched_arrangement_count"]
        == 1
    )


# --------------------------------------------------------------------------- #
# 输入校验
# --------------------------------------------------------------------------- #
def test_recon_validation(client):
    _post_log(client)
    base_url = "/channels/ny-fm/reconciliation"
    # 窗口超过 1 小时
    r = client.put(
        base_url,
        json={"start_utc": 0, "end_utc": 3601, "on_air_seconds": 10},
    )
    assert r.status_code == 422
    # 倒置窗口
    r = client.put(
        base_url, json={"start_utc": 100, "end_utc": 100, "on_air_seconds": 0}
    )
    assert r.status_code == 422
    # 总秒数超过窗口长度
    r = client.put(
        base_url, json={"start_utc": 0, "end_utc": 100, "on_air_seconds": 101}
    )
    assert r.status_code == 422
    # 负的总秒数
    r = client.put(
        base_url, json={"start_utc": 0, "end_utc": 100, "on_air_seconds": -1}
    )
    assert r.status_code == 422
    # 频道不存在
    r = client.put(
        "/channels/nope/reconciliation",
        json={"start_utc": 0, "end_utc": 100, "on_air_seconds": 0},
    )
    assert r.status_code == 404


def test_recon_404_when_missing(client):
    _post_log(client)
    assert client.get("/channels/ny-fm/reconciliation").status_code == 404
    assert client.get("/channels/nope/reconciliation").status_code == 404


def test_one_statement_per_channel_upsert(client):
    _post_log(client)
    client.put(
        "/channels/ny-fm/reconciliation",
        json={"start_utc": BASE, "end_utc": BASE + 3600, "on_air_seconds": 1800},
    )
    # 再 PUT 是换单而非新增：频道始终只有一张，版本递增
    r = client.put(
        "/channels/ny-fm/reconciliation",
        json={"start_utc": BASE, "end_utc": BASE + 1800, "on_air_seconds": 900},
    )
    assert r.status_code == 200 and r.json()["version"] == 3
    detail = client.get("/channels/ny-fm").json()
    assert detail["reconciliation"]["start_utc"] == BASE
    assert detail["reconciliation"]["end_utc"] == BASE + 1800
