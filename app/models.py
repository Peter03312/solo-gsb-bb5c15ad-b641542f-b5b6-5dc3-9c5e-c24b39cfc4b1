from sqlalchemy import JSON, Column, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB

from .db import Base

# PostgreSQL 上使用 JSONB，其他数据库（如本地 sqlite 测试）退化为普通 JSON
JSONType = JSON().with_variant(JSONB, "postgresql")


class Driver(Base):
    __tablename__ = "drivers"

    driver_id = Column(String(64), primary_key=True)
    name = Column(String(200), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Segment(Base):
    __tablename__ = "segments"

    segment_id = Column(String(64), primary_key=True)
    driver_id = Column(String(64), ForeignKey("drivers.driver_id"), nullable=False, index=True)
    start_utc = Column(DateTime(timezone=True), nullable=False)
    end_utc = Column(DateTime(timezone=True), nullable=False)
    type = Column(String(16), nullable=False)  # DRIVING / OTHER_DUTY / REST
    revision = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AuditSnapshot(Base):
    """审核发布快照：固化发布时刻的输入修订与合规结论，之后更正不影响旧版。"""

    __tablename__ = "audit_snapshots"
    __table_args__ = (
        # 同一司机同一输入修订指纹只允许存在一版，并发发布由该唯一约束兜底
        UniqueConstraint("driver_id", "fingerprint", name="uq_audit_driver_fingerprint"),
    )

    audit_id = Column(Integer, primary_key=True, autoincrement=True)
    driver_id = Column(String(64), ForeignKey("drivers.driver_id"), nullable=False, index=True)
    fingerprint = Column(String(64), nullable=False)
    input_revisions = Column(JSONType, nullable=False)  # [{"segment_id":..,"revision":..}, ...]
    result = Column(JSONType, nullable=False)  # evaluate_driver 的结论
    published_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
