"""审计日志写入工具。"""
from typing import Optional

from fastapi import Request

from models import AuditLog, User


def add_audit_log(
    session,
    request: Request,
    *,
    action: str,
    resource_type: str = "",
    resource_id: object = "",
    summary: str = "",
    user: Optional[User] = None,
    username: str = "",
):
    session.add(
        AuditLog(
            user_id=user.id if user else None,
            username=user.username if user else username,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id is not None else "",
            summary=summary,
            ip_address=request.client.host if request.client else "",
            user_agent=(request.headers.get("user-agent") or "")[:512],
        )
    )
