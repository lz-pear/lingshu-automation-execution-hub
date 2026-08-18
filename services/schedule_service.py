"""定时计划服务：校验计划、构建 APScheduler Trigger、记录调度事件。"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import ScheduleEvent, Script, ScriptSchedule


def get_schedule_timezone(timezone_name: str):
    try:
        return ZoneInfo(timezone_name)
    except Exception:
        fallback_map = {
            "Asia/Shanghai": timezone(timedelta(hours=8), name="Asia/Shanghai"),
            "UTC": timezone.utc,
        }
        fallback = fallback_map.get(timezone_name)
        if fallback is not None:
            return fallback
        raise HTTPException(status_code=400, detail=f"不支持的时区: {timezone_name}")


def _normalize_datetime(value: Optional[datetime], tzinfo):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=tzinfo)
    return value.astimezone(tzinfo)


def build_trigger_from_payload(payload: dict):
    tzinfo = get_schedule_timezone(payload.get("timezone") or "Asia/Shanghai")
    trigger_type = payload.get("trigger_type")
    start_at = _normalize_datetime(payload.get("start_at"), tzinfo)
    end_at = _normalize_datetime(payload.get("end_at"), tzinfo)

    if trigger_type == "cron":
        expression = (payload.get("cron_expression") or "").strip()
        parts = expression.split()
        if len(parts) != 5:
            raise HTTPException(status_code=400, detail="Cron 表达式需为 5 段：分 时 日 月 周")
        minute, hour, day, month, day_of_week = parts
        try:
            return CronTrigger(
                minute=minute,
                hour=hour,
                day=day,
                month=month,
                day_of_week=day_of_week,
                start_date=start_at,
                end_date=end_at,
                timezone=tzinfo,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Cron 表达式不合法: {exc}") from exc

    if trigger_type == "interval":
        interval_seconds = payload.get("interval_seconds")
        if not interval_seconds:
            raise HTTPException(status_code=400, detail="间隔类型计划必须填写间隔秒数")
        try:
            return IntervalTrigger(
                seconds=interval_seconds,
                start_date=start_at or datetime.now(tzinfo),
                end_date=end_at,
                timezone=tzinfo,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"间隔计划配置不合法: {exc}") from exc

    if trigger_type == "once":
        run_at = _normalize_datetime(payload.get("run_at"), tzinfo)
        if not run_at:
            raise HTTPException(status_code=400, detail="一次性计划必须填写执行时间")
        return DateTrigger(run_date=run_at, timezone=tzinfo)

    raise HTTPException(status_code=400, detail="触发类型仅支持 cron / interval / once")


def build_trigger_for_schedule(schedule: ScriptSchedule):
    payload = {
        "trigger_type": schedule.trigger_type,
        "cron_expression": schedule.cron_expression,
        "interval_seconds": schedule.interval_seconds,
        "run_at": schedule.run_at,
        "timezone": schedule.timezone,
        "start_at": schedule.start_at,
        "end_at": schedule.end_at,
    }
    return build_trigger_from_payload(payload)


def validate_schedule_payload(payload: dict):
    timezone_name = payload.get("timezone") or "Asia/Shanghai"
    get_schedule_timezone(timezone_name)

    start_at = payload.get("start_at")
    end_at = payload.get("end_at")
    trigger_type = payload.get("trigger_type")

    if trigger_type != "once" and not start_at:
        raise HTTPException(status_code=400, detail="生效开始时间不能为空")

    if start_at and end_at and start_at > end_at:
        raise HTTPException(status_code=400, detail="结束时间不能早于开始时间")

    trigger = build_trigger_from_payload(payload)
    next_fire_time = trigger.get_next_fire_time(None, datetime.now(get_schedule_timezone(timezone_name)))
    if trigger_type == "once" and next_fire_time is None:
        raise HTTPException(status_code=400, detail="一次性计划执行时间必须晚于当前时间")


def compute_next_run_at(schedule: ScriptSchedule, previous_fire_time: Optional[datetime] = None) -> Optional[datetime]:
    trigger = build_trigger_for_schedule(schedule)
    timezone_obj = get_schedule_timezone(schedule.timezone or "Asia/Shanghai")
    now = datetime.now(timezone_obj)
    normalized_previous = _normalize_datetime(previous_fire_time, timezone_obj)
    next_run = trigger.get_next_fire_time(normalized_previous, now)
    if next_run is None:
        return None
    return next_run.replace(tzinfo=None)


async def create_schedule_event(
    session: AsyncSession,
    schedule_id: int,
    script_id: int,
    event_type: str,
    reason: str = "",
    execution_id: Optional[int] = None,
):
    session.add(
        ScheduleEvent(
            schedule_id=schedule_id,
            script_id=script_id,
            event_type=event_type,
            reason=reason,
            execution_id=execution_id,
        )
    )


async def load_schedule_with_script(session: AsyncSession, schedule_id: int) -> tuple[ScriptSchedule, Script]:
    result = await session.execute(
        select(ScriptSchedule, Script)
        .join(Script, Script.id == ScriptSchedule.script_id)
        .where(ScriptSchedule.id == schedule_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="定时计划不存在")
    return row[0], row[1]
