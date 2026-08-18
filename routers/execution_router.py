"""带用户归属与数据范围校验的执行控制。"""
import json
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select, desc, func
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db, async_session
from auth import get_current_user
from models import ExecutionArtifact, ExecutionRecord, Script, User
from schemas import (
    ExecutionArtifactResponse,
    ExecutionHistoryListItem,
    ExecutionHistoryPageResponse,
    ExecutionHistoryResponse,
    ExecutionStartResponse,
    ExecutionStatusResponse,
    ScriptExecutionRequest,
)
from executors import execution_manager
from services.script_runner import run_script_by_id
from services.audit_service import add_audit_log
from services.execution_parameter_service import (
    build_execution_display_name,
    load_execution_parameter_snapshot,
    parameter_audit_summary,
)

router = APIRouter(prefix="/api/execution", tags=["执行控制"])


def _serialize_artifact(artifact: ExecutionArtifact) -> ExecutionArtifactResponse:
    return ExecutionArtifactResponse.model_validate(artifact)


def _ensure_execution_access(record: Optional[ExecutionRecord], current_user: User) -> ExecutionRecord:
    if not record:
        raise HTTPException(status_code=404, detail="执行记录不存在")
    if current_user.role != "admin" and record.created_by_user_id != current_user.id:
        raise HTTPException(status_code=404, detail="执行记录不存在")
    return record


async def _load_script_definition_map(db: AsyncSession, script_ids: list[int]) -> dict[int, str]:
    if not script_ids:
        return {}
    result = await db.execute(select(Script.id, Script.execution_parameters).where(Script.id.in_(script_ids)))
    return {script_id: execution_parameters for script_id, execution_parameters in result.all()}


@router.post("/run/{script_id}", response_model=ExecutionStartResponse, summary="执行脚本")
async def run_script(
    script_id: int,
    request: Request,
    execution_in: Optional[ScriptExecutionRequest] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    execution_id = await run_script_by_id(
        db,
        script_id,
        trigger_source="manual",
        current_user=current_user,
        submitted_parameters=execution_in.parameters if execution_in else {},
    )
    result = await db.execute(select(ExecutionRecord).where(ExecutionRecord.id == execution_id))
    record = result.scalar_one()
    parameter_summary = parameter_audit_summary(load_execution_parameter_snapshot(record.execution_parameters))
    summary = f"执行脚本 {record.script_name}"
    if parameter_summary:
        summary = f"{summary}；参数：{parameter_summary}"
    add_audit_log(db, request, action="execution.run", resource_type="execution", resource_id=record.id, summary=summary, user=current_user)
    await db.commit()
    return ExecutionStartResponse(execution_id=execution_id, message=f"脚本 '{record.script_name}' 已开始执行")


@router.get("/status/{execution_id}", response_model=ExecutionStatusResponse, summary="获取执行状态")
async def get_status(
    execution_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(ExecutionRecord).where(ExecutionRecord.id == execution_id))
    record = result.scalar_one_or_none()
    record = _ensure_execution_access(record, current_user)
    artifact_count_result = await db.execute(
        select(func.count(ExecutionArtifact.id)).where(ExecutionArtifact.execution_id == execution_id)
    )
    artifact_count = artifact_count_result.scalar_one() or 0
    return ExecutionStatusResponse(
        id=record.id,
        script_name=record.script_name,
        script_type=record.script_type,
        trigger_source=record.trigger_source,
        schedule_id=record.schedule_id,
        status=record.status,
        output=record.output or "",
        exit_code=record.exit_code,
        error_message=record.error_message,
        started_at=record.started_at,
        finished_at=record.finished_at,
        duration=record.duration,
        artifact_count=artifact_count,
        created_by_username=record.created_by_username or "system",
        stopped_by_username=record.stopped_by_username or "",
        execution_parameters=load_execution_parameter_snapshot(record.execution_parameters),
    )


@router.post("/stop/{execution_id}", summary="停止执行")
async def stop_execution(
    execution_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(ExecutionRecord).where(ExecutionRecord.id == execution_id))
    record = result.scalar_one_or_none()
    record = _ensure_execution_access(record, current_user)
    if record.status == "stopping":
        return {"message": "该执行正在停止中..."}
    if not execution_manager.is_running(execution_id):
        raise HTTPException(status_code=400, detail="该执行已结束，无法停止")
    marked = await execution_manager.mark_stopping(execution_id)
    if not marked:
        raise HTTPException(status_code=400, detail="该执行已结束，无法停止")
    record.stopped_by_user_id = current_user.id
    record.stopped_by_username = current_user.username
    add_audit_log(db, request, action="execution.stop", resource_type="execution", resource_id=record.id, summary=f"停止脚本 {record.script_name}", user=current_user)
    await db.commit()
    execution_manager.request_stop(execution_id)
    return {"message": "正在停止执行..."}


@router.get("/stream/{execution_id}", summary="SSE实时推送执行输出")
async def stream_output(
    execution_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(ExecutionRecord).where(ExecutionRecord.id == execution_id))
    record = result.scalar_one_or_none()
    record = _ensure_execution_access(record, current_user)

    async def event_generator():
        last_len = 0
        yield f"data: {json.dumps({'output': record.output or '', 'status': record.status})}\n\n"
        last_len = len(record.output or "")
        if record.status in ("completed", "failed", "stopped"):
            yield f"data: {json.dumps({'output': '', 'status': record.status, 'done': True})}\n\n"
            yield "data: [DONE]\n\n"
            return

        while True:
            if await request.is_disconnected():
                break
            await execution_manager.wait_for_update(execution_id, last_len, timeout=10.0)

            async with async_session() as session:
                r = await session.execute(
                    select(ExecutionRecord).where(ExecutionRecord.id == execution_id)
                )
                rec = r.scalar_one_or_none()
                if not rec:
                    break
                new_output = (rec.output or "")[last_len:]
                if new_output:
                    yield f"data: {json.dumps({'output': new_output, 'status': rec.status})}\n\n"
                    last_len = len(rec.output or "")
                if rec.status in ("completed", "failed", "stopped"):
                    yield f"data: {json.dumps({'output': '', 'status': rec.status, 'done': True})}\n\n"
                    break

        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.get("/history", response_model=ExecutionHistoryPageResponse, summary="获取执行历史")
async def get_history(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=500, description="每页条数"),
    script_id: Optional[int] = Query(None, description="按脚本筛选"),
    status_filter: Optional[str] = Query(None, description="按状态筛选"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    query = select(ExecutionRecord)
    count_query = select(func.count()).select_from(ExecutionRecord)
    if current_user.role != "admin":
        query = query.where(ExecutionRecord.created_by_user_id == current_user.id)
        count_query = count_query.where(ExecutionRecord.created_by_user_id == current_user.id)
    if script_id:
        query = query.where(ExecutionRecord.script_id == script_id)
        count_query = count_query.where(ExecutionRecord.script_id == script_id)
    if status_filter:
        query = query.where(ExecutionRecord.status == status_filter)
        count_query = count_query.where(ExecutionRecord.status == status_filter)

    total = await db.scalar(count_query) or 0
    query = query.order_by(desc(ExecutionRecord.created_at))
    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    records = result.scalars().all()
    execution_ids = [record.id for record in records]
    artifact_counts: dict[int, int] = {}
    if execution_ids:
        artifact_result = await db.execute(
            select(ExecutionArtifact.execution_id, func.count(ExecutionArtifact.id))
            .where(ExecutionArtifact.execution_id.in_(execution_ids))
            .group_by(ExecutionArtifact.execution_id)
        )
        artifact_counts = {execution_id: count for execution_id, count in artifact_result.all()}
    script_definition_map = await _load_script_definition_map(
        db,
        [record.script_id for record in records if record.script_id],
    )

    return ExecutionHistoryPageResponse(
        items=[
            ExecutionHistoryListItem(
                id=r.id, script_id=r.script_id, script_name=r.script_name,
                display_script_name=build_execution_display_name(
                    r.script_name,
                    r.execution_parameters,
                    script_definition_map.get(r.script_id or 0),
                ),
                script_type=r.script_type, server_name=r.server_name,
                trigger_source=r.trigger_source, schedule_id=r.schedule_id,
                status=r.status, exit_code=r.exit_code, error_message=r.error_message,
                started_at=r.started_at, finished_at=r.finished_at,
                duration=r.duration, created_at=r.created_at,
                artifact_count=artifact_counts.get(r.id, 0),
                created_by_username=r.created_by_username or "system",
                stopped_by_username=r.stopped_by_username or "",
            )
            for r in records
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/history/{execution_id}", response_model=ExecutionHistoryResponse, summary="获取执行历史详情")
async def get_history_detail(
    execution_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(ExecutionRecord).where(ExecutionRecord.id == execution_id))
    record = result.scalar_one_or_none()
    record = _ensure_execution_access(record, current_user)
    artifact_result = await db.execute(
        select(ExecutionArtifact)
        .where(ExecutionArtifact.execution_id == execution_id)
        .order_by(ExecutionArtifact.created_at.asc(), ExecutionArtifact.id.asc())
    )
    artifacts = [_serialize_artifact(artifact) for artifact in artifact_result.scalars().all()]
    script_definition_map = await _load_script_definition_map(db, [record.script_id] if record.script_id else [])
    return ExecutionHistoryResponse(
        id=record.id, script_id=record.script_id, script_name=record.script_name,
        display_script_name=build_execution_display_name(
            record.script_name,
            record.execution_parameters,
            script_definition_map.get(record.script_id or 0),
        ),
        script_type=record.script_type, server_name=record.server_name,
        trigger_source=record.trigger_source, schedule_id=record.schedule_id,
        status=record.status, exit_code=record.exit_code, error_message=record.error_message,
        output=record.output or "",
        started_at=record.started_at, finished_at=record.finished_at,
        duration=record.duration, created_at=record.created_at,
        artifact_count=len(artifacts), artifacts=artifacts,
        created_by_username=record.created_by_username or "system",
        stopped_by_username=record.stopped_by_username or "",
        execution_parameters=load_execution_parameter_snapshot(record.execution_parameters),
    )
