"""登录、当前用户与退出。"""
import math
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user
from config import (
    LOGIN_LOCK_SECONDS,
    LOGIN_MAX_FAILURES,
    SESSION_COOKIE_NAME,
    SESSION_COOKIE_SECURE,
    SESSION_MAX_AGE_SECONDS,
)
from database import get_db
from models import AuthSession, LoginAttempt, User
from schemas import LoginRequest, UserResponse
from services.audit_service import add_audit_log
from services.auth_service import (
    create_session_token,
    hash_session_token,
    normalize_username,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["登录认证"])


def _login_lock_detail(seconds: float) -> str:
    remaining_seconds = max(math.ceil(seconds), 1)
    if remaining_seconds >= 60:
        return f"登录失败次数过多，请{math.ceil(remaining_seconds / 60)}分钟后再试"
    return f"登录失败次数过多，请{remaining_seconds}秒后再试"


def _user_response(user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        username=user.username,
        display_name=user.display_name or user.username,
        role=user.role,
        is_fixed_admin=user.role == "admin",
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


@router.post("/login", response_model=UserResponse, summary="登录")
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    username = normalize_username(payload.username)
    if not username or payload.password == "":
        raise HTTPException(status_code=400, detail="用户名和密码不能为空")

    now = datetime.now()
    attempt_result = await db.execute(
        select(LoginAttempt).where(func.lower(LoginAttempt.identifier) == username.lower())
    )
    attempt = attempt_result.scalar_one_or_none()
    if attempt and attempt.locked_until and attempt.locked_until > now:
        raise HTTPException(
            status_code=429,
            detail=_login_lock_detail((attempt.locked_until - now).total_seconds()),
        )
    if attempt and attempt.locked_until and attempt.locked_until <= now:
        attempt.failure_count = 0
        attempt.locked_until = None

    user_result = await db.execute(
        select(User).where(func.lower(User.username) == username.lower())
    )
    user = user_result.scalar_one_or_none()
    if not user or not verify_password(payload.password, user.password_hash):
        if not attempt:
            attempt = LoginAttempt(identifier=username, failure_count=0)
            db.add(attempt)
        attempt.failure_count = (attempt.failure_count or 0) + 1
        if attempt.failure_count >= LOGIN_MAX_FAILURES:
            attempt.locked_until = now + timedelta(seconds=LOGIN_LOCK_SECONDS)
        add_audit_log(
            db,
            request,
            action="login.failed",
            resource_type="user",
            summary="用户名或密码错误",
            username=username,
        )
        await db.commit()
        if attempt.locked_until:
            raise HTTPException(
                status_code=429,
                detail=_login_lock_detail(LOGIN_LOCK_SECONDS),
            )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")

    if attempt:
        await db.delete(attempt)

    token, token_hash = create_session_token()
    db.add(
        AuthSession(
            user_id=user.id,
            token_hash=token_hash,
            auth_version=user.auth_version,
        )
    )
    add_audit_log(
        db,
        request,
        action="login.success",
        resource_type="user",
        resource_id=user.id,
        summary="登录成功",
        user=user,
    )
    await db.commit()

    cookie_kwargs = {
        "key": SESSION_COOKIE_NAME,
        "value": token,
        "httponly": True,
        "secure": SESSION_COOKIE_SECURE,
        "samesite": "lax",
        "path": "/",
    }
    if payload.remember_password:
        cookie_kwargs["max_age"] = SESSION_MAX_AGE_SECONDS
    response.set_cookie(**cookie_kwargs)
    return _user_response(user)


@router.get("/me", response_model=UserResponse, summary="当前用户")
async def me(current_user: User = Depends(get_current_user)):
    return _user_response(current_user)


@router.post("/logout", summary="退出登录")
async def logout(
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    if token:
        await db.execute(
            delete(AuthSession).where(AuthSession.token_hash == hash_session_token(token))
        )
    add_audit_log(
        db,
        request,
        action="logout",
        resource_type="user",
        resource_id=current_user.id,
        summary="退出登录",
        user=current_user,
    )
    await db.commit()
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return {"message": "已退出登录"}
