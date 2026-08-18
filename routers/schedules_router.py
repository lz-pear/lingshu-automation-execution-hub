"""定时计划路由。"""
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from auth import require_admin
from models import ScheduleEvent, Script, ScriptSchedule, User
from scheduler_manager import scheduler_manager
from schemas import (
    ScheduleEventResponse,
    ScriptScheduleCreate,
    ScriptScheduleResponse,
    ScriptScheduleUpdate,
)
from services.schedule_service import compute_next_run_at, create_schedule_event, validate_schedule_payload
from services.audit_service import add_audit_log
from services.execution_parameter_service import ExecutionParameterError, resolve_execution_parameters

router = APIRouter(prefix="/api/schedules", tags=["定时计划"])


def _validate_script_schedule_parameters(script: Script, enabled: bool):
    if not enabled:
        return
    try:
        resolve_execution_parameters(script.execution_parameters, defaults_only=True)
    except ExecutionParameterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _get_schedule_or_404(db: AsyncSession, schedule_id: int) -> ScriptSchedule:
    result = await db.execute(select(ScriptSchedule).where(ScriptSchedule.id == schedule_id))
    schedule = result.scalar_one_or_none()
    if not schedule:
        raise HTTPException(status_code=404, detail="定时计划不存在")
    return schedule


@router.get("/", response_model=List[ScriptScheduleResponse], summary="获取定时计划列表")
async def list_schedules(_: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ScriptSchedule).order_by(ScriptSchedule.created_at.desc()))
    return result.scalars().all()


@router.get("/script/{script_id}", response_model=List[ScriptScheduleResponse], summary="获取脚本的定时计划")
async def list_script_schedules(script_id: int, _: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(ScriptSchedule)
        .where(ScriptSchedule.script_id == script_id)
        .order_by(ScriptSchedule.created_at.desc())
    )
    return result.scalars().all()


@router.post("/", response_model=ScriptScheduleResponse, summary="创建定时计划")
async def create_schedule(
    schedule_in: ScriptScheduleCreate,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    payload = schedule_in.model_dump()
    payload["overlap_policy"] = "skip"
    payload["max_concurrent_runs"] = 1
    validate_schedule_payload(payload)

    script_result = await db.execute(select(Script).where(Script.id == schedule_in.script_id))
    script = script_result.scalar_one_or_none()
    if not script:
        raise HTTPException(status_code=404, detail="关联脚本不存在")
    _validate_script_schedule_parameters(script, payload["enabled"])

    existing_schedule_result = await db.execute(
        select(ScriptSchedule).where(ScriptSchedule.script_id == schedule_in.script_id)
    )
    if existing_schedule_result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="每个脚本只允许创建一个定时计划")

    schedule = ScriptSchedule(**payload)
    schedule.next_run_at = compute_next_run_at(schedule) if schedule.enabled else None
    db.add(schedule)
    await db.flush()
    add_audit_log(db, request, action="schedule.create", resource_type="schedule", resource_id=schedule.id, summary=f"创建定时计划 {schedule.name}", user=current_user)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail="每个脚本只允许创建一个定时计划") from exc
    await db.refresh(schedule)
    await scheduler_manager.sync_schedule(schedule.id)
    return schedule


@router.get("/{schedule_id}", response_model=ScriptScheduleResponse, summary="获取定时计划详情")
async def get_schedule(schedule_id: int, _: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    return await _get_schedule_or_404(db, schedule_id)


@router.put("/{schedule_id}", response_model=ScriptScheduleResponse, summary="更新定时计划")
async def update_schedule(
    schedule_id: int,
    schedule_in: ScriptScheduleUpdate,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    schedule = await _get_schedule_or_404(db, schedule_id)
    update_data = schedule_in.model_dump(exclude_unset=True)
    update_data["overlap_policy"] = "skip"
    update_data["max_concurrent_runs"] = 1
    merged_data = {
        "name": schedule.name,
        "enabled": schedule.enabled,
        "trigger_type": schedule.trigger_type,
        "cron_expression": schedule.cron_expression,
        "interval_seconds": schedule.interval_seconds,
        "run_at": schedule.run_at,
        "timezone": schedule.timezone,
        "misfire_policy": schedule.misfire_policy,
        "misfire_grace_seconds": schedule.misfire_grace_seconds,
        "overlap_policy": schedule.overlap_policy,
        "max_concurrent_runs": schedule.max_concurrent_runs,
        "start_at": schedule.start_at,
        "end_at": schedule.end_at,
        "remark": schedule.remark,
        **update_data,
    }
    validate_schedule_payload(merged_data)
    script_result = await db.execute(select(Script).where(Script.id == schedule.script_id))
    script = script_result.scalar_one_or_none()
    if not script:
        raise HTTPException(status_code=404, detail="关联脚本不存在")
    _validate_script_schedule_parameters(script, merged_data["enabled"])

    for field, value in update_data.items():
        setattr(schedule, field, value)
    schedule.next_run_at = compute_next_run_at(schedule) if schedule.enabled else None
    add_audit_log(db, request, action="schedule.update", resource_type="schedule", resource_id=schedule.id, summary=f"更新定时计划 {schedule.name}", user=current_user)
    await db.commit()
    await db.refresh(schedule)
    await scheduler_manager.sync_schedule(schedule.id)
    return schedule


@router.delete("/{schedule_id}", summary="删除定时计划")
async def delete_schedule(
    schedule_id: int,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    schedule = await _get_schedule_or_404(db, schedule_id)
    event_result = await db.execute(select(ScheduleEvent).where(ScheduleEvent.schedule_id == schedule.id))
    for event in event_result.scalars().all():
        await db.delete(event)
    add_audit_log(db, request, action="schedule.delete", resource_type="schedule", resource_id=schedule.id, summary=f"删除定时计划 {schedule.name}", user=current_user)
    await db.delete(schedule)
    await db.commit()
    await scheduler_manager.remove_schedule(schedule_id)
    return {"message": "定时计划已删除"}


@router.post("/{schedule_id}/enable", response_model=ScriptScheduleResponse, summary="启用定时计划")
async def enable_schedule(
    schedule_id: int,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    schedule = await _get_schedule_or_404(db, schedule_id)
    script_result = await db.execute(select(Script).where(Script.id == schedule.script_id))
    script = script_result.scalar_one_or_none()
    if not script:
        raise HTTPException(status_code=404, detail="关联脚本不存在")
    _validate_script_schedule_parameters(script, True)
    schedule.enabled = True
    schedule.next_run_at = compute_next_run_at(schedule)
    if schedule.last_status == "disabled":
        schedule.last_status = ""
    await create_schedule_event(db, schedule.id, schedule.script_id, "enabled", "用户手动启用计划")
    add_audit_log(db, request, action="schedule.enable", resource_type="schedule", resource_id=schedule.id, summary=f"启用定时计划 {schedule.name}", user=current_user)
    await db.commit()
    await db.refresh(schedule)
    await scheduler_manager.sync_schedule(schedule.id)
    return schedule


@router.post("/{schedule_id}/disable", response_model=ScriptScheduleResponse, summary="停用定时计划")
async def disable_schedule(
    schedule_id: int,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    schedule = await _get_schedule_or_404(db, schedule_id)
    schedule.enabled = False
    schedule.next_run_at = None
    schedule.last_status = "disabled"
    await create_schedule_event(db, schedule.id, schedule.script_id, "disabled", "用户手动停用计划")
    add_audit_log(db, request, action="schedule.disable", resource_type="schedule", resource_id=schedule.id, summary=f"停用定时计划 {schedule.name}", user=current_user)
    await db.commit()
    await db.refresh(schedule)
    await scheduler_manager.sync_schedule(schedule.id)
    return schedule


@router.get("/{schedule_id}/events", response_model=List[ScheduleEventResponse], summary="获取定时计划事件")
async def list_schedule_events(schedule_id: int, _: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    await _get_schedule_or_404(db, schedule_id)
    result = await db.execute(
        select(ScheduleEvent)
        .where(ScheduleEvent.schedule_id == schedule_id)
        .order_by(desc(ScheduleEvent.created_at))
        .limit(50)
    )
    return result.scalars().all()
