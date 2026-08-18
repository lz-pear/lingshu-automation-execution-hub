"""脚本文件管理的受控文件操作。文件内容只在传输期间暂存。"""
import asyncio
import base64
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
import hashlib
import logging
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import stat
import tempfile
import zipfile

import paramiko
from fastapi import HTTPException, UploadFile
from sqlalchemy import select

from config import (
    LOCAL_SCRIPT_ROOT, SCRIPT_FILE_MAX_COUNT,
    SCRIPT_FILE_MAX_SIZE, SCRIPT_FILE_MAX_TOTAL_SIZE, SCRIPT_FILE_TEMP_ROOT,
)
from database import async_session
from models import Script, ScriptFileResource, ScriptFileRoot, Server
from services.script_runner import is_script_running


EXECUTABLE_SUFFIXES = {".sh", ".bat", ".cmd", ".ps1"}
WINDOWS_INVALID_PATH_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
WINDOWS_PATH_COMPONENT_MAX_LENGTH = 80
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_resource_locks: dict[int, asyncio.Lock] = {}
logger = logging.getLogger(__name__)


@dataclass
class UploadSummary:
    file_name: str
    file_count: int
    total_size: int
    sha256: str
    cleanup_warning: str = ""


def _http_error(status: int, detail: str):
    raise HTTPException(status_code=status, detail=detail)


def normalize_relative_path(value: str) -> str:
    """仅接受根目录下的相对路径，统一以 / 保存。"""
    raw = (value or "").strip().replace("\\", "/")
    if not raw or "\x00" in raw:
        _http_error(400, "目标路径不能为空或包含非法字符")
    if raw.startswith("//") or re.match(r"^[A-Za-z]:($|/)", raw):
        _http_error(400, "目标路径必须相对于受控根目录，不能填写Windows绝对路径")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        _http_error(400, "目标路径必须是受控根目录下的有效相对路径")
    return path.as_posix()


def sanitize_upload_relative_path(value: str) -> str:
    """将浏览器文件夹清单转换为可写入 Windows 暂存目录的安全路径。"""
    relative = normalize_relative_path(value)
    safe_parts = []
    for part in PurePosixPath(relative).parts:
        safe_part = WINDOWS_INVALID_PATH_CHARS.sub("_", part).rstrip(". ")
        if not safe_part:
            safe_part = "_"
        stem = safe_part.split(".", 1)[0].upper()
        if stem in WINDOWS_RESERVED_NAMES:
            safe_part = f"_{safe_part}"
        if len(safe_part) > WINDOWS_PATH_COMPONENT_MAX_LENGTH:
            suffix = Path(safe_part).suffix[:20]
            digest = hashlib.sha256(part.encode("utf-8")).hexdigest()[:12]
            head_length = WINDOWS_PATH_COMPONENT_MAX_LENGTH - len(suffix) - len(digest) - 1
            safe_part = f"{safe_part[:max(head_length, 1)]}_{digest}{suffix}"
        safe_parts.append(safe_part)
    return PurePosixPath(*safe_parts).as_posix()


def validate_ssh_root_path(value: str) -> str:
    raw = (value or "").strip()
    if not raw or "\x00" in raw or not raw.startswith("/"):
        _http_error(400, "SSH脚本文件根目录必须是绝对路径")
    normalized = posixpath.normpath(raw)
    if normalized == "/" or "/../" in f"/{normalized}/":
        _http_error(400, "SSH脚本文件根目录不合法")
    return normalized


def _validate_uploaded_file_name(name: str):
    leaf = Path(name).name
    if not leaf or leaf in {".", ".."} or "\x00" in leaf:
        _http_error(400, "上传文件名不合法")


def validate_resource_target(resource_type: str, relative_path: str):
    """校验管理员填写的目标名称，不能只信任上传源文件名称。"""
    if resource_type == "file":
        _validate_uploaded_file_name(PurePosixPath(relative_path).name)


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _manifest_for_tree(root: Path) -> UploadSummary:
    entries = []
    total = 0
    count = 0
    for item in sorted(root.rglob("*"), key=lambda value: value.as_posix()):
        if item.is_dir():
            continue
        if item.is_symlink():
            _http_error(400, "上传目录中不允许包含符号链接")
        size, digest = _hash_file(item)
        count += 1
        total += size
        entries.append((item.relative_to(root).as_posix(), digest))
    if count == 0:
        _http_error(400, "上传文件夹不能为空；浏览器目录选择不保留空目录")
    digest = hashlib.sha256()
    for relative, file_digest in entries:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_digest.encode("ascii"))
        digest.update(b"\0")
    return UploadSummary("", count, total, digest.hexdigest())


def _directory_payload_root(staging_root: Path) -> Path:
    """浏览器目录选择应只产生一个顶层目录，复制时不能再嵌套一层。"""
    entries = list(staging_root.iterdir())
    if len(entries) != 1 or not entries[0].is_dir():
        _http_error(400, "上传文件夹结构异常，请重新选择一个完整文件夹")
    return entries[0]


async def _stage_uploads(
    uploads: list[UploadFile], relative_paths: list[str], resource_type: str,
) -> tuple[tempfile.TemporaryDirectory, Path, UploadSummary]:
    if resource_type not in {"file", "directory"}:
        _http_error(400, "资源类型必须为文件或文件夹")
    if not uploads:
        _http_error(400, "请选择要上传的文件")
    if resource_type == "file" and len(uploads) != 1:
        _http_error(400, "单文件上传只能包含一个文件")
    if resource_type == "directory" and len(uploads) > SCRIPT_FILE_MAX_COUNT:
        _http_error(400, f"文件夹最多包含 {SCRIPT_FILE_MAX_COUNT} 个文件")
    if relative_paths and len(relative_paths) != len(uploads):
        _http_error(400, "目录相对路径清单与上传文件不一致")

    SCRIPT_FILE_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    temp_dir = tempfile.TemporaryDirectory(prefix="upload-", dir=str(SCRIPT_FILE_TEMP_ROOT))
    try:
        staging_root = Path(temp_dir.name) / "payload"
        staging_root.mkdir()
        total = 0
        seen_paths = set()
        for index, upload in enumerate(uploads):
            source_name = upload.filename or ""
            relative = relative_paths[index] if relative_paths else source_name
            relative = sanitize_upload_relative_path(relative)
            if relative in seen_paths:
                _http_error(400, "上传文件夹中包含重复的相对路径")
            seen_paths.add(relative)
            if resource_type == "file" and "/" in relative:
                _http_error(400, "单文件上传不允许包含目录层级")
            _validate_uploaded_file_name(PurePosixPath(relative).name)
            destination = staging_root.joinpath(*PurePosixPath(relative).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            size = 0
            with destination.open("wb") as output:
                while chunk := await upload.read(1024 * 1024):
                    size += len(chunk)
                    total += len(chunk)
                    if size > SCRIPT_FILE_MAX_SIZE:
                        _http_error(400, f"单文件不能超过 {SCRIPT_FILE_MAX_SIZE // 1024 // 1024} MB")
                    if total > SCRIPT_FILE_MAX_TOTAL_SIZE:
                        _http_error(400, f"文件夹总大小不能超过 {SCRIPT_FILE_MAX_TOTAL_SIZE // 1024 // 1024} MB")
                    output.write(chunk)
        if resource_type == "file":
            only_file = next(staging_root.iterdir())
            size, digest = _hash_file(only_file)
            return temp_dir, staging_root, UploadSummary(only_file.name, 1, size, digest)
        summary = _manifest_for_tree(_directory_payload_root(staging_root))
        if summary.total_size > SCRIPT_FILE_MAX_TOTAL_SIZE:
            _http_error(400, f"文件夹总大小不能超过 {SCRIPT_FILE_MAX_TOTAL_SIZE // 1024 // 1024} MB")
        return temp_dir, staging_root, summary
    except Exception:
        temp_dir.cleanup()
        raise


def _apply_permissions(path: Path, resource_type: str):
    if os.name == "nt":
        return
    if resource_type == "file":
        path.chmod(0o755 if path.suffix.lower() in EXECUTABLE_SUFFIXES else 0o644)
        return
    for item in path.rglob("*"):
        if item.is_dir():
            item.chmod(0o755)
        else:
            item.chmod(0o755 if item.suffix.lower() in EXECUTABLE_SUFFIXES else 0o644)
    path.chmod(0o755)


def _local_target(root_path: str, relative_path: str) -> Path:
    root = Path(root_path).resolve()
    root.mkdir(parents=True, exist_ok=True)
    candidate = (root / Path(*PurePosixPath(relative_path).parts)).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError:
        _http_error(400, "目标路径超出了允许的本机脚本文件根目录")
    return candidate


def _local_kind(path: Path) -> str | None:
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink():
        _http_error(409, "目标路径是符号链接，平台拒绝操作")
    return "directory" if path.is_dir() else "file"


def _replace_local(source: Path, target: Path, resource_type: str, operation_id: str) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    existing_kind = _local_kind(target)
    if existing_kind and existing_kind != resource_type:
        _http_error(409, "目标资源类型与管理记录不一致")
    # 必须使用数据库中记录的 operation_id；进程异常退出后才能定位这次操作的现场。
    token = operation_id
    staging = target.parent / f".platform-upload-{token}"
    backup = target.parent / f".platform-backup-{token}"
    cleanup_warning = ""
    try:
        if resource_type == "file":
            shutil.copy2(source, staging)
        else:
            shutil.copytree(source, staging)
        _apply_permissions(staging, resource_type)
        if target.exists():
            os.replace(target, backup)
        try:
            os.replace(staging, target)
        except Exception:
            if backup.exists() and not target.exists():
                os.replace(backup, target)
            raise
        if backup.exists():
            try:
                if backup.is_dir():
                    shutil.rmtree(backup)
                else:
                    backup.unlink()
            except Exception as exc:
                cleanup_warning = f"新内容已生效，但旧内容备份清理失败：{exc}"
        return cleanup_warning
    finally:
        if staging.exists():
            if staging.is_dir():
                shutil.rmtree(staging, ignore_errors=True)
            else:
                staging.unlink(missing_ok=True)


def _parse_host_key(value: str):
    parts = (value or "").strip().split()
    if len(parts) < 2 or not parts[0].startswith("ssh-"):
        _http_error(400, "服务器未配置有效的固定SSH主机公钥")
    try:
        data = base64.b64decode(parts[1].encode("ascii"), validate=True)
        key_type = parts[0]
        mapping = {
            "ssh-rsa": paramiko.RSAKey,
            "ssh-ed25519": paramiko.Ed25519Key,
            "ecdsa-sha2-nistp256": paramiko.ECDSAKey,
            "ecdsa-sha2-nistp384": paramiko.ECDSAKey,
            "ecdsa-sha2-nistp521": paramiko.ECDSAKey,
        }
        key_class = mapping.get(key_type)
        if not key_class:
            _http_error(400, "不支持的SSH主机公钥类型")
        return key_type, key_class(data=data)
    except HTTPException:
        raise
    except Exception as exc:
        _http_error(400, f"SSH主机公钥格式无效：{exc}")


def _open_ssh(server):
    key_type, host_key = _parse_host_key(server.ssh_host_key)
    client = paramiko.SSHClient()
    host_name = server.hostname if server.port == 22 else f"[{server.hostname}]:{server.port}"
    client.get_host_keys().add(host_name, key_type, host_key)
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect(
        hostname=server.hostname,
        port=server.port,
        username=server.username,
        password=server.password or None,
        timeout=15,
        auth_timeout=15,
    )
    return client


def _remote_join(root_path: str, relative_path: str) -> str:
    return posixpath.join(root_path, *PurePosixPath(relative_path).parts)


def _remote_kind(sftp, path: str) -> str | None:
    try:
        attrs = sftp.lstat(path)
    except IOError:
        return None
    if stat.S_ISLNK(attrs.st_mode):
        _http_error(409, "目标路径是符号链接，平台拒绝操作")
    return "directory" if stat.S_ISDIR(attrs.st_mode) else "file"


def _remote_mkdirs(sftp, path: str):
    current = "/" if path.startswith("/") else ""
    for part in [item for item in path.split("/") if item]:
        current = posixpath.join(current, part)
        try:
            attrs = sftp.lstat(current)
            if stat.S_ISLNK(attrs.st_mode) or not stat.S_ISDIR(attrs.st_mode):
                _http_error(409, "目标目录包含不安全路径")
        except IOError:
            sftp.mkdir(current)
            sftp.chmod(current, 0o755)


def _remote_put_tree(sftp, source: Path, destination: str, resource_type: str):
    if resource_type == "file":
        sftp.put(str(source), destination)
        sftp.chmod(destination, 0o755 if source.suffix.lower() in EXECUTABLE_SUFFIXES else 0o644)
        return
    sftp.mkdir(destination)
    sftp.chmod(destination, 0o755)
    for item in sorted(source.rglob("*"), key=lambda value: value.as_posix()):
        relative = item.relative_to(source).as_posix()
        remote_path = posixpath.join(destination, relative)
        if item.is_dir():
            sftp.mkdir(remote_path)
            sftp.chmod(remote_path, 0o755)
        else:
            sftp.put(str(item), remote_path)
            sftp.chmod(remote_path, 0o755 if item.suffix.lower() in EXECUTABLE_SUFFIXES else 0o644)


def _remote_remove_tree(sftp, path: str):
    kind = _remote_kind(sftp, path)
    if kind is None:
        return
    if kind == "file":
        sftp.remove(path)
        return
    for item in sftp.listdir_attr(path):
        child = posixpath.join(path, item.filename)
        if stat.S_ISLNK(item.st_mode):
            _http_error(409, "目录中包含符号链接，平台拒绝递归删除")
        if stat.S_ISDIR(item.st_mode):
            _remote_remove_tree(sftp, child)
        else:
            sftp.remove(child)
    sftp.rmdir(path)


def _replace_remote(
    server, root_path: str, relative_path: str, source: Path, resource_type: str, operation_id: str,
) -> str:
    client = _open_ssh(server)
    sftp = None
    staging = ""
    backup = ""
    target = ""
    moved_old_target = False
    switched = False
    cleanup_warning = ""
    try:
        sftp = client.open_sftp()
        if _remote_kind(sftp, root_path) != "directory":
            _http_error(400, "SSH脚本文件根目录不存在或不是目录")
        target = _remote_join(root_path, relative_path)
        _remote_mkdirs(sftp, posixpath.dirname(target))
        existing_kind = _remote_kind(sftp, target)
        if existing_kind and existing_kind != resource_type:
            _http_error(409, "目标资源类型与管理记录不一致")
        # 与数据库 operation_id 保持一致，以支持异常退出后的恢复。
        token = operation_id
        staging = posixpath.join(posixpath.dirname(target), f".platform-upload-{token}")
        backup = posixpath.join(posixpath.dirname(target), f".platform-backup-{token}")
        _remote_put_tree(sftp, source, staging, resource_type)
        if existing_kind:
            sftp.rename(target, backup)
            moved_old_target = True
        try:
            sftp.rename(staging, target)
        except Exception:
            if _remote_kind(sftp, backup) and not _remote_kind(sftp, target):
                sftp.rename(backup, target)
            raise
        switched = True
        if _remote_kind(sftp, backup):
            try:
                _remote_remove_tree(sftp, backup)
            except Exception as exc:
                cleanup_warning = f"新内容已生效，但旧内容备份清理失败：{exc}"
        return cleanup_warning
    except Exception:
        # 请求仍在线时优先收口现场，避免把失败操作遗留到下一次上传。
        try:
            if moved_old_target and not _remote_kind(sftp, target) and _remote_kind(sftp, backup):
                sftp.rename(backup, target)
            if staging and _remote_kind(sftp, staging):
                _remote_remove_tree(sftp, staging)
            if not switched and backup and _remote_kind(sftp, backup) and _remote_kind(sftp, target):
                _remote_remove_tree(sftp, backup)
        except Exception:
            # 保留原始异常；进程重启时会依据同一 operation_id 再次恢复。
            pass
        raise
    finally:
        if sftp:
            sftp.close()
        client.close()


def _local_root_contains_resources(root_path: Path, resources: list[ScriptFileResource]) -> bool:
    """确认新根目录能够完整接管已有资源记录，避免迁移到空目录。"""
    resolved_root = root_path.resolve()
    for resource in resources:
        try:
            relative = normalize_relative_path(resource.relative_path)
            target = resolved_root.joinpath(*PurePosixPath(relative).parts)
            if target.is_symlink():
                return False
            resolved_target = target.resolve(strict=False)
            resolved_target.relative_to(resolved_root)
        except (HTTPException, OSError, ValueError):
            return False
        if resource.resource_type == "directory":
            if not resolved_target.is_dir():
                return False
        elif resource.resource_type == "file":
            if not resolved_target.is_file():
                return False
        else:
            return False
    return True


async def ensure_local_script_root():
    """启动时确保唯一的本机受控根目录记录存在，并安全迁移旧部署路径。"""
    LOCAL_SCRIPT_ROOT.mkdir(parents=True, exist_ok=True)
    SCRIPT_FILE_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    async with async_session() as session:
        result = await session.execute(
            select(ScriptFileRoot).where(ScriptFileRoot.target_type == "local")
        )
        root = result.scalar_one_or_none()
        if not root:
            session.add(ScriptFileRoot(
                name="平台本机脚本目录", target_type="local", root_path=str(LOCAL_SCRIPT_ROOT), enabled=True,
            ))
        elif root.root_path != str(LOCAL_SCRIPT_ROOT):
            resources = (await session.execute(
                select(ScriptFileResource).where(ScriptFileResource.root_id == root.id)
            )).scalars().all()
            if _local_root_contains_resources(LOCAL_SCRIPT_ROOT, resources):
                logger.info("本机脚本根目录已从 %s 迁移到 %s", root.root_path, LOCAL_SCRIPT_ROOT)
                root.root_path = str(LOCAL_SCRIPT_ROOT)
            else:
                logger.warning(
                    "未迁移本机脚本根目录 %s：新目录 %s 缺少已登记资源或资源类型不一致",
                    root.root_path,
                    LOCAL_SCRIPT_ROOT,
                )
        await session.commit()


def _recover_local_operation(root_path: str, resource: ScriptFileResource):
    """应用异常后的本机暂存/备份恢复，永远优先保留已有正式目标。"""
    target = _local_target(root_path, resource.relative_path)
    operation_id = resource.operation_id
    if not operation_id:
        return True
    staging = target.parent / f".platform-upload-{operation_id}"
    backup = target.parent / f".platform-backup-{operation_id}"
    try:
        if not target.exists() and backup.exists():
            os.replace(backup, target)
        elif backup.exists():
            if backup.is_dir():
                shutil.rmtree(backup, ignore_errors=True)
            else:
                backup.unlink(missing_ok=True)
        if staging.exists():
            if staging.is_dir():
                shutil.rmtree(staging, ignore_errors=True)
            else:
                staging.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def _recover_remote_operation(server: Server, root_path: str, resource: ScriptFileResource) -> bool:
    """恢复 SSH 中断操作，始终优先保留已经存在的正式目标。"""
    operation_id = resource.operation_id
    if not operation_id:
        return True
    client = _open_ssh(server)
    sftp = None
    try:
        sftp = client.open_sftp()
        target = _remote_join(root_path, resource.relative_path)
        staging = posixpath.join(posixpath.dirname(target), f".platform-upload-{operation_id}")
        backup = posixpath.join(posixpath.dirname(target), f".platform-backup-{operation_id}")
        if _remote_kind(sftp, target) is None and _remote_kind(sftp, backup):
            sftp.rename(backup, target)
        elif _remote_kind(sftp, backup):
            _remote_remove_tree(sftp, backup)
        if _remote_kind(sftp, staging):
            _remote_remove_tree(sftp, staging)
        return True
    except Exception:
        return False
    finally:
        if sftp:
            sftp.close()
        client.close()


async def recover_resource_operation(root: ScriptFileRoot, resource: ScriptFileResource, server: Server | None) -> bool:
    """按记录的操作标识恢复本机或 SSH 现场。"""
    if root.target_type == "local":
        return await asyncio.to_thread(_recover_local_operation, root.root_path, resource)
    if not server:
        return False
    return await asyncio.to_thread(_recover_remote_operation, server, root.root_path, resource)


async def recover_incomplete_script_file_operations():
    """启动时收口中断操作；SSH 不可达时保留恢复标记，避免新操作覆盖现场。"""
    async with async_session() as session:
        resources = (await session.execute(
            select(ScriptFileResource).where(ScriptFileResource.operation_id != "")
        )).scalars().all()
        for resource in resources:
            root = (await session.execute(
                select(ScriptFileRoot).where(ScriptFileRoot.id == resource.root_id)
            )).scalar_one_or_none()
            if not root:
                resource.last_attempt_status = "failed"
                resource.last_error = "恢复失败：关联的脚本文件根目录不存在"
                resource.operation_phase = "recovery_pending"
                continue
            server = None
            if root.target_type == "ssh":
                server = (await session.execute(
                    select(Server).where(Server.id == root.server_id)
                )).scalar_one_or_none()
            recovered = await recover_resource_operation(root, resource, server)
            if recovered:
                if resource.operation_phase == "cleanup_pending":
                    # 正式目标已在上次请求中生效，本次只完成遗留备份清理。
                    resource.last_error = ""
                else:
                    resource.last_attempt_status = "failed"
                    resource.last_attempt_at = datetime.now()
                    resource.last_error = "上次传输在应用退出时中断，已清理暂存内容"
                resource.operation_id = ""
                resource.operation_phase = ""
            else:
                if resource.operation_phase == "cleanup_pending":
                    # 正式目标已经成功切换，仅保留旧备份的清理任务，不能误报上传失败。
                    resource.last_error = "上传已成功，但旧内容备份尚未清理，等待目标机器恢复连接后自动处理"
                else:
                    resource.last_attempt_status = "failed"
                    resource.last_attempt_at = datetime.now()
                    resource.last_error = "上次传输中断，等待管理员检查目标机器后恢复"
                resource.operation_phase = "recovery_pending"
        await session.commit()


@asynccontextmanager
async def resource_lock(resource_id: int):
    lock = _resource_locks.setdefault(resource_id, asyncio.Lock())
    async with lock:
        yield


async def ensure_resource_not_running(session, root: ScriptFileRoot, resource: ScriptFileResource):
    if root.target_type == "local":
        scripts = (await session.execute(select(Script).where(Script.script_type == "local"))).scalars().all()
        for script in scripts:
            if await is_script_running(session, script.id):
                _http_error(409, "存在本机脚本正在执行，暂不能修改本机脚本文件")
        return
    target = _remote_join(root.root_path, resource.relative_path)
    scripts = (await session.execute(
        select(Script).where(Script.script_type == "ssh", Script.server_id == root.server_id)
    )).scalars().all()
    for script in scripts:
        remote_path = posixpath.normpath(script.remote_path or "")
        if remote_path == target or (resource.resource_type == "directory" and remote_path.startswith(f"{target}/")):
            if await is_script_running(session, script.id):
                _http_error(409, f"关联脚本“{script.name}”正在执行，暂不能修改文件")


async def transfer_upload(
    root: ScriptFileRoot, resource: ScriptFileResource, server, uploads: list[UploadFile], relative_paths: list[str],
) -> UploadSummary:
    temp_dir, staging_root, summary = await _stage_uploads(uploads, relative_paths, resource.resource_type)
    try:
        source = next(staging_root.iterdir()) if resource.resource_type == "file" else _directory_payload_root(staging_root)
        if root.target_type == "local":
            cleanup_warning = await asyncio.to_thread(
                _replace_local, source, _local_target(root.root_path, resource.relative_path), resource.resource_type,
                resource.operation_id,
            )
        else:
            if not server:
                _http_error(400, "关联的SSH服务器不存在")
            cleanup_warning = await asyncio.to_thread(
                _replace_remote, server, root.root_path, resource.relative_path, source, resource.resource_type,
                resource.operation_id,
            )
        summary.cleanup_warning = cleanup_warning
        return summary
    finally:
        temp_dir.cleanup()


def _copy_local_target(target: Path, destination: Path, resource_type: str):
    kind = _local_kind(target)
    if kind is None:
        _http_error(404, "目标文件不存在")
    if kind != resource_type:
        _http_error(409, "目标资源类型与管理记录不一致")
    if resource_type == "file":
        shutil.copy2(target, destination)
        return
    shutil.copytree(target, destination)


def _remote_get_tree(sftp, source: str, destination: Path):
    destination.mkdir(parents=True, exist_ok=True)
    for item in sftp.listdir_attr(source):
        if stat.S_ISLNK(item.st_mode):
            _http_error(409, "目录中包含符号链接，平台拒绝下载")
        local_path = destination / item.filename
        remote_path = posixpath.join(source, item.filename)
        if stat.S_ISDIR(item.st_mode):
            _remote_get_tree(sftp, remote_path, local_path)
        else:
            sftp.get(remote_path, str(local_path))


def _prepare_remote_download(server, root_path: str, relative_path: str, resource_type: str, destination: Path):
    client = _open_ssh(server)
    sftp = None
    try:
        sftp = client.open_sftp()
        target = _remote_join(root_path, relative_path)
        kind = _remote_kind(sftp, target)
        if kind is None:
            _http_error(404, "目标文件不存在")
        if kind != resource_type:
            _http_error(409, "目标资源类型与管理记录不一致")
        if kind == "file":
            sftp.get(target, str(destination))
        else:
            _remote_get_tree(sftp, target, destination)
    finally:
        if sftp:
            sftp.close()
        client.close()


async def prepare_download(root: ScriptFileRoot, resource: ScriptFileResource, server) -> tuple[Path, Path, str]:
    SCRIPT_FILE_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix="download-", dir=str(SCRIPT_FILE_TEMP_ROOT)))
    try:
        if resource.resource_type == "file":
            output = temp_dir / PurePosixPath(resource.relative_path).name
        else:
            output = temp_dir / "folder"
        if root.target_type == "local":
            await asyncio.to_thread(_copy_local_target, _local_target(root.root_path, resource.relative_path), output, resource.resource_type)
        else:
            if not server:
                _http_error(400, "关联的SSH服务器不存在")
            await asyncio.to_thread(_prepare_remote_download, server, root.root_path, resource.relative_path, resource.resource_type, output)
        if resource.resource_type == "file":
            return output, temp_dir, output.name
        archive_base = temp_dir / PurePosixPath(resource.relative_path).name
        archive = Path(shutil.make_archive(str(archive_base), "zip", root_dir=output))
        return archive, temp_dir, archive.name
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def _delete_local(root_path: str, relative_path: str, resource_type: str):
    target = _local_target(root_path, relative_path)
    kind = _local_kind(target)
    if kind is None:
        return "目标已不存在"
    if kind != resource_type:
        _http_error(409, "目标资源类型与管理记录不一致")
    if kind == "directory":
        shutil.rmtree(target)
    else:
        target.unlink()
    return "目标已删除"


def _delete_remote(server, root_path: str, relative_path: str, resource_type: str):
    client = _open_ssh(server)
    sftp = None
    try:
        sftp = client.open_sftp()
        target = _remote_join(root_path, relative_path)
        kind = _remote_kind(sftp, target)
        if kind is None:
            return "目标已不存在"
        if kind != resource_type:
            _http_error(409, "目标资源类型与管理记录不一致")
        _remote_remove_tree(sftp, target)
        return "目标已删除"
    finally:
        if sftp:
            sftp.close()
        client.close()


async def delete_target(root: ScriptFileRoot, resource: ScriptFileResource, server) -> str:
    if root.target_type == "local":
        return await asyncio.to_thread(_delete_local, root.root_path, resource.relative_path, resource.resource_type)
    if not server:
        _http_error(400, "关联的SSH服务器不存在")
    return await asyncio.to_thread(_delete_remote, server, root.root_path, resource.relative_path, resource.resource_type)
