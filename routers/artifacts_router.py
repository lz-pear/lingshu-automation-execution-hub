"""执行产物下载与预览路由。"""
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from auth import get_current_user
from models import ExecutionArtifact, ExecutionRecord, User
from services.artifact_service import (
    HTML_ARTIFACT_EXTENSIONS,
    build_preview_html,
    build_standalone_html,
    is_previewable_artifact,
    resolve_storage_path,
)
from services.audit_service import add_audit_log

router = APIRouter(prefix="/api/artifacts", tags=["执行产物"])


async def _load_artifact(artifact_id: int, db: AsyncSession, current_user: User) -> ExecutionArtifact:
    result = await db.execute(
        select(ExecutionArtifact, ExecutionRecord)
        .join(ExecutionRecord, ExecutionRecord.id == ExecutionArtifact.execution_id)
        .where(ExecutionArtifact.id == artifact_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="执行产物不存在")
    artifact, execution = row
    if current_user.role != "admin" and execution.created_by_user_id != current_user.id:
        raise HTTPException(status_code=404, detail="执行产物不存在")
    return artifact


def _get_artifact_file_path(artifact: ExecutionArtifact) -> Path:
    try:
        return resolve_storage_path(artifact.storage_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="执行产物路径非法") from exc


def _get_artifact_content_path(artifact: ExecutionArtifact, content_path: str) -> Path:
    artifact_path = _get_artifact_file_path(artifact)
    artifact_dir = artifact_path.parent.resolve()
    requested_path = (artifact_dir / content_path).resolve()
    try:
        requested_path.relative_to(artifact_dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="资源路径非法") from exc
    if not requested_path.exists() or not requested_path.is_file():
        raise HTTPException(status_code=404, detail="资源不存在")
    return requested_path


@router.get("/{artifact_id}/download", summary="下载执行产物")
async def download_artifact(
    artifact_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    artifact = await _load_artifact(artifact_id, db, current_user)
    file_path = _get_artifact_file_path(artifact)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="执行产物文件不存在")
    add_audit_log(db, request, action="artifact.download", resource_type="artifact", resource_id=artifact.id, summary=f"下载产物 {artifact.file_name}", user=current_user)
    await db.commit()
    if file_path.suffix.lower() in HTML_ARTIFACT_EXTENSIONS:
        content = build_standalone_html(file_path)
        return Response(
            content=content.encode("utf-8"),
            media_type="text/html",
            headers={"Content-Disposition": f"attachment; filename*=utf-8''{quote(artifact.file_name, safe='')}"},
        )
    return FileResponse(path=file_path, media_type=artifact.mime_type or None, filename=artifact.file_name)


@router.get("/{artifact_id}/preview", summary="预览HTML产物")
async def preview_artifact(
    artifact_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    artifact = await _load_artifact(artifact_id, db, current_user)
    file_path = _get_artifact_file_path(artifact)
    if file_path.suffix.lower() not in HTML_ARTIFACT_EXTENSIONS or not is_previewable_artifact(file_path):
        raise HTTPException(status_code=400, detail="当前产物不支持预览")
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="执行产物文件不存在")

    base_href = f"/api/artifacts/{artifact.id}/content/"
    content = build_preview_html(file_path, base_href)
    add_audit_log(db, request, action="artifact.preview", resource_type="artifact", resource_id=artifact.id, summary=f"预览产物 {artifact.file_name}", user=current_user)
    await db.commit()
    return HTMLResponse(
        content=content,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "sandbox allow-scripts allow-forms allow-downloads allow-popups",
        },
    )


@router.get("/{artifact_id}/content/{content_path:path}", summary="HTML产物关联资源")
async def preview_artifact_content(
    artifact_id: int,
    content_path: str,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(ExecutionArtifact).where(ExecutionArtifact.id == artifact_id))
    artifact = result.scalar_one_or_none()
    if not artifact:
        raise HTTPException(status_code=404, detail="资源不存在")
    requested_path = _get_artifact_content_path(artifact, content_path)

    media_type = None
    if requested_path.suffix.lower() in HTML_ARTIFACT_EXTENSIONS:
        media_type = "text/html"
    return FileResponse(
        path=requested_path,
        media_type=media_type,
        filename=requested_path.name,
        content_disposition_type="inline",
    )
