"""管理员脚本文件管理接口。"""
import json
from datetime import datetime
from pathlib import PurePosixPath
import shutil
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from auth import require_admin
from database import get_db
from models import ScriptFileResource, ScriptFileRoot, Server, User
from schemas import (
    ScriptFileResourceMetadataUpdate, ScriptFileResourceResponse, ScriptFileRootCreate, ScriptFileRootResponse, ScriptFileRootUpdate,
)
from services.audit_service import add_audit_log
from services.script_file_service import (
    delete_target, ensure_resource_not_running, prepare_download,
    recover_resource_operation,
    resource_lock, sanitize_upload_relative_path, transfer_upload, validate_resource_target, validate_ssh_root_path,
)

router = APIRouter(prefix="/api/script-files", tags=["脚本文件管理"])


AUDIT_ACTION_LABELS = {
    "script_file.upload": "上传脚本文件",
    "script_file.update": "更新脚本文件",
    "script_file.metadata.update": "修改脚本文件信息",
}


def _root_response(root: ScriptFileRoot, server_name: str = "") -> ScriptFileRootResponse:
    return ScriptFileRootResponse(
        id=root.id, name=root.name, target_type=root.target_type, server_id=root.server_id,
        server_name=server_name, root_path=root.root_path, enabled=root.enabled,
        created_at=root.created_at, updated_at=root.updated_at,
    )


def _target_path(root: ScriptFileRoot, resource: ScriptFileResource) -> str:
    if root.target_type == "ssh":
        return f"{root.root_path.rstrip('/')}/{resource.relative_path}"
    return str(PurePosixPath(root.root_path) / PurePosixPath(resource.relative_path))


def _resource_response(
    resource: ScriptFileResource, root: ScriptFileRoot, server_name: str = "", *, include_error: bool = False,
) -> ScriptFileResourceResponse:
    return ScriptFileResourceResponse(
        id=resource.id, name=resource.name, description=resource.description,
        resource_type=resource.resource_type, root_id=root.id, target_type=root.target_type,
        server_id=root.server_id, server_name=server_name, root_name=root.name, root_path=root.root_path,
        relative_path=resource.relative_path, target_path=_target_path(root, resource),
        last_success_file_name=resource.last_success_file_name,
        last_success_file_count=resource.last_success_file_count,
        last_success_total_size=resource.last_success_total_size,
        last_success_sha256=resource.last_success_sha256,
        last_success_at=resource.last_success_at,
        last_success_by_username=resource.last_success_by_username,
        last_attempt_status=resource.last_attempt_status,
        last_attempt_at=resource.last_attempt_at,
        last_attempt_by_username=resource.last_attempt_by_username,
        # 列表只需要状态；完整异常可能很大，仅在管理员主动查看详情时返回。
        last_error=resource.last_error if include_error else "", created_by_username=resource.created_by_username,
        created_at=resource.created_at, updated_at=resource.updated_at,
    )


async def _root_and_server(db: AsyncSession, root_id: int, *, require_enabled: bool = True):
    root = (await db.execute(select(ScriptFileRoot).where(ScriptFileRoot.id == root_id))).scalar_one_or_none()
    if not root:
        raise HTTPException(status_code=404, detail="脚本文件根目录不存在")
    if require_enabled and not root.enabled:
        raise HTTPException(status_code=400, detail="脚本文件根目录已停用")
    server = None
    if root.target_type == "ssh":
        server = (await db.execute(select(Server).where(Server.id == root.server_id))).scalar_one_or_none()
        if not server:
            raise HTTPException(status_code=400, detail="关联的SSH服务器不存在")
    return root, server


async def _resource_context(db: AsyncSession, resource_id: int):
    resource = (await db.execute(select(ScriptFileResource).where(ScriptFileResource.id == resource_id))).scalar_one_or_none()
    if not resource:
        raise HTTPException(status_code=404, detail="脚本文件管理记录不存在")
    root, server = await _root_and_server(db, resource.root_id)
    return resource, root, server


async def _ensure_no_path_overlap(db: AsyncSession, root_id: int, relative_path: str, *, ignored_id: int | None = None):
    resources = (await db.execute(select(ScriptFileResource).where(ScriptFileResource.root_id == root_id))).scalars().all()
    for resource in resources:
        if resource.id == ignored_id:
            continue
        existing = resource.relative_path.rstrip("/")
        candidate = relative_path.rstrip("/")
        if existing == candidate or existing.startswith(f"{candidate}/") or candidate.startswith(f"{existing}/"):
            raise HTTPException(status_code=409, detail="目标路径与已有管理文件或文件夹重叠")


def _parse_relative_paths(raw: str) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="文件夹相对路径清单格式错误")
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise HTTPException(status_code=400, detail="文件夹相对路径清单格式错误")
    return value


async def _mark_failed(db, request, user, resource, action: str, error: str):
    resource.last_attempt_status = "failed"
    resource.last_attempt_at = datetime.now()
    resource.last_attempt_by_username = user.username
    # 最近一次失败原因用于管理员排障，必须保留完整内容；审计摘要仍做截断，避免审计列表膨胀。
    resource.last_error = str(error)
    resource.operation_id = ""
    resource.operation_phase = ""
    add_audit_log(
        db, request, action=action, resource_type="script_file", resource_id=resource.id,
        summary=f"{AUDIT_ACTION_LABELS.get(action, action)}失败：{resource.name}，原因：{str(error)[:400]}", user=user,
    )
    await db.commit()


async def _run_transfer(db, request, user, resource, root, server, uploads, relative_paths, action: str):
    async with resource_lock(resource.id):
        # 先收口此前成功切换但未清掉备份的现场；恢复失败时保留锁定状态。
        if resource.operation_id:
            recovered = await recover_resource_operation(root, resource, server)
            if not recovered:
                resource.operation_phase = "recovery_pending"
                await db.commit()
                raise HTTPException(status_code=409, detail="上次传输中断尚未完成恢复，请先检查目标机器")
            resource.operation_id = ""
            resource.operation_phase = ""
            if resource.last_attempt_status == "success":
                resource.last_error = ""
            await db.commit()
        try:
            await ensure_resource_not_running(db, root, resource)
            resource.last_attempt_status = "uploading"
            resource.last_attempt_at = datetime.now()
            resource.last_attempt_by_username = user.username
            resource.last_error = ""
            resource.operation_id = __import__("uuid").uuid4().hex
            resource.operation_phase = "staging"
            await db.commit()
            summary = await transfer_upload(root, resource, server, uploads, relative_paths)
        except HTTPException as exc:
            await _mark_failed(db, request, user, resource, action, exc.detail)
            raise
        except Exception as exc:
            await _mark_failed(db, request, user, resource, action, f"传输失败：{exc}")
            raise HTTPException(status_code=500, detail="脚本文件传输失败，请查看最近上传结果")
        resource.last_success_file_name = summary.file_name
        resource.last_success_file_count = summary.file_count
        resource.last_success_total_size = summary.total_size
        resource.last_success_sha256 = summary.sha256
        resource.last_success_at = datetime.now()
        resource.last_success_by_username = user.username
        resource.last_attempt_status = "success"
        resource.last_attempt_at = datetime.now()
        resource.last_attempt_by_username = user.username
        resource.last_error = summary.cleanup_warning
        if summary.cleanup_warning:
            # 正式目标已经切换成功；保留标识，后续操作或重启时可继续清理旧备份。
            resource.operation_phase = "cleanup_pending"
        else:
            resource.operation_id = ""
            resource.operation_phase = ""
        result_text = "成功" if not summary.cleanup_warning else "成功（备份清理待处理）"
        action_label = AUDIT_ACTION_LABELS.get(action, action)
        add_audit_log(
            db, request, action=action, resource_type="script_file", resource_id=resource.id,
            summary=f"{action_label}{result_text}：{resource.name}，路径：{_target_path(root, resource)}，{summary.file_count} 个文件，{summary.total_size} 字节",
            user=user,
        )
        await db.commit()
        return _resource_response(resource, root, server.name if server else "")


@router.get("/roots", response_model=list[ScriptFileRootResponse], summary="获取脚本文件根目录")
async def list_roots(current_user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    roots = (await db.execute(select(ScriptFileRoot).order_by(ScriptFileRoot.target_type, ScriptFileRoot.name))).scalars().all()
    servers = (await db.execute(select(Server))).scalars().all()
    names = {server.id: server.name for server in servers}
    return [_root_response(root, names.get(root.server_id, "")) for root in roots]


@router.post("/roots", response_model=ScriptFileRootResponse, summary="创建SSH脚本文件根目录")
async def create_root(
    root_in: ScriptFileRootCreate, request: Request, current_user: User = Depends(require_admin), db: AsyncSession = Depends(get_db),
):
    server = (await db.execute(select(Server).where(Server.id == root_in.server_id))).scalar_one_or_none()
    if not server:
        raise HTTPException(status_code=400, detail="关联的SSH服务器不存在")
    if not (server.ssh_host_key or "").strip():
        raise HTTPException(status_code=400, detail="请先在服务器配置中填写固定SSH主机公钥")
    root_path = validate_ssh_root_path(root_in.root_path)
    duplicated = (await db.execute(
        select(ScriptFileRoot).where(ScriptFileRoot.target_type == "ssh", ScriptFileRoot.server_id == server.id, ScriptFileRoot.root_path == root_path)
    )).scalar_one_or_none()
    if duplicated:
        raise HTTPException(status_code=409, detail="该服务器已配置相同的脚本文件根目录")
    root = ScriptFileRoot(name=root_in.name.strip(), target_type="ssh", server_id=server.id, root_path=root_path, enabled=True)
    db.add(root)
    await db.flush()
    add_audit_log(db, request, action="script_file.root.create", resource_type="script_file_root", resource_id=root.id, summary=f"创建SSH脚本文件根目录 {root.name}：{root.root_path}", user=current_user)
    await db.commit()
    await db.refresh(root)
    return _root_response(root, server.name)


@router.put("/roots/{root_id}", response_model=ScriptFileRootResponse, summary="更新脚本文件根目录")
async def update_root(
    root_id: int, root_in: ScriptFileRootUpdate, request: Request, current_user: User = Depends(require_admin), db: AsyncSession = Depends(get_db),
):
    root, server = await _root_and_server(db, root_id, require_enabled=False)
    for field, value in root_in.model_dump(exclude_unset=True).items():
        setattr(root, field, value)
    add_audit_log(db, request, action="script_file.root.update", resource_type="script_file_root", resource_id=root.id, summary=f"更新脚本文件根目录 {root.name}", user=current_user)
    await db.commit()
    await db.refresh(root)
    return _root_response(root, server.name if server else "")


@router.delete("/roots/{root_id}", summary="删除SSH脚本文件根目录")
async def delete_root(root_id: int, request: Request, current_user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    root, _ = await _root_and_server(db, root_id, require_enabled=False)
    if root.target_type == "local":
        raise HTTPException(status_code=400, detail="平台本机脚本文件根目录由环境变量管理，不能删除")
    used = (await db.execute(select(ScriptFileResource.id).where(ScriptFileResource.root_id == root.id).limit(1))).scalar_one_or_none()
    if used:
        raise HTTPException(status_code=409, detail="根目录仍被脚本文件管理记录使用，不能删除")
    add_audit_log(db, request, action="script_file.root.delete", resource_type="script_file_root", resource_id=root.id, summary=f"删除SSH脚本文件根目录 {root.name}", user=current_user)
    await db.delete(root)
    await db.commit()
    return {"message": "脚本文件根目录已删除"}


@router.get("/", response_model=list[ScriptFileResourceResponse], summary="获取脚本文件列表")
async def list_resources(current_user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    resources = (await db.execute(select(ScriptFileResource).order_by(ScriptFileResource.updated_at.desc(), ScriptFileResource.id.desc()))).scalars().all()
    roots = (await db.execute(select(ScriptFileRoot))).scalars().all()
    servers = (await db.execute(select(Server))).scalars().all()
    root_map = {root.id: root for root in roots}
    server_names = {server.id: server.name for server in servers}
    return [
        _resource_response(resource, root_map[resource.root_id], server_names.get(root_map[resource.root_id].server_id, ""))
        for resource in resources if resource.root_id in root_map
    ]


@router.post("/upload", response_model=ScriptFileResourceResponse, summary="上传脚本文件或文件夹")
async def upload_resource(
    request: Request,
    name: Annotated[str, Form(...)], resource_type: Annotated[str, Form(...)], root_id: Annotated[int, Form(...)],
    files: Annotated[list[UploadFile], File(...)], description: Annotated[str, Form()] = "",
    relative_paths_json: Annotated[str, Form()] = "", source_name: Annotated[str, Form()] = "",
    relative_path: Annotated[str, Form()] = "",
    current_user: User = Depends(require_admin), db: AsyncSession = Depends(get_db),
):
    if resource_type not in {"file", "directory"}:
        raise HTTPException(status_code=400, detail="资源类型必须为文件或文件夹")
    clean_name = name.strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="名称不能为空")
    relative_paths = _parse_relative_paths(relative_paths_json)
    # 新建资源不再让用户填写目标路径：始终写入选定根目录的顶层。
    # relative_path 保留为兼容旧客户端字段，新的页面只提交 source_name。
    target_name = source_name.strip() or relative_path.strip()
    if not target_name:
        if resource_type == "file" and len(files) == 1:
            target_name = files[0].filename or ""
        elif resource_type == "directory" and relative_paths:
            target_name = relative_paths[0].replace("\\", "/").split("/", 1)[0]
    clean_path = sanitize_upload_relative_path(target_name)
    if "/" in clean_path:
        raise HTTPException(status_code=400, detail="目标名称必须是根目录下的单层文件或文件夹名称")
    validate_resource_target(resource_type, clean_path)
    root, server = await _root_and_server(db, root_id)
    await _ensure_no_path_overlap(db, root.id, clean_path)
    resource = ScriptFileResource(
        name=clean_name, description=description.strip(), resource_type=resource_type, root_id=root.id,
        relative_path=clean_path, last_attempt_status="", created_by_username=current_user.username,
    )
    db.add(resource)
    await db.commit()
    await db.refresh(resource)
    return await _run_transfer(db, request, current_user, resource, root, server, files, relative_paths, "script_file.upload")


@router.get("/{resource_id}", response_model=ScriptFileResourceResponse, summary="获取脚本文件详情")
async def get_resource(resource_id: int, current_user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    resource, root, server = await _resource_context(db, resource_id)
    return _resource_response(resource, root, server.name if server else "", include_error=True)


@router.get("/{resource_id}/error", summary="获取脚本文件完整错误信息")
async def get_resource_error(resource_id: int, current_user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    resource = (await db.execute(
        select(ScriptFileResource).where(ScriptFileResource.id == resource_id)
    )).scalar_one_or_none()
    if not resource:
        raise HTTPException(status_code=404, detail="脚本文件管理记录不存在")
    return {"last_error": resource.last_error or ""}


@router.put("/{resource_id}/metadata", response_model=ScriptFileResourceResponse, summary="修改脚本文件名称和描述")
async def update_resource_metadata(
    resource_id: int,
    metadata_in: ScriptFileResourceMetadataUpdate,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    resource, root, server = await _resource_context(db, resource_id)
    resource.name = metadata_in.name
    resource.description = metadata_in.description
    add_audit_log(
        db, request, action="script_file.metadata.update", resource_type="script_file", resource_id=resource.id,
        summary=f"修改脚本文件信息：{resource.name}", user=current_user,
    )
    await db.commit()
    await db.refresh(resource)
    return _resource_response(resource, root, server.name if server else "")


@router.post("/{resource_id}/update", response_model=ScriptFileResourceResponse, summary="更新脚本文件或文件夹")
async def update_resource(
    resource_id: int, request: Request, files: Annotated[list[UploadFile], File(...)],
    relative_paths_json: Annotated[str, Form()] = "", current_user: User = Depends(require_admin), db: AsyncSession = Depends(get_db),
):
    resource, root, server = await _resource_context(db, resource_id)
    return await _run_transfer(db, request, current_user, resource, root, server, files, _parse_relative_paths(relative_paths_json), "script_file.update")


@router.get("/{resource_id}/download", summary="下载脚本文件或文件夹")
async def download_resource(resource_id: int, request: Request, current_user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    resource, root, server = await _resource_context(db, resource_id)
    async with resource_lock(resource.id):
        file_path, temp_dir, download_name = await prepare_download(root, resource, server)
    add_audit_log(db, request, action="script_file.download", resource_type="script_file", resource_id=resource.id, summary=f"开始下载：{resource.name}，路径：{_target_path(root, resource)}", user=current_user)
    await db.commit()
    return FileResponse(file_path, filename=download_name, background=BackgroundTask(shutil.rmtree, temp_dir, ignore_errors=True))


@router.delete("/{resource_id}", summary="删除脚本文件或文件夹")
async def delete_resource(resource_id: int, request: Request, current_user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    resource, root, server = await _resource_context(db, resource_id)
    async with resource_lock(resource.id):
        await ensure_resource_not_running(db, root, resource)
        try:
            message = await delete_target(root, resource, server)
        except HTTPException as exc:
            add_audit_log(db, request, action="script_file.delete", resource_type="script_file", resource_id=resource.id, summary=f"删除失败：{resource.name}，原因：{exc.detail}", user=current_user)
            await db.commit()
            raise
        except Exception as exc:
            add_audit_log(db, request, action="script_file.delete", resource_type="script_file", resource_id=resource.id, summary=f"删除失败：{resource.name}，原因：{str(exc)[:400]}", user=current_user)
            await db.commit()
            raise HTTPException(status_code=500, detail="删除脚本文件失败")
        add_audit_log(db, request, action="script_file.delete", resource_type="script_file", resource_id=resource.id, summary=f"{message}：{resource.name}，路径：{_target_path(root, resource)}", user=current_user)
        await db.delete(resource)
        await db.commit()
    return {"message": message}
