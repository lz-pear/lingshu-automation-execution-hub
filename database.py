"""数据库配置、兼容迁移与初始化。"""
import os
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from config import _load_deploy_env


_load_deploy_env()

def _get_async_database_url() -> str:
    """获取 PostgreSQL 异步 SQLAlchemy 连接串。"""
    configured_url = os.getenv("PLATFORM_DATABASE_URL", "").strip()
    if not configured_url:
        raise RuntimeError("必须设置 PLATFORM_DATABASE_URL 为 PostgreSQL 连接串。")

    if configured_url.startswith("postgres://"):
        configured_url = f"postgresql://{configured_url[len('postgres://'):]}"
    if configured_url.startswith("postgresql://"):
        return f"postgresql+asyncpg://{configured_url[len('postgresql://'):]}"
    if configured_url.startswith("postgresql+asyncpg://"):
        return configured_url
    raise RuntimeError("PLATFORM_DATABASE_URL 必须使用 postgresql+asyncpg:// 或 postgresql:// 格式。")


ASYNC_DB_URL = _get_async_database_url()

engine = create_async_engine(ASYNC_DB_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def _sync_migrate(sync_conn):
    """为现有 PostgreSQL 数据库补齐缺失字段和索引。"""
    inspector = inspect(sync_conn)
    table_names = set(inspector.get_table_names())
    if "scripts" in table_names:
        script_columns = {column["name"] for column in inspector.get_columns("scripts")}
        if "pinned_at" not in script_columns:
            sync_conn.execute(text("ALTER TABLE scripts ADD COLUMN pinned_at TIMESTAMP WITHOUT TIME ZONE"))
        if "execution_parameters" not in script_columns:
            sync_conn.execute(text("ALTER TABLE scripts ADD COLUMN execution_parameters TEXT DEFAULT '[]'"))

    if "servers" in table_names:
        server_columns = {column["name"] for column in inspector.get_columns("servers")}
        if "ssh_host_key" not in server_columns:
            sync_conn.execute(text("ALTER TABLE servers ADD COLUMN ssh_host_key VARCHAR(1024) DEFAULT ''"))
    if "execution_records" in table_names:
        execution_columns = {column["name"] for column in inspector.get_columns("execution_records")}
        if "trigger_source" not in execution_columns:
            sync_conn.execute(text("ALTER TABLE execution_records ADD COLUMN trigger_source VARCHAR(16) DEFAULT 'manual'"))
        if "schedule_id" not in execution_columns:
            sync_conn.execute(text("ALTER TABLE execution_records ADD COLUMN schedule_id INTEGER"))
        if "trigger_snapshot" not in execution_columns:
            sync_conn.execute(text("ALTER TABLE execution_records ADD COLUMN trigger_snapshot TEXT DEFAULT ''"))
        if "execution_parameters" not in execution_columns:
            sync_conn.execute(text("ALTER TABLE execution_records ADD COLUMN execution_parameters TEXT DEFAULT '[]'"))
        if "created_by_user_id" not in execution_columns:
            sync_conn.execute(text("ALTER TABLE execution_records ADD COLUMN created_by_user_id INTEGER"))
        if "created_by_username" not in execution_columns:
            sync_conn.execute(text("ALTER TABLE execution_records ADD COLUMN created_by_username VARCHAR(256) DEFAULT ''"))
        if "stopped_by_user_id" not in execution_columns:
            sync_conn.execute(text("ALTER TABLE execution_records ADD COLUMN stopped_by_user_id INTEGER"))
        if "stopped_by_username" not in execution_columns:
            sync_conn.execute(text("ALTER TABLE execution_records ADD COLUMN stopped_by_username VARCHAR(256) DEFAULT ''"))

    if "script_schedules" in table_names:
        schedule_columns = {column["name"] for column in inspector.get_columns("script_schedules")}
        if "pending_run_count" not in schedule_columns:
            sync_conn.execute(text("ALTER TABLE script_schedules ADD COLUMN pending_run_count INTEGER DEFAULT 0"))
        if "last_execution_id" not in schedule_columns:
            sync_conn.execute(text("ALTER TABLE script_schedules ADD COLUMN last_execution_id INTEGER"))
        if "last_status" not in schedule_columns:
            sync_conn.execute(text("ALTER TABLE script_schedules ADD COLUMN last_status VARCHAR(32) DEFAULT ''"))
        if "remark" not in schedule_columns:
            sync_conn.execute(text("ALTER TABLE script_schedules ADD COLUMN remark VARCHAR(512) DEFAULT ''"))
        sync_conn.execute(
            text("UPDATE script_schedules SET misfire_grace_seconds = 1 WHERE misfire_grace_seconds IS NULL OR misfire_grace_seconds < 1")
        )
        sync_conn.execute(
            text(
                "UPDATE script_schedules "
                "SET overlap_policy = 'skip', max_concurrent_runs = 1, pending_run_count = 0 "
                "WHERE overlap_policy IS NULL OR overlap_policy NOT IN ('skip', 'queue', 'parallel') "
                "OR max_concurrent_runs IS NULL OR max_concurrent_runs < 1 "
                "OR pending_run_count IS NULL OR pending_run_count < 0"
            )
        )

    index_sql_list = [
        ("script_schedules", "CREATE UNIQUE INDEX IF NOT EXISTS uq_script_schedules_script_id ON script_schedules (script_id)"),
        ("script_schedules", "CREATE INDEX IF NOT EXISTS ix_script_schedules_script_id ON script_schedules (script_id)"),
        ("script_schedules", "CREATE INDEX IF NOT EXISTS ix_script_schedules_enabled ON script_schedules (enabled)"),
        ("script_schedules", "CREATE INDEX IF NOT EXISTS ix_script_schedules_next_run_at ON script_schedules (next_run_at)"),
        ("schedule_events", "CREATE INDEX IF NOT EXISTS ix_schedule_events_schedule_id ON schedule_events (schedule_id)"),
        ("schedule_events", "CREATE INDEX IF NOT EXISTS ix_schedule_events_script_id ON schedule_events (script_id)"),
        ("execution_records", "CREATE INDEX IF NOT EXISTS ix_execution_records_schedule_id ON execution_records (schedule_id)"),
        ("execution_records", "CREATE INDEX IF NOT EXISTS ix_execution_records_trigger_source ON execution_records (trigger_source)"),
        ("execution_artifacts", "CREATE INDEX IF NOT EXISTS ix_execution_artifacts_execution_id ON execution_artifacts (execution_id)"),
        ("execution_records", "CREATE INDEX IF NOT EXISTS ix_execution_records_created_by_user_id ON execution_records (created_by_user_id)"),
        ("users", "CREATE INDEX IF NOT EXISTS ix_users_username ON users (username)"),
        ("users", "CREATE INDEX IF NOT EXISTS ix_users_role ON users (role)"),
        ("auth_sessions", "CREATE INDEX IF NOT EXISTS ix_auth_sessions_user_id ON auth_sessions (user_id)"),
        ("auth_sessions", "CREATE INDEX IF NOT EXISTS ix_auth_sessions_token_hash ON auth_sessions (token_hash)"),
        ("user_script_pins", "CREATE INDEX IF NOT EXISTS ix_user_script_pins_user_id ON user_script_pins (user_id)"),
        ("user_script_pins", "CREATE INDEX IF NOT EXISTS ix_user_script_pins_script_id ON user_script_pins (script_id)"),
        ("audit_logs", "CREATE INDEX IF NOT EXISTS ix_audit_logs_created_at ON audit_logs (created_at)"),
        ("script_file_roots", "CREATE INDEX IF NOT EXISTS ix_script_file_roots_server_id ON script_file_roots (server_id)"),
        ("script_file_resources", "CREATE INDEX IF NOT EXISTS ix_script_file_resources_root_id ON script_file_resources (root_id)"),
    ]
    for table_name, sql in index_sql_list:
        if table_name in table_names:
            sync_conn.execute(text(sql))


async def init_db():
    """初始化数据库，创建所有表"""
    async with engine.begin() as conn:
        from models import Server, Script, ExecutionRecord, User, AuthSession, UserScriptPin, AuditLog, ScriptFileRoot, ScriptFileResource  # noqa
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_sync_migrate)
    from services.auth_service import sync_fixed_admin
    await sync_fixed_admin()
    from services.script_file_service import ensure_local_script_root
    await ensure_local_script_root()
    from services.script_file_service import recover_incomplete_script_file_operations
    await recover_incomplete_script_file_operations()


async def get_db():
    """获取数据库会话"""
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
