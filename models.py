"""SQLAlchemy 数据库模型。"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Boolean, Float, UniqueConstraint
from sqlalchemy.orm import relationship
from database import Base


def now():
    return datetime.now()


class User(Base):
    """平台用户。角色固定为 admin / user。"""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(256).with_variant(String(256, collation="NOCASE"), "sqlite"), nullable=False, unique=True, index=True)
    display_name = Column(String(256), default="")
    password_hash = Column(String(512), nullable=False)
    role = Column(String(16), nullable=False, default="user", index=True)
    auth_version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, default=now)
    updated_at = Column(DateTime, default=now, onupdate=now)


class AuthSession(Base):
    """永久登录会话。数据库只保存令牌哈希。"""
    __tablename__ = "auth_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    auth_version = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=now)
    last_seen_at = Column(DateTime, default=now)


class LoginAttempt(Base):
    """按登录名记录连续失败与锁定时间。"""
    __tablename__ = "login_attempts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    identifier = Column(String(256).with_variant(String(256, collation="NOCASE"), "sqlite"), nullable=False, unique=True, index=True)
    failure_count = Column(Integer, nullable=False, default=0)
    locked_until = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=now, onupdate=now)


class UserScriptPin(Base):
    """每位用户独立保存脚本置顶状态。"""
    __tablename__ = "user_script_pins"
    __table_args__ = (UniqueConstraint("user_id", "script_id", name="uq_user_script_pin"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    script_id = Column(Integer, ForeignKey("scripts.id"), nullable=False, index=True)
    pinned_at = Column(DateTime, default=now, nullable=False)


class AuditLog(Base):
    """关键业务操作审计。"""
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=True, index=True)
    username = Column(String(256), default="", index=True)
    action = Column(String(64), nullable=False, index=True)
    resource_type = Column(String(64), default="", index=True)
    resource_id = Column(String(64), default="")
    summary = Column(String(1024), default="")
    ip_address = Column(String(128), default="")
    user_agent = Column(String(512), default="")
    created_at = Column(DateTime, default=now, index=True)


class Server(Base):
    """SSH服务器配置"""
    __tablename__ = "servers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False, comment="服务器名称")
    hostname = Column(String(256), nullable=False, comment="主机地址")
    port = Column(Integer, default=22, comment="SSH端口")
    username = Column(String(64), nullable=False, comment="登录用户名")
    password = Column(String(256), comment="登录密码")
    root_password = Column(String(256), comment="root密码(用于su切换)")
    ssh_host_key = Column(String(1024), default="", comment="固定SSH主机公钥，供文件管理校验")
    description = Column(String(512), default="", comment="描述")
    created_at = Column(DateTime, default=now)
    updated_at = Column(DateTime, default=now, onupdate=now)


class ScriptFileRoot(Base):
    """脚本文件管理可访问的受控根目录。"""
    __tablename__ = "script_file_roots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False)
    target_type = Column(String(16), nullable=False, comment="local/ssh")
    server_id = Column(Integer, ForeignKey("servers.id"), nullable=True, index=True)
    root_path = Column(String(1024), nullable=False)
    enabled = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=now)
    updated_at = Column(DateTime, default=now, onupdate=now)


class ScriptFileResource(Base):
    """平台管理的一个目标文件或目录，不保存内容本体。"""
    __tablename__ = "script_file_resources"
    __table_args__ = (UniqueConstraint("root_id", "relative_path", name="uq_script_file_resource_path"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False)
    description = Column(String(512), default="")
    resource_type = Column(String(16), nullable=False, comment="file/directory")
    root_id = Column(Integer, ForeignKey("script_file_roots.id"), nullable=False, index=True)
    relative_path = Column(String(1024), nullable=False)
    last_success_file_name = Column(String(255), default="")
    last_success_file_count = Column(Integer, default=0)
    last_success_total_size = Column(Integer, default=0)
    last_success_sha256 = Column(String(64), default="")
    last_success_at = Column(DateTime, nullable=True)
    last_success_by_username = Column(String(256), default="")
    last_attempt_status = Column(String(16), default="", comment="uploading/success/failed")
    last_attempt_at = Column(DateTime, nullable=True)
    last_attempt_by_username = Column(String(256), default="")
    last_error = Column(Text, default="")
    operation_id = Column(String(64), default="")
    operation_phase = Column(String(32), default="")
    created_by_username = Column(String(256), default="")
    created_at = Column(DateTime, default=now)
    updated_at = Column(DateTime, default=now, onupdate=now)


class Script(Base):
    """脚本定义"""
    __tablename__ = "scripts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False, comment="脚本名称")
    description = Column(String(512), default="", comment="脚本描述")
    script_type = Column(String(16), nullable=False, comment="脚本类型: ssh/local/http")
    server_id = Column(Integer, ForeignKey("servers.id"), nullable=True, comment="关联服务器ID")
    server = relationship("Server", lazy="joined")
    remote_path = Column(String(512), default="", comment="远程脚本路径")
    run_as_user = Column(String(64), default="", comment="以指定用户身份执行(如dmdba)")
    command = Column(String(2048), default="", comment="本地执行命令")
    http_url = Column(String(1024), default="", comment="HTTP接口地址")
    http_method = Column(String(8), default="GET", comment="HTTP方法")
    http_headers = Column(Text, default="{}", comment="HTTP请求头(JSON)")
    http_body = Column(Text, default="", comment="HTTP请求体")
    timeout = Column(Integer, nullable=True, comment="超时时间(秒)，为空表示不限时")
    tags = Column(String(256), default="", comment="标签(逗号分隔)")
    execution_parameters = Column(Text, default="[]", comment="执行时可填写参数定义(JSON)")
    enabled = Column(Boolean, default=True, comment="是否启用")
    pinned_at = Column(DateTime, nullable=True, comment="最近置顶时间")
    created_at = Column(DateTime, default=now)
    updated_at = Column(DateTime, default=now, onupdate=now)


class ExecutionRecord(Base):
    """执行记录"""
    __tablename__ = "execution_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    script_id = Column(Integer, ForeignKey("scripts.id"), nullable=True)
    script_name = Column(String(128), nullable=False)
    script_type = Column(String(16), nullable=False)
    server_name = Column(String(128), default="", comment="执行时的服务器名")
    trigger_source = Column(String(16), default="manual", comment="触发来源: manual/schedule")
    schedule_id = Column(Integer, ForeignKey("script_schedules.id"), nullable=True, comment="关联定时计划ID")
    trigger_snapshot = Column(Text, default="", comment="触发时的调度快照(JSON)")
    execution_parameters = Column(Text, default="[]", comment="本次执行参数快照(JSON)")
    created_by_user_id = Column(Integer, nullable=True, index=True, comment="手动执行用户ID")
    created_by_username = Column(String(256), default="", comment="执行用户名快照")
    stopped_by_user_id = Column(Integer, nullable=True, comment="停止操作用户ID")
    stopped_by_username = Column(String(256), default="", comment="停止用户名快照")
    status = Column(String(16), default="pending", comment="平台执行状态: pending/running/stopping/completed/failed/stopped")
    output = Column(Text, default="", comment="执行输出")
    exit_code = Column(Integer, nullable=True, comment="退出码")
    error_message = Column(String(1024), default="", comment="错误信息")
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    duration = Column(Float, default=0.0, comment="持续秒数")
    created_at = Column(DateTime, default=now)
    artifacts = relationship("ExecutionArtifact", back_populates="execution", cascade="all, delete-orphan")


class ExecutionArtifact(Base):
    """执行产物"""
    __tablename__ = "execution_artifacts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    execution_id = Column(Integer, ForeignKey("execution_records.id"), nullable=False, index=True, comment="关联执行记录ID")
    file_name = Column(String(255), nullable=False, comment="文件名")
    file_ext = Column(String(32), default="", comment="文件扩展名")
    mime_type = Column(String(128), default="", comment="文件MIME类型")
    file_size = Column(Integer, default=0, comment="文件大小(字节)")
    storage_path = Column(String(1024), nullable=False, comment="平台存储相对路径")
    source_path = Column(String(1024), default="", comment="来源路径")
    created_at = Column(DateTime, default=now)

    execution = relationship("ExecutionRecord", back_populates="artifacts")


class ScriptSchedule(Base):
    """脚本定时计划"""
    __tablename__ = "script_schedules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    script_id = Column(Integer, ForeignKey("scripts.id"), nullable=False, index=True, comment="关联脚本ID")
    script = relationship("Script", lazy="joined")
    name = Column(String(128), nullable=False, comment="计划名称")
    enabled = Column(Boolean, default=True, index=True, comment="是否启用")
    trigger_type = Column(String(16), nullable=False, comment="触发类型: cron/interval/once")
    cron_expression = Column(String(128), default="", comment="Cron表达式")
    interval_seconds = Column(Integer, nullable=True, comment="间隔秒数")
    run_at = Column(DateTime, nullable=True, comment="一次性执行时间")
    timezone = Column(String(64), default="Asia/Shanghai", comment="计划时区")
    misfire_policy = Column(String(16), default="skip", comment="错过策略: skip/fire_once")
    misfire_grace_seconds = Column(Integer, default=300, comment="错过宽限秒数")
    overlap_policy = Column(String(16), default="skip", comment="并发策略: skip/queue/parallel")
    max_concurrent_runs = Column(Integer, default=1, comment="最大并发数")
    start_at = Column(DateTime, nullable=True, comment="生效开始时间")
    end_at = Column(DateTime, nullable=True, comment="生效结束时间")
    pending_run_count = Column(Integer, default=0, comment="排队等待中的触发次数")
    last_run_at = Column(DateTime, nullable=True, comment="上次触发时间")
    next_run_at = Column(DateTime, nullable=True, index=True, comment="下次触发时间")
    last_execution_id = Column(Integer, ForeignKey("execution_records.id"), nullable=True, comment="最近一次执行记录ID")
    last_status = Column(String(32), default="", comment="最近调度状态")
    remark = Column(String(512), default="", comment="备注")
    created_at = Column(DateTime, default=now)
    updated_at = Column(DateTime, default=now, onupdate=now)


class ScheduleEvent(Base):
    """调度事件日志"""
    __tablename__ = "schedule_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    schedule_id = Column(Integer, ForeignKey("script_schedules.id"), nullable=False, index=True, comment="定时计划ID")
    script_id = Column(Integer, ForeignKey("scripts.id"), nullable=False, index=True, comment="脚本ID")
    event_type = Column(String(32), nullable=False, comment="事件类型")
    reason = Column(String(1024), default="", comment="事件说明")
    execution_id = Column(Integer, ForeignKey("execution_records.id"), nullable=True, comment="关联执行记录")
    created_at = Column(DateTime, default=now)
