import hashlib
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models, schemas
from .compliance import Seg, evaluate_driver
from .db import get_db

router = APIRouter()


# ---------------------------------------------------------------- 工具函数

def _utc(dt: datetime) -> datetime:
    """数据库读出的时间统一规范为带 UTC 时区的 datetime。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_seg(s: models.Segment) -> Seg:
    return Seg(segment_id=s.segment_id, start=_utc(s.start_utc), end=_utc(s.end_utc), type=s.type)


def _get_driver_or_404(db: Session, driver_id: str) -> models.Driver:
    driver = db.get(models.Driver, driver_id)
    if driver is None:
        raise HTTPException(status_code=404, detail=f"司机 '{driver_id}' 不存在")
    return driver


def _driver_segments(db: Session, driver_id: str) -> list[models.Segment]:
    stmt = (
        select(models.Segment)
        .where(models.Segment.driver_id == driver_id)
        .order_by(models.Segment.start_utc, models.Segment.end_utc, models.Segment.segment_id)
    )
    return list(db.execute(stmt).scalars())


def _ensure_no_overlap(
    db: Session, driver_id: str, start: datetime, end: datetime, exclude_segment_id: str | None = None
) -> None:
    """同司机片段不得重叠；首尾相接（end == start）合法。"""
    stmt = select(models.Segment).where(
        models.Segment.driver_id == driver_id,
        models.Segment.start_utc < end,
        models.Segment.end_utc > start,
    )
    if exclude_segment_id is not None:
        stmt = stmt.where(models.Segment.segment_id != exclude_segment_id)
    hit = db.execute(stmt.limit(1)).scalars().first()
    if hit is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "segment_overlap",
                "message": f"与既有片段 '{hit.segment_id}' 时间重叠（相接合法、重叠非法）",
                "conflicting_segment_id": hit.segment_id,
            },
        )


def _evaluate_current(db: Session, driver_id: str) -> dict:
    """按现存记录实时重算合规结论。"""
    segs = _driver_segments(db, driver_id)
    return evaluate_driver([_to_seg(s) for s in segs])


# ---------------------------------------------------------------- 司机

@router.post("/drivers", response_model=schemas.DriverOut, status_code=201, tags=["drivers"])
def create_driver(body: schemas.DriverCreate, response: Response, db: Session = Depends(get_db)):
    """录入司机。同 ID 同内容重试幂等返回 200；同 ID 异内容返回 409。"""
    existing = db.get(models.Driver, body.driver_id)
    if existing is not None:
        if existing.name != body.name:
            raise HTTPException(
                status_code=409,
                detail=f"driver_id '{body.driver_id}' 已存在且内容不一致",
            )
        response.status_code = 200
        return existing
    driver = models.Driver(driver_id=body.driver_id, name=body.name)
    db.add(driver)
    db.commit()
    db.refresh(driver)
    return driver


@router.get("/drivers", response_model=list[schemas.DriverOut], tags=["drivers"])
def list_drivers(db: Session = Depends(get_db)):
    return list(db.execute(select(models.Driver).order_by(models.Driver.driver_id)).scalars())


@router.get("/drivers/{driver_id}", response_model=schemas.DriverOut, tags=["drivers"])
def get_driver(driver_id: str, db: Session = Depends(get_db)):
    return _get_driver_or_404(db, driver_id)


# ---------------------------------------------------------------- 片段

@router.post("/segments", response_model=schemas.SegmentOut, status_code=201, tags=["segments"])
def create_segment(body: schemas.SegmentCreate, response: Response, db: Session = Depends(get_db)):
    """录入值勤片段。同 ID 同内容重试幂等；同 ID 异内容 409；重叠 409。"""
    existing = db.get(models.Segment, body.segment_id)
    if existing is not None:
        same = (
            existing.driver_id == body.driver_id
            and _utc(existing.start_utc) == body.start_utc
            and _utc(existing.end_utc) == body.end_utc
            and existing.type == body.type.value
        )
        if not same:
            raise HTTPException(
                status_code=409,
                detail=f"segment_id '{body.segment_id}' 已存在且内容不一致（请用 PATCH 按修订号更正）",
            )
        response.status_code = 200  # 幂等重试
        return existing

    _get_driver_or_404(db, body.driver_id)
    _ensure_no_overlap(db, body.driver_id, body.start_utc, body.end_utc)

    seg = models.Segment(
        segment_id=body.segment_id,
        driver_id=body.driver_id,
        start_utc=body.start_utc,
        end_utc=body.end_utc,
        type=body.type.value,
        revision=1,
    )
    db.add(seg)
    db.commit()
    db.refresh(seg)
    return seg


@router.get("/segments/{segment_id}", response_model=schemas.SegmentOut, tags=["segments"])
def get_segment(segment_id: str, db: Session = Depends(get_db)):
    seg = db.get(models.Segment, segment_id)
    if seg is None:
        raise HTTPException(status_code=404, detail=f"片段 '{segment_id}' 不存在")
    return seg


@router.patch("/segments/{segment_id}", response_model=schemas.SegmentOut, tags=["segments"])
def correct_segment(segment_id: str, body: schemas.SegmentPatch, db: Session = Depends(get_db)):
    """按片段 ID 与预期修订号更正；修订号不符拒绝（409）。"""
    seg = db.execute(
        select(models.Segment).where(models.Segment.segment_id == segment_id).with_for_update()
    ).scalars().first()
    if seg is None:
        raise HTTPException(status_code=404, detail=f"片段 '{segment_id}' 不存在")
    if seg.revision != body.expected_revision:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "revision_conflict",
                "message": "预期修订号与当前修订号不符，拒绝更正",
                "current_revision": seg.revision,
                "expected_revision": body.expected_revision,
            },
        )

    new_driver = body.driver_id if body.driver_id is not None else seg.driver_id
    new_start = body.start_utc if body.start_utc is not None else _utc(seg.start_utc)
    new_end = body.end_utc if body.end_utc is not None else _utc(seg.end_utc)
    new_type = body.type.value if body.type is not None else seg.type

    if new_end <= new_start:
        raise HTTPException(status_code=422, detail="end_utc 必须晚于 start_utc（拒绝非正时长片段）")
    _get_driver_or_404(db, new_driver)
    _ensure_no_overlap(db, new_driver, new_start, new_end, exclude_segment_id=segment_id)

    seg.driver_id = new_driver
    seg.start_utc = new_start
    seg.end_utc = new_end
    seg.type = new_type
    seg.revision += 1
    db.commit()
    db.refresh(seg)
    return seg


@router.get(
    "/drivers/{driver_id}/segments",
    response_model=list[schemas.SegmentOut],
    tags=["segments"],
)
def list_driver_segments(driver_id: str, db: Session = Depends(get_db)):
    _get_driver_or_404(db, driver_id)
    return _driver_segments(db, driver_id)


# ---------------------------------------------------------------- 合规查询

@router.get(
    "/drivers/{driver_id}/compliance",
    response_model=schemas.ComplianceOut,
    tags=["compliance"],
)
def get_compliance(driver_id: str, db: Session = Depends(get_db)):
    """按现存记录实时重算，返回违规类型、首次越限时刻与参与片段。"""
    _get_driver_or_404(db, driver_id)
    result = _evaluate_current(db, driver_id)
    return schemas.ComplianceOut(
        driver_id=driver_id,
        evaluated_at=datetime.now(timezone.utc),
        compliant=result["compliant"],
        violations=result["violations"],
    )


# ---------------------------------------------------------------- 审核发布

def _fingerprint(revisions: list[dict]) -> str:
    payload = json.dumps(revisions, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@router.post(
    "/drivers/{driver_id}/audits",
    response_model=schemas.PublishResponse,
    status_code=201,
    tags=["audits"],
)
def publish_audit(driver_id: str, response: Response, db: Session = Depends(get_db)):
    """审核发布：固化当前输入修订与合规结论。

    同一司机同一输入修订（指纹相同）只允许发布一版；并发或重复发布
    返回既有版本（created_new=false），之后的更正不会改动旧版。
    """
    _get_driver_or_404(db, driver_id)
    segs = _driver_segments(db, driver_id)
    revisions = [
        {"segment_id": s.segment_id, "revision": s.revision}
        for s in sorted(segs, key=lambda x: x.segment_id)
    ]
    fingerprint = _fingerprint(revisions)
    result = evaluate_driver([_to_seg(s) for s in segs])

    snap = models.AuditSnapshot(
        driver_id=driver_id,
        fingerprint=fingerprint,
        input_revisions=revisions,
        result=result,
    )
    db.add(snap)
    try:
        db.commit()
    except IntegrityError:
        # 并发下同修订发布已被其他事务抢先提交：只留一版，返回既有版本
        db.rollback()
        snap = db.execute(
            select(models.AuditSnapshot).where(
                models.AuditSnapshot.driver_id == driver_id,
                models.AuditSnapshot.fingerprint == fingerprint,
            )
        ).scalars().one()
        response.status_code = 200
        return schemas.PublishResponse(created_new=False, snapshot=snap)

    db.refresh(snap)
    return schemas.PublishResponse(created_new=True, snapshot=snap)


@router.get(
    "/drivers/{driver_id}/audits",
    response_model=list[schemas.AuditSnapshotOut],
    tags=["audits"],
)
def list_audits(driver_id: str, db: Session = Depends(get_db)):
    _get_driver_or_404(db, driver_id)
    stmt = (
        select(models.AuditSnapshot)
        .where(models.AuditSnapshot.driver_id == driver_id)
        .order_by(models.AuditSnapshot.audit_id)
    )
    return list(db.execute(stmt).scalars())


def _get_audit_or_404(db: Session, driver_id: str, audit_id: int) -> models.AuditSnapshot:
    snap = db.get(models.AuditSnapshot, audit_id)
    if snap is None or snap.driver_id != driver_id:
        raise HTTPException(status_code=404, detail=f"审核版本 '{audit_id}' 不存在")
    return snap


@router.get(
    "/drivers/{driver_id}/audits/{audit_id}",
    response_model=schemas.AuditSnapshotOut,
    tags=["audits"],
)
def get_audit(driver_id: str, audit_id: int, db: Session = Depends(get_db)):
    return _get_audit_or_404(db, driver_id, audit_id)


@router.get(
    "/drivers/{driver_id}/audits/{audit_id}/compare",
    response_model=schemas.CompareOut,
    tags=["audits"],
)
def compare_audit(driver_id: str, audit_id: int, db: Session = Depends(get_db)):
    """对照历史发布版本与当前实时重算结果。"""
    snap = _get_audit_or_404(db, driver_id, audit_id)
    segs = _driver_segments(db, driver_id)
    current_result = evaluate_driver([_to_seg(s) for s in segs])

    snap_rev = {r["segment_id"]: r["revision"] for r in snap.input_revisions}
    cur_rev = {s.segment_id: s.revision for s in segs}
    changed = [
        {
            "segment_id": sid,
            "snapshot_revision": snap_rev[sid],
            "current_revision": cur_rev[sid],
        }
        for sid in sorted(snap_rev.keys() & cur_rev.keys())
        if snap_rev[sid] != cur_rev[sid]
    ]
    input_changes = schemas.InputChanges(
        added=sorted(cur_rev.keys() - snap_rev.keys()),
        removed=sorted(snap_rev.keys() - cur_rev.keys()),
        revision_changed=changed,
    )
    return schemas.CompareOut(
        audit_id=snap.audit_id,
        driver_id=driver_id,
        published_at=_utc(snap.published_at),
        snapshot_result=snap.result,
        current_result=current_result,
        results_differ=current_result["violations"] != snap.result["violations"],
        input_changes=input_changes,
    )
