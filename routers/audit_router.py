"""管理员查看审计日志。"""
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import require_admin
from database import get_db
from models import AuditLog, User
from schemas import AuditLogPageResponse

router = APIRouter(prefix="/api/audit", tags=["审计日志"])


AUDIT_ACTION_CATEGORY_MAP = {
    "登录认证": ["login.success", "login.failed", "logout"],
    "用户管理": ["user.create", "user.update", "user.delete"],
    "服务器管理": ["server.create", "server.update", "server.delete"],
    "脚本管理": ["script.create", "script.update", "script.delete"],
    "计划管理": ["schedule.create", "schedule.update", "schedule.delete", "schedule.enable", "schedule.disable"],
    "执行控制": ["execution.run", "execution.stop"],
    "产物访问": ["artifact.download", "artifact.preview"],
    "脚本文件管理": [
        "script_file.root.create", "script_file.root.update", "script_file.root.delete",
        "script_file.upload", "script_file.update", "script_file.metadata.update", "script_file.download", "script_file.delete",
    ],
}


def _build_category_to_actions():
    return {category: actions[:] for category, actions in AUDIT_ACTION_CATEGORY_MAP.items()}


def _action_to_category(action: str) -> str:
    for category, actions in AUDIT_ACTION_CATEGORY_MAP.items():
        if action in actions:
            return category
    return "其他"


@router.get("/", response_model=AuditLogPageResponse, summary="审计日志")
async def list_audit_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    username: Optional[str] = Query(None),
    action_category: Optional[str] = Query(None),
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    query = select(AuditLog)
    count_query = select(func.count()).select_from(AuditLog)
    if username:
        query = query.where(AuditLog.username == username)
        count_query = count_query.where(AuditLog.username == username)
    category_actions = _build_category_to_actions()
    if action_category:
        actions = category_actions.get(action_category, [])
        if not actions:
            return AuditLogPageResponse(
                items=[],
                available_action_categories=[],
                total=0,
                page=page,
                page_size=page_size,
            )
        query = query.where(AuditLog.action.in_(actions))
        count_query = count_query.where(AuditLog.action.in_(actions))

    category_query = select(AuditLog.action)
    if username:
        category_query = category_query.where(AuditLog.username == username)
    category_result = await db.execute(category_query.distinct())
    available_action_categories = sorted(
        {
            _action_to_category(action)
            for action in category_result.scalars().all()
            if action
        },
        key=lambda item: (item == "其他", item),
    )
    total = await db.scalar(count_query) or 0
    result = await db.execute(
        query.order_by(desc(AuditLog.created_at), desc(AuditLog.id))
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return AuditLogPageResponse(
        items=result.scalars().all(),
        available_action_categories=available_action_categories,
        total=total,
        page=page,
        page_size=page_size,
    )
