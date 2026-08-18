"""服务器管理路由（无需认证）"""
from typing import List
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from auth import require_admin
from models import Server, Script, ScriptFileRoot, ScriptFileResource, User
from schemas import ServerCreate, ServerUpdate, ServerResponse, ServerDetailResponse
from services.audit_service import add_audit_log

router = APIRouter(prefix="/api/servers", tags=["服务器管理"])


@router.get("/", response_model=List[ServerResponse], summary="获取服务器列表")
async def list_servers(_: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Server).order_by(Server.created_at.desc()))
    return result.scalars().all()


@router.post("/", response_model=ServerResponse, summary="创建服务器")
async def create_server(
    server_in: ServerCreate,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    server = Server(**server_in.model_dump())
    db.add(server)
    await db.flush()
    add_audit_log(db, request, action="server.create", resource_type="server", resource_id=server.id, summary=f"创建服务器 {server.name}", user=current_user)
    await db.commit()
    await db.refresh(server)
    return server


@router.get("/{server_id}", response_model=ServerDetailResponse, summary="获取服务器详情")
async def get_server(server_id: int, _: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Server).where(Server.id == server_id))
    server = result.scalar_one_or_none()
    if not server:
        raise HTTPException(status_code=404, detail="服务器不存在")
    return server


@router.put("/{server_id}", response_model=ServerResponse, summary="更新服务器")
async def update_server(
    server_id: int,
    server_in: ServerUpdate,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Server).where(Server.id == server_id))
    server = result.scalar_one_or_none()
    if not server:
        raise HTTPException(status_code=404, detail="服务器不存在")
    update_data = server_in.model_dump(exclude_unset=True)
    target_fields = {"hostname", "port", "username", "ssh_host_key"}
    target_changes = {
        field for field in target_fields
        if field in update_data and getattr(server, field) != update_data[field]
    }
    if target_changes:
        root_in_use = await db.execute(
            select(ScriptFileRoot.id).where(ScriptFileRoot.server_id == server_id).limit(1)
        )
        if root_in_use.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="服务器已配置脚本文件根目录，不能修改主机、端口、SSH用户或主机公钥")
    for field, value in update_data.items():
        setattr(server, field, value)
    add_audit_log(db, request, action="server.update", resource_type="server", resource_id=server.id, summary=f"更新服务器 {server.name}", user=current_user)
    await db.commit()
    await db.refresh(server)
    return server


@router.delete("/{server_id}", summary="删除服务器")
async def delete_server(
    server_id: int,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Server).where(Server.id == server_id))
    server = result.scalar_one_or_none()
    if not server:
        raise HTTPException(status_code=404, detail="服务器不存在")

    script_count_result = await db.execute(
        select(func.count()).select_from(Script).where(Script.server_id == server_id)
    )
    script_count = script_count_result.scalar_one()
    if script_count:
        raise HTTPException(
            status_code=400,
            detail=f"服务器“{server.name}”仍被 {script_count} 个脚本使用，不能删除",
        )

    file_root_count_result = await db.execute(
        select(func.count()).select_from(ScriptFileRoot).where(ScriptFileRoot.server_id == server_id)
    )
    file_root_count = file_root_count_result.scalar_one()
    if file_root_count:
        raise HTTPException(
            status_code=400,
            detail=f"服务器“{server.name}”仍被 {file_root_count} 个脚本文件根目录使用，不能删除",
        )

    add_audit_log(db, request, action="server.delete", resource_type="server", resource_id=server.id, summary=f"删除服务器 {server.name}", user=current_user)
    await db.delete(server)
    await db.commit()
    return {"message": "服务器已删除"}
