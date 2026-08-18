"""脚本列表、个人置顶与管理员脚本管理。"""
from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import delete, desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database import get_db
from auth import get_current_user, require_admin
from executors import execution_manager
from models import ExecutionRecord, ScheduleEvent, Script, ScriptSchedule, Server, User, UserScriptPin
from schemas import ScriptCreate, ScriptUpdate, ScriptResponse
from services.audit_service import add_audit_log
from services.execution_parameter_service import (
    ExecutionParameterError,
    dump_parameter_definitions,
    load_parameter_definitions,
    resolve_execution_parameters,
)
from scheduler_manager import scheduler_manager

router = APIRouter(prefix="/api/scripts", tags=["脚本管理"])


def _weekday_text(value: str) -> str:
    mapping = {
        "0": "周一",
        "1": "周二",
        "2": "周三",
        "3": "周四",
        "4": "周五",
        "5": "周六",
        "6": "周日",
        "7": "周一",
        "0-4": "工作日",
        "0,1,2,3,4": "工作日",
        "5,6": "周末",
        "6,5": "周末",
    }
    return mapping.get(value, "")


def _cron_summary(expression: str) -> str:
    raw = (expression or "").strip()
    if not raw:
        return "Cron"

    preset_map = {
        "0 22 * * *": "每天 22:00",
        "0 0 * * *": "每天 00:00",
        "0 * * * *": "每小时整点",
        "*/30 * * * *": "每 30 分钟",
        "*/10 * * * *": "每 10 分钟",
        "0 9 * * 0-4": "工作日 09:00",
        "0 18 * * 0-4": "工作日 18:00",
        "0 9 * * 0": "周一 09:00",
        "0 23 * * 6": "周日 23:00",
        "0 9 1 * *": "每月 1 号 09:00",
    }
    if raw in preset_map:
        return preset_map[raw]

    parts = raw.split()
    if len(parts) != 5:
        return f"Cron {raw}"

    minute, hour, day, month, day_of_week = parts
    if minute == "*" and hour == "*" and day == "*" and month == "*" and day_of_week == "*":
        return "每分钟"
    if minute.startswith("*/") and hour == "*" and day == "*" and month == "*" and day_of_week == "*":
        return f"每 {minute[2:]} 分钟"
    if minute == "0" and hour == "*" and day == "*" and month == "*" and day_of_week == "*":
        return "每小时整点"

    time_text = ""
    if minute.isdigit() and hour.isdigit():
        time_text = f"{hour.zfill(2)}:{minute.zfill(2)}"

    weekday = _weekday_text(day_of_week)
    if day == "*" and month == "*" and day_of_week == "*" and time_text:
        return f"每天 {time_text}"
    if day == "*" and month == "*" and weekday and time_text:
        return f"{weekday} {time_text}"
    if day.isdigit() and month == "*" and day_of_week == "*" and time_text:
        return f"每月 {int(day)} 号 {time_text}"
    return f"Cron {raw}"


def _build_schedule_summary(schedule: Optional[ScriptSchedule]) -> tuple[str, Optional[bool]]:
    if not schedule:
        return "", None

    if schedule.trigger_type == "interval":
        return f"每{schedule.interval_seconds or 0}秒", schedule.enabled

    if schedule.trigger_type == "once":
        if schedule.run_at:
            return f"一次性 {schedule.run_at.strftime('%m-%d %H:%M')}", schedule.enabled
        return "一次性执行", schedule.enabled

    return _cron_summary(schedule.cron_expression or ""), schedule.enabled


def _build_script_response(script: Script, schedule: Optional[ScriptSchedule] = None) -> ScriptResponse:
    summary, schedule_enabled = _build_schedule_summary(schedule)
    return ScriptResponse(
        id=script.id,
        name=script.name,
        description=script.description,
        script_type=script.script_type,
        server_id=script.server_id,
        server=script.server,
        remote_path=script.remote_path,
        run_as_user=script.run_as_user,
        command=script.command,
        http_url=script.http_url,
        http_method=script.http_method,
        http_headers=script.http_headers,
        http_body=script.http_body,
        timeout=script.timeout,
        tags=script.tags,
        execution_parameters=load_parameter_definitions(script.execution_parameters),
        enabled=script.enabled,
        schedule_summary=summary,
        schedule_enabled=schedule_enabled,
        last_success_duration=None,
        last_success_finished_at=None,
        running_execution_id=None,
        running_started_at=None,
        running_status="",
        pinned_at=script.pinned_at,
        created_at=script.created_at,
        updated_at=script.updated_at,
    )


async def _build_script_responses(
    db: AsyncSession,
    scripts: List[Script],
    current_user: User,
) -> list[dict]:
    if not scripts:
        return []

    script_ids = [script.id for script in scripts]
    schedule_map = {}
    if current_user.role == "admin":
        result = await db.execute(
            select(ScriptSchedule).where(ScriptSchedule.script_id.in_(script_ids))
        )
        schedule_map = {schedule.script_id: schedule for schedule in result.scalars().all()}

    pin_result = await db.execute(
        select(UserScriptPin).where(
            UserScriptPin.user_id == current_user.id,
            UserScriptPin.script_id.in_(script_ids),
        )
    )
    pin_map = {pin.script_id: pin.pinned_at for pin in pin_result.scalars().all()}

    execution_result = await db.execute(
        select(ExecutionRecord)
        .where(ExecutionRecord.script_id.in_(script_ids))
        .order_by(desc(ExecutionRecord.created_at), desc(ExecutionRecord.id))
    )

    running_statuses = {"pending", "running", "stopping"}
    success_statuses = {"completed"}
    running_map: dict[int, ExecutionRecord] = {}
    last_success_map: dict[int, ExecutionRecord] = {}

    for record in execution_result.scalars().all():
        if record.script_id is None:
            continue

        if (
            record.status in running_statuses
            and record.script_id not in running_map
            and execution_manager.is_active(record.id)
        ):
            running_map[record.script_id] = record

        if record.status in success_statuses and record.script_id not in last_success_map:
            last_success_map[record.script_id] = record

        if len(running_map) == len(script_ids) and len(last_success_map) == len(script_ids):
            break

    responses: list[dict] = []
    for script in scripts:
        response = _build_script_response(script, schedule_map.get(script.id))
        response.pinned_at = pin_map.get(script.id)

        running_record = running_map.get(script.id)
        if running_record:
            response.running_status = running_record.status
            if current_user.role == "admin" or running_record.created_by_user_id == current_user.id:
                response.running_execution_id = running_record.id
                response.running_started_at = running_record.started_at

        last_success_record = last_success_map.get(script.id)
        if current_user.role == "admin" and last_success_record:
            response.last_success_duration = last_success_record.duration
            response.last_success_finished_at = last_success_record.finished_at

        payload = response.model_dump()
        if current_user.role != "admin":
            allowed_fields = {
                "id", "name", "description", "script_type", "timeout", "tags", "enabled",
                "execution_parameters",
                "running_execution_id", "running_started_at", "running_status", "pinned_at",
            }
            payload = {key: value for key, value in payload.items() if key in allowed_fields}
        responses.append(payload)

    return sorted(
        responses,
        key=lambda item: (item.get("pinned_at") is not None, item.get("pinned_at") or datetime.min),
        reverse=True,
    )


async def validate_script_payload(db: AsyncSession, script_data: dict):
    """按脚本最终状态校验字段，避免保存出不可执行的配置。"""
    script_type = script_data.get("script_type")

    if script_type == "ssh":
        if not script_data.get("server_id"):
            raise HTTPException(status_code=400, detail="SSH类型脚本必须关联服务器")
        if not script_data.get("remote_path"):
            raise HTTPException(status_code=400, detail="SSH类型脚本必须填写远程脚本路径")
        srv = await db.execute(select(Server).where(Server.id == script_data["server_id"]))
        if not srv.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="关联的服务器不存在")
    elif script_type == "local":
        if not script_data.get("command"):
            raise HTTPException(status_code=400, detail="本地类型脚本必须填写执行命令")
    elif script_type == "http":
        if not script_data.get("http_url"):
            raise HTTPException(status_code=400, detail="HTTP类型脚本必须填写URL")
        if script_data.get("execution_parameters"):
            raise HTTPException(status_code=400, detail="执行参数首期仅支持本地和SSH脚本")

    definitions = script_data.get("execution_parameters") or []
    if definitions:
        try:
            # 同时校验默认值长度；必填项允许不设默认值，供手动执行时填写。
            resolve_execution_parameters(definitions, {})
        except ExecutionParameterError as exc:
            if "请填写参数" not in str(exc):
                raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/", response_model=None, summary="获取脚本列表")
async def list_scripts(
    script_type: Optional[str] = Query(None, pattern="^(ssh|local|http)$"),
    tag: Optional[str] = Query(None),
    enabled: Optional[bool] = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    query = (
        select(Script)
        .options(selectinload(Script.server))
        .order_by(Script.created_at.desc())
    )
    if script_type:
        query = query.where(Script.script_type == script_type)
    if tag:
        query = query.where(Script.tags.contains(tag))
    if enabled is not None:
        query = query.where(Script.enabled == enabled)
    result = await db.execute(query)
    scripts = result.scalars().all()
    return await _build_script_responses(db, scripts, current_user)


@router.post("/", response_model=ScriptResponse, summary="创建脚本")
async def create_script(
    script_in: ScriptCreate,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    await validate_script_payload(db, script_in.model_dump())

    script_data = script_in.model_dump()
    script_data["execution_parameters"] = dump_parameter_definitions(script_data["execution_parameters"])
    script = Script(**script_data)
    db.add(script)
    await db.flush()
    add_audit_log(
        db,
        request,
        action="script.create",
        resource_type="script",
        resource_id=script.id,
        summary=f"创建脚本 {script.name}",
        user=current_user,
    )
    await db.commit()
    await db.refresh(script)
    # 重新查询以加载server关系
    result = await db.execute(
        select(Script).options(selectinload(Script.server)).where(Script.id == script.id)
    )
    loaded_script = result.scalar_one()
    return (await _build_script_responses(db, [loaded_script], current_user))[0]


@router.get("/{script_id}", response_model=ScriptResponse, summary="获取脚本详情")
async def get_script(
    script_id: int,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Script).options(selectinload(Script.server)).where(Script.id == script_id)
    )
    script = result.scalar_one_or_none()
    if not script:
        raise HTTPException(status_code=404, detail="脚本不存在")
    return (await _build_script_responses(db, [script], current_user))[0]


@router.put("/{script_id}", response_model=ScriptResponse, summary="更新脚本")
async def update_script(
    script_id: int,
    script_in: ScriptUpdate,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Script).options(selectinload(Script.server)).where(Script.id == script_id)
    )
    script = result.scalar_one_or_none()
    if not script:
        raise HTTPException(status_code=404, detail="脚本不存在")

    update_data = script_in.model_dump(exclude_unset=True)
    validation_update_data = dict(update_data)
    merged_data = {
        "script_type": script.script_type,
        "server_id": script.server_id,
        "remote_path": script.remote_path,
        "command": script.command,
        "http_url": script.http_url,
        "execution_parameters": load_parameter_definitions(script.execution_parameters),
        **validation_update_data,
    }
    await validate_script_payload(db, merged_data)

    schedule_result = await db.execute(
        select(ScriptSchedule).where(
            ScriptSchedule.script_id == script.id,
            ScriptSchedule.enabled.is_(True),
        )
    )
    if schedule_result.scalar_one_or_none():
        try:
            resolve_execution_parameters(merged_data.get("execution_parameters"), defaults_only=True)
        except ExecutionParameterError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"脚本已有启用中的定时计划：{exc}",
            ) from exc

    if "execution_parameters" in update_data:
        update_data["execution_parameters"] = dump_parameter_definitions(update_data["execution_parameters"])

    for field, value in update_data.items():
        setattr(script, field, value)
    add_audit_log(
        db,
        request,
        action="script.update",
        resource_type="script",
        resource_id=script.id,
        summary=f"更新脚本 {script.name}",
        user=current_user,
    )
    await db.commit()
    await db.refresh(script)
    result = await db.execute(
        select(Script).options(selectinload(Script.server)).where(Script.id == script.id)
    )
    loaded_script = result.scalar_one()
    return (await _build_script_responses(db, [loaded_script], current_user))[0]


@router.post("/{script_id}/pin", response_model=None, summary="置顶脚本")
async def pin_script(
    script_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Script).options(selectinload(Script.server)).where(Script.id == script_id)
    )
    script = result.scalar_one_or_none()
    if not script:
        raise HTTPException(status_code=404, detail="脚本不存在")

    pin_result = await db.execute(
        select(UserScriptPin).where(
            UserScriptPin.user_id == current_user.id,
            UserScriptPin.script_id == script.id,
        )
    )
    pin = pin_result.scalar_one_or_none()
    if not pin:
        db.add(UserScriptPin(user_id=current_user.id, script_id=script.id, pinned_at=datetime.now()))
    else:
        pin.pinned_at = datetime.now()
    await db.commit()
    return (await _build_script_responses(db, [script], current_user))[0]


@router.post("/{script_id}/unpin", response_model=None, summary="取消置顶")
async def unpin_script(
    script_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Script).options(selectinload(Script.server)).where(Script.id == script_id)
    )
    script = result.scalar_one_or_none()
    if not script:
        raise HTTPException(status_code=404, detail="脚本不存在")

    await db.execute(
        delete(UserScriptPin).where(
            UserScriptPin.user_id == current_user.id,
            UserScriptPin.script_id == script.id,
        )
    )
    await db.commit()
    return (await _build_script_responses(db, [script], current_user))[0]


@router.delete("/{script_id}", summary="删除脚本")
async def delete_script(
    script_id: int,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Script).where(Script.id == script_id))
    script = result.scalar_one_or_none()
    if not script:
        raise HTTPException(status_code=404, detail="脚本不存在")

    running_result = await db.execute(
        select(ExecutionRecord).where(
            ExecutionRecord.script_id == script_id,
            ExecutionRecord.status.in_(["pending", "running", "stopping"]),
        )
    )
    running_records = running_result.scalars().all()
    if any(execution_manager.is_active(record.id) for record in running_records):
        raise HTTPException(status_code=400, detail=f"脚本“{script.name}”正在执行，请先停止后再删除")

    history_result = await db.execute(
        select(ExecutionRecord).where(ExecutionRecord.script_id == script_id)
    )
    for record in history_result.scalars().all():
        record.script_id = None
        record.schedule_id = None

    schedule_result = await db.execute(select(ScriptSchedule).where(ScriptSchedule.script_id == script_id))
    schedules = schedule_result.scalars().all()
    schedule_ids = [schedule.id for schedule in schedules]
    for schedule in schedules:
        event_result = await db.execute(select(ScheduleEvent).where(ScheduleEvent.schedule_id == schedule.id))
        for event in event_result.scalars().all():
            await db.delete(event)
        await db.delete(schedule)

    await db.execute(delete(UserScriptPin).where(UserScriptPin.script_id == script_id))
    add_audit_log(
        db,
        request,
        action="script.delete",
        resource_type="script",
        resource_id=script.id,
        summary=f"删除脚本 {script.name}，保留已有执行历史",
        user=current_user,
    )
    await db.delete(script)
    # APScheduler 的任务在内存中，删除数据库记录不会自动取消下一次触发。
    # 先注销所有关联任务；若数据库提交失败则重新同步，避免两边状态不一致。
    for schedule_id in schedule_ids:
        await scheduler_manager.remove_schedule(schedule_id)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        for schedule_id in schedule_ids:
            await scheduler_manager.sync_schedule(schedule_id)
        raise
    return {"message": "脚本已删除"}
