"""FastAPI 入口：电台档案 UTC 查档服务。

端点
----
* ``POST /channels``                接收/整份替换单频道日志（含 IANA 时区）
* ``GET  /channels/{id}``           读取一份已提交的完整档案（含版本号、当前对账单）
* ``GET  /channels/{id}/solve``     整档可解性与（按当前对账单过滤后的）完整排法数
* ``PUT  /channels/{id}/blackouts`` 登记/更正一条 UTC 停播段
* ``DELETE /channels/{id}/blackouts/{bid}`` 删除停播段
* ``PUT  /channels/{id}/reconciliation`` 登记/换单：提交核对窗口与在播总秒数
* ``GET  /channels/{id}/reconciliation``  读取当前对账单及其匹配排法数
* ``GET  /channels/{id}/query?utc_second=`` 查档：某 UTC 秒的播出节目与空档判定

每次查询在单快照事务内读取同一份已提交资料（节目、停播段、对账单、版本号）；
写入在单事务内按同一档案状态校验并提交，无解（含对账单对不上）或写入失败一律
回滚，旧档、旧单继续可查。
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

from .db import ReconciliationMismatchError, Repository, UnsolvableError
from .domain import (
    Reconciliation,
    effective_arrangement_count,
    is_solvable,
    query_second,
    recon_query_second,
)
from .schemas import (
    BlackoutIn,
    ChannelOut,
    CommitOut,
    LogUpload,
    QueryOut,
    ReconciliationBrief,
    ReconciliationIn,
    ReconciliationOut,
)

DB_PATH = os.environ.get("RADIO_DB_PATH", "/data/radio.db")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.repo = Repository(DB_PATH)
    yield


app = FastAPI(
    title="电台档案 UTC 查档服务",
    version="1.1.0",
    description=(
        "按 UTC 秒查询单频道电台节目。日志只记本地开播时刻，服务依据 IANA 时区"
        "换算 UTC；夏令时回拨导致本地时刻出现两次时，两种 UTC 时刻均纳入核对。"
        "值班员可提交对账单（窗口最长 1 小时）：只有存在对得上在播总秒数的完整"
        "排法时才登记/换单，此后排法数与按秒查档都只按这些排法计算。"
        "时段均为半开区间 [start, end)。"
    ),
    lifespan=lifespan,
)


def _repo() -> Repository:
    return app.state.repo


def _require_channel(channel_id: str):
    model, _, recon = _repo().get_snapshot(channel_id)
    if model is None:
        raise HTTPException(status_code=404, detail=f"频道 {channel_id} 不存在")
    return model, recon


def _require_snapshot(channel_id: str):
    """返回同一份已提交资料的模型、版本号与对账单（单快照事务）。"""
    model, version, recon = _repo().get_snapshot(channel_id)
    if model is None:
        raise HTTPException(status_code=404, detail=f"频道 {channel_id} 不存在")
    return model, version, recon


def _recon_brief(recon: Reconciliation | None, model, count: int) -> ReconciliationBrief | None:
    if recon is None:
        return None
    return ReconciliationBrief(
        start_utc=recon.start,
        end_utc=recon.end,
        on_air_seconds=recon.on_air_seconds,
        matched_arrangement_count=count,
    )


# --------------------------------------------------------------------------- #
# 日志写入
# --------------------------------------------------------------------------- #
@app.post("/channels", response_model=CommitOut, status_code=201, tags=["日志"])
def upload_log(payload: LogUpload) -> CommitOut:
    """接收（或整份替换）单频道日志。

    新日志与既有停播段冲突导致整档无解时返回 409，旧档案保留不动。
    """
    rows = [
        {
            "seq": p.seq,
            "local_start": p.local_start.isoformat(),
            "duration": p.duration,
        }
        for p in payload.programs
    ]
    try:
        version = _repo().replace_log(payload.channel_id, payload.timezone, rows)
    except ReconciliationMismatchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except UnsolvableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:  # 春令跳空等
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    model, recon = _require_channel(payload.channel_id)
    count = effective_arrangement_count(model, recon)
    return CommitOut(
        channel_id=payload.channel_id,
        version=version,
        solvable=is_solvable(model),
        arrangement_count=count,
    )


# --------------------------------------------------------------------------- #
# 停播段
# --------------------------------------------------------------------------- #
@app.put(
    "/channels/{channel_id}/blackouts",
    response_model=CommitOut,
    tags=["停播段"],
)
def upsert_blackout(channel_id: str, payload: BlackoutIn) -> CommitOut:
    """登记或更正一条 UTC 停播段（按 blackout_id 覆盖）。

    若该停播段会使整档不存在任何完整排法，返回 409 并保留旧档。
    """
    try:
        version = _repo().upsert_blackout(
            channel_id,
            payload.blackout_id,
            payload.start_utc,
            payload.end_utc,
            payload.note,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"频道 {channel_id} 不存在") from exc
    except (UnsolvableError, ReconciliationMismatchError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    model, recon = _require_channel(channel_id)
    return CommitOut(
        channel_id=channel_id,
        version=version,
        solvable=True,
        arrangement_count=effective_arrangement_count(model, recon),
    )


@app.delete(
    "/channels/{channel_id}/blackouts/{blackout_id}",
    response_model=CommitOut,
    tags=["停播段"],
)
def delete_blackout(channel_id: str, blackout_id: str) -> CommitOut:
    try:
        version = _repo().delete_blackout(channel_id, blackout_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail=f"频道或停播段不存在: {channel_id}/{blackout_id}"
        ) from exc
    except (UnsolvableError, ReconciliationMismatchError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    model, recon = _require_channel(channel_id)
    return CommitOut(
        channel_id=channel_id,
        version=version,
        solvable=is_solvable(model),
        arrangement_count=effective_arrangement_count(model, recon),
    )


# --------------------------------------------------------------------------- #
# 对账单（每频道至多一张；登记/换单/读取）
# --------------------------------------------------------------------------- #
@app.put(
    "/channels/{channel_id}/reconciliation",
    response_model=ReconciliationOut,
    tags=["对账"],
)
def upsert_reconciliation(channel_id: str, payload: ReconciliationIn) -> ReconciliationOut:
    """登记对账单，或用新单替换当前单（换单）。

    只有存在至少一种完整排法，使其窗口内在播总秒数恰等于提交值时才落档；
    否则返回 409，旧单、旧档案原封不动、版本号不增加。换单用新单重新核对，
    原先被旧单排除的候选可重新成立。
    """
    try:
        version = _repo().upsert_reconciliation(
            channel_id,
            payload.start_utc,
            payload.end_utc,
            payload.on_air_seconds,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"频道 {channel_id} 不存在") from exc
    except (UnsolvableError, ReconciliationMismatchError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    model, recon = _require_channel(channel_id)
    assert recon is not None
    matched = effective_arrangement_count(model, recon)
    return ReconciliationOut(
        channel_id=channel_id,
        version=version,
        start_utc=recon.start,
        end_utc=recon.end,
        on_air_seconds=recon.on_air_seconds,
        matched_arrangement_count=matched,
    )


@app.get(
    "/channels/{channel_id}/reconciliation",
    response_model=ReconciliationOut,
    tags=["对账"],
)
def get_reconciliation(channel_id: str) -> ReconciliationOut:
    # 单快照事务同时取模型、版本号与对账单，保证匹配排法数与对账单同版本。
    model, version, recon = _repo().get_snapshot(channel_id)
    if model is None:
        raise HTTPException(status_code=404, detail=f"频道 {channel_id} 不存在")
    if recon is None:
        raise HTTPException(
            status_code=404, detail=f"频道 {channel_id} 尚未登记对账单"
        )
    matched = effective_arrangement_count(model, recon)
    return ReconciliationOut(
        channel_id=channel_id,
        version=version,
        start_utc=recon.start,
        end_utc=recon.end,
        on_air_seconds=recon.on_air_seconds,
        matched_arrangement_count=matched,
    )


# --------------------------------------------------------------------------- #
# 查档
# --------------------------------------------------------------------------- #
@app.get("/channels/{channel_id}", response_model=ChannelOut, tags=["查档"])
def get_channel(channel_id: str) -> ChannelOut:
    # 单快照事务内取整档资料与对账单；匹配排法数用同一快照的模型计算。
    data = _repo().get_channel(channel_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"频道 {channel_id} 不存在")
    model, _version, recon = _repo().get_snapshot(channel_id)
    count = effective_arrangement_count(model, recon)
    if data["reconciliation"] is not None:
        data["reconciliation"]["matched_arrangement_count"] = count
    return ChannelOut(**data)


@app.get("/channels/{channel_id}/solve", tags=["查档"])
def solve(channel_id: str) -> dict:
    model, version, recon = _require_snapshot(channel_id)
    count = effective_arrangement_count(model, recon)
    return {
        "channel_id": channel_id,
        "version": version,
        "solvable": is_solvable(model),
        "arrangement_count": count,
        "reconciliation": (
            _recon_brief(recon, model, count).model_dump() if recon is not None else None
        ),
    }


@app.get("/channels/{channel_id}/query", response_model=QueryOut, tags=["查档"])
def query(
    channel_id: str,
    utc_second: int = Query(..., description="要查的 UTC 纪元秒"),
) -> QueryOut:
    """列出在（按当前对账单过滤后的）完整排法中播到该秒的节目流水号，并判定空档。

    未登记对账单时按全部完整排法计算；已登记时只用对得上对账单总秒数的完整
    排法，排法数、在播节目与空档均取自同一已提交版本。
    """
    model, version, recon = _require_snapshot(channel_id)
    if not is_solvable(model):
        # 正常情况下无解资料无法提交；这里防御性处理，保证语义完整。
        raise HTTPException(status_code=409, detail="该档案当前不存在完整排法，无法查档")
    if recon is None:
        result = query_second(model, utc_second)
    else:
        result = recon_query_second(model, recon, utc_second)
        # 有对账单但没有任何排法对得上时（正常不会落档），防御性报错。
        if result["arrangement_count"] == 0:
            raise HTTPException(
                status_code=409, detail="当前对账单没有可对上的完整排法，无法查档"
            )
    return QueryOut(
        channel_id=channel_id,
        utc_second=utc_second,
        version=version,
        on_air=result["on_air"],
        may_be_gap=result["may_be_gap"],
        in_blackout=result["in_blackout"],
        arrangement_count=result["arrangement_count"],
        reconciliation=(
            _recon_brief(recon, model, result["arrangement_count"])
            if recon is not None
            else None
        ),
    )


@app.get("/", tags=["meta"])
def root() -> dict:
    return {"service": "radio-archive", "docs": "/docs"}
