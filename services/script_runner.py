"""统一脚本执行服务，供手动执行和定时执行共用。"""
import asyncio
import json
from typing import Mapping, Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import ExecutionRecord, Script, ScriptSchedule, Server, User
from executors import execution_manager
from services.execution_parameter_service import ExecutionParameterError, resolve_execution_parameters


_script_locks: dict[int, asyncio.Lock] = {}


def _script_lock(script_id: int) -> asyncio.Lock:
    lock = _script_locks.get(script_id)
    if lock is None:
        lock = asyncio.Lock()
        _script_locks[script_id] = lock
    return lock


def _build_schedule_snapshot(schedule: ScriptSchedule) -> str:
    payload = {
        "schedule_id": schedule.id,
        "schedule_name": schedule.name,
        "trigger_type": schedule.trigger_type,
        "cron_expression": schedule.cron_expression,
        "interval_seconds": schedule.interval_seconds,
        "run_at": schedule.run_at.isoformat() if schedule.run_at else None,
        "timezone": schedule.timezone,
        "overlap_policy": schedule.overlap_policy,
        "misfire_policy": schedule.misfire_policy,
    }
    return json.dumps(payload, ensure_ascii=False)


async def run_script_by_id(
    db: AsyncSession,
    script_id: int,
    *,
    trigger_source: str = "manual",
    schedule: Optional[ScriptSchedule] = None,
    current_user: Optional[User] = None,
    submitted_parameters: Optional[Mapping[str, str]] = None,
) -> int:
    """按 script_id 统一校验并触发执行。"""
    async with _script_lock(script_id):
        result = await db.execute(select(Script).where(Script.id == script_id))
        script = result.scalar_one_or_none()
        if not script:
            raise HTTPException(status_code=404, detail="脚本不存在")
        if not script.enabled:
            raise HTTPException(status_code=400, detail="脚本已被禁用")
        if await count_script_running(db, script_id) > 0:
            raise HTTPException(status_code=409, detail="脚本正在执行中，不能重复启动")

        server = None
        if script.server_id:
            srv = await db.execute(select(Server).where(Server.id == script.server_id))
            server = srv.scalar_one_or_none()
            if not server:
                raise HTTPException(status_code=400, detail="关联的服务器不存在")

        schedule_id = schedule.id if schedule else None
        trigger_snapshot = _build_schedule_snapshot(schedule) if schedule else ""
        try:
            parameter_values, parameter_snapshot = resolve_execution_parameters(
                script.execution_parameters,
                submitted_parameters,
                defaults_only=bool(schedule),
            )
        except ExecutionParameterError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        execution_id = await execution_manager.create_execution(
            script,
            server,
            trigger_source=trigger_source,
            schedule_id=schedule_id,
            trigger_snapshot=trigger_snapshot,
            execution_parameters=json.dumps(parameter_snapshot, ensure_ascii=False),
            created_by_user_id=current_user.id if current_user else None,
            created_by_username=current_user.username if current_user else "system",
        )
        await execution_manager.start_execution(execution_id, script, server, parameter_values)
        return execution_id


async def is_script_running(db: AsyncSession, script_id: int) -> bool:
    """检查脚本是否存在活动中的执行任务。"""
    return await count_script_running(db, script_id) > 0


async def count_script_running(db: AsyncSession, script_id: int) -> int:
    """统计脚本当前活动中的执行数量。"""
    running_result = await db.execute(
        select(ExecutionRecord).where(
            ExecutionRecord.script_id == script_id,
            ExecutionRecord.status.in_(["pending", "running", "stopping"]),
        )
    )
    count = 0
    for record in running_result.scalars().all():
        if execution_manager.is_active(record.id):
            count += 1
    return count
