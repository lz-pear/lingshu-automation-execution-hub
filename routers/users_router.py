"""固定管理员管理普通用户。"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import require_admin
from database import get_db
from models import AuditLog, AuthSession, ExecutionArtifact, ExecutionRecord, User, UserScriptPin
from schemas import UserCreate, UserResponse, UserUpdate
from services.artifact_service import (
    purge_staged_artifact_dirs,
    restore_staged_artifact_dirs,
    stage_execution_artifact_dirs,
)
from services.audit_service import add_audit_log
from services.auth_service import hash_password, invalidate_user_sessions, normalize_username

router = APIRouter(prefix="/api/users", tags=["用户管理"])


def _serialize_user(user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        username=user.username,
        display_name=user.display_name or user.username,
        role=user.role,
        is_fixed_admin=user.role == "admin",
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


async def _ordinary_user_or_404(db: AsyncSession, user_id: int) -> User:
    user = await db.get(User, user_id)
    if not user or user.role == "admin":
        raise HTTPException(status_code=404, detail="用户不存在")
    return user


@router.get("/", response_model=list[UserResponse], summary="用户列表")
async def list_users(
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).order_by(User.role.asc(), User.created_at.desc()))
    return [_serialize_user(user) for user in result.scalars().all()]


@router.post("/", response_model=UserResponse, summary="创建普通用户")
async def create_user(
    payload: UserCreate,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    username = normalize_username(payload.username)
    if not username or payload.password == "":
        raise HTTPException(status_code=400, detail="用户名和密码不能为空")
    existing_result = await db.execute(
        select(User).where(func.lower(User.username) == username.lower())
    )
    if existing_result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="用户名已存在")

    user = User(
        username=username,
        display_name=(payload.display_name or "").strip() or username,
        password_hash=hash_password(payload.password),
        role="user",
        auth_version=1,
    )
    db.add(user)
    await db.flush()
    add_audit_log(
        db,
        request,
        action="user.create",
        resource_type="user",
        resource_id=user.id,
        summary=f"创建普通用户 {user.username}",
        user=current_user,
    )
    await db.commit()
    await db.refresh(user)
    return _serialize_user(user)


@router.put("/{user_id}", response_model=UserResponse, summary="修改普通用户")
async def update_user(
    user_id: int,
    payload: UserUpdate,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await _ordinary_user_or_404(db, user_id)
    changes = []
    if payload.display_name is not None:
        user.display_name = payload.display_name.strip() or user.username
        changes.append("显示名称")
    if payload.password is not None:
        if payload.password == "":
            raise HTTPException(status_code=400, detail="密码不能为空")
        user.password_hash = hash_password(payload.password)
        await invalidate_user_sessions(db, user)
        changes.append("密码")
    if not changes:
        return _serialize_user(user)

    add_audit_log(
        db,
        request,
        action="user.update",
        resource_type="user",
        resource_id=user.id,
        summary=f"修改用户 {user.username}: {', '.join(changes)}",
        user=current_user,
    )
    await db.commit()
    await db.refresh(user)
    return _serialize_user(user)


@router.delete("/{user_id}", summary="删除普通用户")
async def delete_user(
    user_id: int,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await _ordinary_user_or_404(db, user_id)
    username = user.username
    execution_result = await db.execute(
        select(ExecutionRecord).where(ExecutionRecord.created_by_user_id == user.id)
    )
    executions = execution_result.scalars().all()
    execution_ids = [record.id for record in executions]
    staged_artifact_dirs = stage_execution_artifact_dirs(execution_ids)

    try:
        if execution_ids:
            await db.execute(
                delete(ExecutionArtifact).where(ExecutionArtifact.execution_id.in_(execution_ids))
            )
            for record in executions:
                await db.delete(record)

        await db.execute(delete(AuthSession).where(AuthSession.user_id == user.id))
        await db.execute(delete(UserScriptPin).where(UserScriptPin.user_id == user.id))
        await db.execute(delete(AuditLog).where(AuditLog.user_id == user.id))
        await db.delete(user)
        add_audit_log(
            db,
            request,
            action="user.delete",
            resource_type="user",
            resource_id=user_id,
            summary=f"删除普通用户 {username} 及其执行数据",
            user=current_user,
        )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        restore_failures = restore_staged_artifact_dirs(staged_artifact_dirs)
        if restore_failures:
            raise RuntimeError("删除用户失败，且部分产物目录无法恢复") from exc
        raise

    cleanup_failures = purge_staged_artifact_dirs(staged_artifact_dirs)
    if cleanup_failures:
        return {
            "message": "用户及其执行数据已删除，部分产物文件将在服务重启后继续清理",
            "cleanup_pending": True,
        }
    return {"message": "用户及其执行数据已删除", "cleanup_pending": False}
