"""基于 APScheduler AsyncIOScheduler 的调度管理器。"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional

from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR, EVENT_JOB_MISSED, JobExecutionEvent
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.base import BaseTrigger
from sqlalchemy import select

from database import async_session
from executors import execution_manager
from models import ExecutionRecord, ScriptSchedule
from services.schedule_service import build_trigger_for_schedule, compute_next_run_at, create_schedule_event
from services.script_runner import count_script_running, run_script_by_id


class SchedulerManager:
    """调度管理器：业务数据在数据库，触发内核用 APScheduler。"""

    def __init__(self):
        self.scheduler: Optional[AsyncIOScheduler] = None
        self._running = False
        self._completion_hook_registered = False

    async def start(self):
        if self._running:
            return
        await self.recover_interrupted_executions()
        self.scheduler = AsyncIOScheduler()
        self.scheduler.add_listener(
            self._handle_job_event,
            EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED,
        )
        self.scheduler.start()
        self._running = True
        if not self._completion_hook_registered:
            execution_manager.register_completion_callback(self.on_execution_completed)
            self._completion_hook_registered = True
        await self.reload_all_schedules()

    async def shutdown(self):
        self._running = False
        if self.scheduler:
            self.scheduler.shutdown(wait=True)
            self.scheduler = None

    async def reload_all_schedules(self):
        async with async_session() as session:
            result = await session.execute(select(ScriptSchedule))
            schedules = result.scalars().all()

        if not self.scheduler:
            return

        existing_ids = {job.id for job in self.scheduler.get_jobs()}
        active_ids: set[str] = set()

        for schedule in schedules:
            if schedule.enabled:
                await self._upsert_scheduler_job(schedule)
                active_ids.add(self._job_id(schedule.id))
            else:
                schedule.next_run_at = None
                await self._persist_schedule_runtime(schedule)

        for job_id in existing_ids - active_ids:
            self.scheduler.remove_job(job_id)

    async def sync_schedule(self, schedule_id: int):
        async with async_session() as session:
            result = await session.execute(select(ScriptSchedule).where(ScriptSchedule.id == schedule_id))
            schedule = result.scalar_one_or_none()
        if not schedule:
            await self.remove_schedule(schedule_id)
            return
        if schedule.enabled:
            await self._upsert_scheduler_job(schedule)
        else:
            await self.remove_schedule(schedule_id)
            schedule.next_run_at = None
            await self._persist_schedule_runtime(schedule)

    async def remove_schedule(self, schedule_id: int):
        if self.scheduler:
            try:
                self.scheduler.remove_job(self._job_id(schedule_id))
            except Exception:
                pass

    async def recover_interrupted_executions(self):
        async with async_session() as session:
            result = await session.execute(
                select(ExecutionRecord).where(
                    ExecutionRecord.status.in_(["pending", "running", "stopping"])
                )
            )
            records = result.scalars().all()
            now = datetime.now()
            for record in records:
                record.status = "stopped"
                record.error_message = "服务重启导致执行中断"
                if record.finished_at is None:
                    record.finished_at = now
                if record.started_at and not record.duration:
                    record.duration = max((record.finished_at - record.started_at).total_seconds(), 0.0)
            await session.commit()

    async def fire_schedule(self, schedule_id: int, *, manual: bool = False):
        async with async_session() as session:
            result = await session.execute(
                select(ScriptSchedule).where(ScriptSchedule.id == schedule_id)
            )
            schedule = result.scalar_one_or_none()
            if not schedule:
                return

            now = datetime.now()
            if not schedule.enabled and not manual:
                return

            running_count = await count_script_running(session, schedule.script_id)

            if running_count > 0:
                schedule.last_status = "skipped"
                schedule.last_run_at = now
                await create_schedule_event(session, schedule.id, schedule.script_id, "skipped", "脚本仍在执行，本次调度已跳过")
                await session.commit()
                await self._refresh_job_runtime(schedule.id)
                return

            execution_id = await run_script_by_id(
                session,
                schedule.script_id,
                trigger_source="schedule",
                schedule=schedule,
            )
            schedule.last_run_at = now
            schedule.last_execution_id = execution_id
            schedule.last_status = "triggered" if not manual else "manual-triggered"
            await create_schedule_event(
                session,
                schedule.id,
                schedule.script_id,
                "triggered",
                "用户手动立即执行计划" if manual else "APScheduler 触发执行",
                execution_id=execution_id,
            )
            await session.commit()
            await self._refresh_job_runtime(schedule.id)

    async def on_execution_completed(self, execution_id: int):
        async with async_session() as session:
            result = await session.execute(
                select(ExecutionRecord).where(ExecutionRecord.id == execution_id)
            )
            record = result.scalar_one_or_none()
            if not record or not record.script_id:
                return

            is_scheduled_execution = record.trigger_source == "schedule" and bool(record.schedule_id)
            schedule_query = (
                select(ScriptSchedule).where(ScriptSchedule.id == record.schedule_id)
                if is_scheduled_execution
                else select(ScriptSchedule).where(
                    ScriptSchedule.script_id == record.script_id,
                    ScriptSchedule.overlap_policy == "queue",
                    ScriptSchedule.pending_run_count > 0,
                )
            )
            schedule_result = await session.execute(schedule_query)
            schedule = schedule_result.scalar_one_or_none()
            if not schedule:
                return

            if is_scheduled_execution and record.status in ("completed", "stopped", "failed"):
                schedule.last_status = record.status

            should_disable = (
                is_scheduled_execution
                and schedule.trigger_type == "once"
                and schedule.pending_run_count <= 0
            )
            if should_disable:
                schedule.enabled = False
                schedule.next_run_at = None

            if schedule.overlap_policy == "queue" and (schedule.pending_run_count or 0) > 0:
                schedule.pending_run_count -= 1
                await session.commit()
                if schedule.enabled:
                    await self.fire_schedule(schedule.id)
                return

            await session.commit()
            await self._refresh_job_runtime(schedule.id)

    async def _upsert_scheduler_job(self, schedule: ScriptSchedule):
        if not self.scheduler:
            return
        trigger = build_trigger_for_schedule(schedule)
        job_id = self._job_id(schedule.id)
        # “跳过”不补跑旧触发；“补跑一次”在用户设定的宽限期内合并补跑一次。
        misfire_grace_time = 1 if schedule.misfire_policy == "skip" else schedule.misfire_grace_seconds
        job_kwargs = {
            "id": job_id,
            "replace_existing": True,
            "misfire_grace_time": misfire_grace_time,
            "coalesce": True,
            # skip / queue 依赖业务层判断，不能让 APScheduler 提前在 wrapper 层丢掉触发
            "max_instances": max(schedule.max_concurrent_runs or 1, 1) if schedule.overlap_policy == "parallel" else 20,
        }
        self.scheduler.add_job(self._job_wrapper, trigger=trigger, args=[schedule.id], **job_kwargs)
        await self._refresh_job_runtime(schedule.id)

    async def _refresh_job_runtime(self, schedule_id: int):
        if not self.scheduler:
            return
        async with async_session() as session:
            result = await session.execute(
                select(ScriptSchedule).where(ScriptSchedule.id == schedule_id)
            )
            schedule = result.scalar_one_or_none()
            if not schedule:
                return
            job = self.scheduler.get_job(self._job_id(schedule.id))
            schedule.next_run_at = job.next_run_time.replace(tzinfo=None) if job and job.next_run_time else None
            if schedule.trigger_type == "once" and schedule.next_run_at is None and schedule.pending_run_count <= 0:
                schedule.enabled = False
            await session.commit()

    async def _persist_schedule_runtime(self, schedule: ScriptSchedule):
        async with async_session() as session:
            db_schedule = await session.get(ScriptSchedule, schedule.id)
            if not db_schedule:
                return
            db_schedule.next_run_at = schedule.next_run_at
            db_schedule.enabled = schedule.enabled
            db_schedule.last_status = schedule.last_status
            await session.commit()

    async def _job_wrapper(self, schedule_id: int):
        await self.fire_schedule(schedule_id)

    def _handle_job_event(self, event: JobExecutionEvent):
        if not self.scheduler:
            return
        if event.code == EVENT_JOB_MISSED:
            schedule_id = self._schedule_id_from_job_id(event.job_id)
            if schedule_id is None:
                return
            asyncio.create_task(self._record_misfire(schedule_id))
            return
        if event.code == EVENT_JOB_ERROR:
            schedule_id = self._schedule_id_from_job_id(event.job_id)
            if schedule_id is None:
                return
            asyncio.create_task(self._record_job_error(schedule_id, event.exception))

    async def _record_misfire(self, schedule_id: int):
        async with async_session() as session:
            result = await session.execute(select(ScriptSchedule).where(ScriptSchedule.id == schedule_id))
            schedule = result.scalar_one_or_none()
            if not schedule:
                return
            schedule.last_status = "misfired"
            schedule.last_run_at = datetime.now()
            grace_seconds = 1 if schedule.misfire_policy == "skip" else schedule.misfire_grace_seconds
            await create_schedule_event(
                session,
                schedule.id,
                schedule.script_id,
                "misfired",
                f"超过宽限时间 {grace_seconds} 秒，APScheduler 已判定为错过执行",
            )
            await session.commit()
            await self._refresh_job_runtime(schedule.id)

    async def _record_job_error(self, schedule_id: int, exception: Optional[BaseException]):
        async with async_session() as session:
            result = await session.execute(select(ScriptSchedule).where(ScriptSchedule.id == schedule_id))
            schedule = result.scalar_one_or_none()
            if not schedule:
                return
            schedule.last_status = "error"
            await create_schedule_event(
                session,
                schedule.id,
                schedule.script_id,
                "error",
                f"调度包装任务异常: {exception}" if exception else "调度包装任务异常",
            )
            await session.commit()

    @staticmethod
    def _job_id(schedule_id: int) -> str:
        return f"schedule:{schedule_id}"

    @staticmethod
    def _schedule_id_from_job_id(job_id: str) -> Optional[int]:
        if not job_id.startswith("schedule:"):
            return None
        try:
            return int(job_id.split(":", 1)[1])
        except ValueError:
            return None


scheduler_manager = SchedulerManager()
