"""请求/响应模型与输入校验。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from .domain import load_zone


class ProgramIn(BaseModel):
    seq: int = Field(..., ge=1, description="流水号（正整数，全频道唯一、按升序排）")
    local_start: datetime = Field(
        ..., description="本地开播时刻（无时区墙钟时间，如 2024-11-03T01:30:00）"
    )
    duration: int = Field(..., gt=0, description="播出秒数（正整数）")

    @field_validator("local_start")
    @classmethod
    def _naive_wall_clock(cls, v: datetime) -> datetime:
        if v.tzinfo is not None:
            raise ValueError("local_start 必须是不带时区偏移的本地墙钟时刻")
        return v


class LogUpload(BaseModel):
    channel_id: str = Field(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    timezone: str = Field(..., description="IANA 时区名，如 America/New_York")
    programs: list[ProgramIn] = Field(..., min_length=1)

    @field_validator("timezone")
    @classmethod
    def _valid_zone(cls, v: str) -> str:
        load_zone(v)  # 非法时区直接 422
        return v.strip()

    @model_validator(mode="after")
    def _check_seq(self) -> "LogUpload":
        seqs = [p.seq for p in self.programs]
        if len(set(seqs)) != len(seqs):
            raise ValueError("流水号必须唯一，不能重复")
        if seqs != sorted(seqs):
            raise ValueError("整份日志必须按流水号升序排列")
        return self


class BlackoutIn(BaseModel):
    blackout_id: str = Field(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    start_utc: int = Field(..., description="停播段起点 UTC 纪元秒（含）")
    end_utc: int = Field(..., description="停播段终点 UTC 纪元秒（不含）")
    note: str = Field("", max_length=500)

    @model_validator(mode="after")
    def _half_open(self) -> "BlackoutIn":
        if self.end_utc <= self.start_utc:
            raise ValueError("停播段为半开区间 [start_utc, end_utc)，必须 end_utc > start_utc")
        return self


class ReconciliationIn(BaseModel):
    """值班员提交的对账单：核对窗口（UTC 秒，半开）与监听记录的在播总秒数。"""

    start_utc: int = Field(..., description="核对窗口起点 UTC 纪元秒（含）")
    end_utc: int = Field(..., description="核对窗口终点 UTC 纪元秒（不含），窗口最长 1 小时")
    on_air_seconds: int = Field(..., ge=0, description="监听记录给出的窗口内在播总秒数")

    @model_validator(mode="after")
    def _window_rules(self) -> "ReconciliationIn":
        if self.end_utc <= self.start_utc:
            raise ValueError("核对窗口为半开区间 [start_utc, end_utc)，必须 end_utc > start_utc")
        if self.end_utc - self.start_utc > 3600:
            raise ValueError("核对窗口最长为 1 小时（3600 秒）")
        if self.on_air_seconds > self.end_utc - self.start_utc:
            raise ValueError("在播总秒数不能超过核对窗口长度")
        return self


class ReconciliationBrief(BaseModel):
    """随档案/查档结果回显的当前对账单（每频道至多一张）。"""

    start_utc: int
    end_utc: int
    on_air_seconds: int
    matched_arrangement_count: int = Field(
        ..., description="对得上该总秒数的完整排法数（即当前有效排法数）"
    )


class ReconciliationOut(ReconciliationBrief):
    channel_id: str
    version: int


class ProgramOut(BaseModel):
    seq: int
    local_start: str
    duration: int
    utc_options: list[int]


class BlackoutOut(BaseModel):
    blackout_id: str
    start_utc: int
    end_utc: int
    note: str


class ChannelOut(BaseModel):
    channel_id: str
    timezone: str
    version: int
    created_at: str
    updated_at: str
    programs: list[ProgramOut]
    blackouts: list[BlackoutOut]
    reconciliation: ReconciliationBrief | None = None


class CommitOut(BaseModel):
    channel_id: str
    version: int
    solvable: bool
    arrangement_count: int


class QueryOut(BaseModel):
    channel_id: str
    utc_second: int
    version: int
    on_air: list[int]
    may_be_gap: bool
    in_blackout: bool
    arrangement_count: int
    reconciliation: ReconciliationBrief | None = None
