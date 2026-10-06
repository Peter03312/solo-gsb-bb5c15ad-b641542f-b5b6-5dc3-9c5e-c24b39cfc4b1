from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SegmentType(str, Enum):
    DRIVING = "DRIVING"
    OTHER_DUTY = "OTHER_DUTY"
    REST = "REST"


def _aware_utc(v: datetime) -> datetime:
    if v.tzinfo is None or v.tzinfo.utcoffset(v) is None:
        raise ValueError("时间必须携带时区（建议使用 UTC，如 2026-10-05T08:00:00Z）")
    return v.astimezone(timezone.utc)


class DriverCreate(BaseModel):
    driver_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)


class DriverOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    driver_id: str
    name: str
    created_at: datetime


class SegmentCreate(BaseModel):
    segment_id: str = Field(min_length=1, max_length=64)
    driver_id: str = Field(min_length=1, max_length=64)
    start_utc: datetime
    end_utc: datetime
    type: SegmentType

    @field_validator("start_utc", "end_utc")
    @classmethod
    def _tz(cls, v: datetime) -> datetime:
        return _aware_utc(v)

    @model_validator(mode="after")
    def _positive_duration(self):
        if self.end_utc <= self.start_utc:
            raise ValueError("end_utc 必须晚于 start_utc（拒绝非正时长片段）")
        return self


class SegmentPatch(BaseModel):
    """按预期修订号更正片段；字段缺省表示不修改。"""

    expected_revision: int = Field(ge=1)
    driver_id: Optional[str] = Field(default=None, min_length=1, max_length=64)
    start_utc: Optional[datetime] = None
    end_utc: Optional[datetime] = None
    type: Optional[SegmentType] = None

    @field_validator("start_utc", "end_utc")
    @classmethod
    def _tz(cls, v: Optional[datetime]) -> Optional[datetime]:
        return _aware_utc(v) if v is not None else v


class SegmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    segment_id: str
    driver_id: str
    start_utc: datetime
    end_utc: datetime
    type: str
    revision: int
    created_at: datetime
    updated_at: datetime


class ViolationOut(BaseModel):
    rule: str
    first_violation_at: datetime
    segment_ids: list[str]
    detail: dict[str, Any]


class ComplianceOut(BaseModel):
    driver_id: str
    evaluated_at: datetime
    compliant: bool
    violations: list[ViolationOut]


class ComplianceResult(BaseModel):
    compliant: bool
    violations: list[ViolationOut]


class ReassignmentItem(BaseModel):
    """批次内单条片段的调整指令：目标司机、起止时间与类型均为必填（调整后的完整终态）。"""

    segment_id: str = Field(min_length=1, max_length=64)
    expected_revision: int = Field(ge=1)
    driver_id: str = Field(min_length=1, max_length=64)
    start_utc: datetime
    end_utc: datetime
    type: SegmentType

    @field_validator("start_utc", "end_utc")
    @classmethod
    def _tz(cls, v: datetime) -> datetime:
        return _aware_utc(v)

    @model_validator(mode="after")
    def _positive_duration(self):
        if self.end_utc <= self.start_utc:
            raise ValueError("end_utc 必须晚于 start_utc（拒绝非正时长片段）")
        return self


class ReassignmentRequest(BaseModel):
    """一批已有片段的跨司机调整：作为一次操作提交，整批原子生效或整批拒绝。"""

    batch_id: str = Field(min_length=1, max_length=64)
    items: list[ReassignmentItem] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_segments(self):
        ids = [it.segment_id for it in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("同批次内不得重复指定同一片段")
        return self


class SegmentRevisionBump(BaseModel):
    segment_id: str
    previous_revision: int
    revision: int


class DriverComplianceDelta(BaseModel):
    driver_id: str
    before: ComplianceResult
    after: ComplianceResult


class ReassignmentResponse(BaseModel):
    batch_id: str
    applied_at: datetime
    segment_changes: list[SegmentRevisionBump]
    drivers: list[DriverComplianceDelta]  # 受影响司机（源 ∪ 目标）调整前后的实时合规结论


class InputRevision(BaseModel):
    segment_id: str
    revision: int


class AuditSnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    audit_id: int
    driver_id: str
    fingerprint: str
    input_revisions: list[InputRevision]
    result: dict[str, Any]
    published_at: datetime


class PublishResponse(BaseModel):
    created_new: bool  # False 表示同修订的发布已存在，返回既有版本
    snapshot: AuditSnapshotOut


class SegmentRevisionChange(BaseModel):
    segment_id: str
    snapshot_revision: int
    current_revision: int


class InputChanges(BaseModel):
    added: list[str]
    removed: list[str]
    revision_changed: list[SegmentRevisionChange]


class CompareOut(BaseModel):
    audit_id: int
    driver_id: str
    published_at: datetime
    snapshot_result: dict[str, Any]
    current_result: dict[str, Any]
    results_differ: bool
    input_changes: InputChanges
