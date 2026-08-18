"""运行配置：优先从环境变量读取，方便部署。"""
import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent


def _load_deploy_env() -> None:
    """加载项目内的本地配置，外部环境变量优先。"""
    env_path = PROJECT_ROOT / "deploy.env"
    if not env_path.is_file():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name:
            os.environ.setdefault(name, value.strip())


_load_deploy_env()


def _get_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _get_list(name: str, default: list[str]) -> list[str]:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    return [item.strip() for item in value.split(",") if item.strip()]


def _get_path(name: str, default: Path, *, base_dir: Path | None = None) -> Path:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    path = Path(value).expanduser()
    if not path.is_absolute() and base_dir is not None:
        path = base_dir / path
    return path


APP_HOST = os.getenv("PLATFORM_HOST", "0.0.0.0")
APP_PORT = _get_int("PLATFORM_PORT", 5002)
APP_RELOAD = _get_bool("PLATFORM_RELOAD", True)
LOCAL_SHELL = os.getenv("PLATFORM_LOCAL_SHELL", "/bin/bash").strip() or "/bin/bash"

ADMIN_USERNAME = os.getenv("PLATFORM_ADMIN_USERNAME", "admin").strip() or "admin"
ADMIN_DISPLAY_NAME = os.getenv("PLATFORM_ADMIN_DISPLAY_NAME", "管理员").strip() or "管理员"
ADMIN_PASSWORD = os.getenv("PLATFORM_ADMIN_PASSWORD", "").strip()
SESSION_COOKIE_NAME = os.getenv("PLATFORM_SESSION_COOKIE_NAME", "platform_session").strip() or "platform_session"
SESSION_COOKIE_SECURE = _get_bool("PLATFORM_SESSION_COOKIE_SECURE", False)
SESSION_MAX_AGE_SECONDS = _get_int("PLATFORM_SESSION_MAX_AGE_SECONDS", 7 * 24 * 60 * 60)
LOGIN_MAX_FAILURES = _get_int("PLATFORM_LOGIN_MAX_FAILURES", 5)
LOGIN_LOCK_SECONDS = _get_int("PLATFORM_LOGIN_LOCK_SECONDS", 10 * 60)

CORS_ORIGINS = _get_list(
    "PLATFORM_CORS_ORIGINS",
    [],
)

ARTIFACT_STORAGE_ROOT = _get_path(
    "PLATFORM_ARTIFACT_STORAGE_ROOT",
    PROJECT_ROOT / "storage" / "artifacts",
    base_dir=PROJECT_ROOT,
)
REMOTE_ARTIFACT_ROOT = os.getenv(
    "PLATFORM_REMOTE_ARTIFACT_ROOT",
    "/tmp/automated-script-platform",
).strip() or "/tmp/automated-script-platform"

# 脚本文件管理。所有本机文件操作都限制在这个受控根目录内。
LOCAL_SCRIPT_ROOT = _get_path(
    "PLATFORM_LOCAL_SCRIPT_ROOT",
    PROJECT_ROOT / "storage" / "local-scripts",
    base_dir=PROJECT_ROOT,
)
SCRIPT_FILE_TEMP_ROOT = _get_path(
    "PLATFORM_SCRIPT_FILE_TEMP_ROOT",
    PROJECT_ROOT / "storage" / "script-file-temp",
    base_dir=PROJECT_ROOT,
)
SCRIPT_FILE_MAX_SIZE = _get_int("PLATFORM_SCRIPT_FILE_MAX_SIZE", 20 * 1024 * 1024)
SCRIPT_FILE_MAX_COUNT = _get_int("PLATFORM_SCRIPT_FILE_MAX_COUNT", 500)
SCRIPT_FILE_MAX_TOTAL_SIZE = _get_int("PLATFORM_SCRIPT_FILE_MAX_TOTAL_SIZE", 100 * 1024 * 1024)
