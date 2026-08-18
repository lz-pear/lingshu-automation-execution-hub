"""Pydantic 数据交换模型。"""
from datetime import datetime
from typing import Optional, Literal, List
from pydantic import BaseModel, Field, field_validator, model_validator


# ===================== 登录与用户 =====================

class LoginRequest(BaseModel):
    username: str
    password: str
    remember_password: bool = False


class UserCreate(BaseModel):
    username: str
    display_name: str = ""
    password: str


class UserUpdate(BaseModel):
    display_name: Optional[str] = None
    password: Optional[str] = None


class UserResponse(BaseModel):
    id: int
    username: str
    display_name: str
    role: str
    is_fixed_admin: bool = False
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class AuditLogResponse(BaseModel):
    id: int
    user_id: Optional[int] = None
    username: str
    action: str
    resource_type: str
    resource_id: str
    summary: str
    ip_address: str
    user_agent: str
    created_at: datetime

    class Config:
        from_attributes = True


class AuditLogPageResponse(BaseModel):
    items: List[AuditLogResponse] = Field(default_factory=list)
    available_action_categories: List[str] = Field(default_factory=list)
    total: int = 0
    page: int
    page_size: int


# ===================== 服务器管理 =====================

class ServerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128, description="服务器名称")
    hostname: str = Field(min_length=1, max_length=256, description="主机地址")
    port: int = Field(default=22, ge=1, le=65535, description="SSH端口")
    username: str = Field(min_length=1, max_length=64, description="登录用户名")
    password: Optional[str] = Field(default="", max_length=256, description="登录密码")
    root_password: Optional[str] = Field(default="", max_length=256, description="root密码(用于su切换)")
    ssh_host_key: Optional[str] = Field(default="", max_length=1024, description="SSH主机公钥")
    description: str = Field(default="", max_length=512, description="描述")


class ServerUpdate(BaseModel):
    name: Optional[str] = None
    hostname: Optional[str] = None
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    username: Optional[str] = None
    password: Optional[str] = None
    root_password: Optional[str] = None
    ssh_host_key: Optional[str] = None
    description: Optional[str] = None


class ServerResponse(BaseModel):
    id: int
    name: str
    hostname: str
    port: int
    username: str
    description: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ServerDetailResponse(ServerResponse):
    password: Optional[str] = None
    root_password: Optional[str] = None
    ssh_host_key: Optional[str] = None


# ===================== 脚本文件管理 =====================

class ScriptFileRootCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    server_id: int = Field(ge=1)
    root_path: str = Field(min_length=1, max_length=1024)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("根目录名称不能为空")
        return value


class ScriptFileRootUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    enabled: Optional[bool] = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("根目录名称不能为空")
        return value


class ScriptFileRootResponse(BaseModel):
    id: int
    name: str
    target_type: str
    server_id: Optional[int] = None
    server_name: str = ""
    root_path: str
    enabled: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ScriptFileResourceResponse(BaseModel):
    id: int
    name: str
    description: str
    resource_type: str
    root_id: int
    target_type: str
    server_id: Optional[int] = None
    server_name: str = ""
    root_name: str
    root_path: str
    relative_path: str
    target_path: str
    last_success_file_name: str
    last_success_file_count: int
    last_success_total_size: int
    last_success_sha256: str
    last_success_at: Optional[datetime] = None
    last_success_by_username: str
    last_attempt_status: str
    last_attempt_at: Optional[datetime] = None
    last_attempt_by_username: str
    last_error: str
    created_by_username: str
    created_at: datetime
    updated_at: datetime


class ScriptFileResourceMetadataUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=512)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("名称不能为空")
        return value

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str) -> str:
        return value.strip()


# ===================== 脚本管理 =====================

class ExecutionParameterDefinition(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$", description="参数标识，用于生成 PLATFORM_PARAM_* 环境变量")
    label: str = Field(min_length=1, max_length=64)
    input_type: Literal["text", "textarea", "select"] = "text"
    required: bool = False
    default: str = Field(default="", max_length=4000)
    placeholder: str = Field(default="", max_length=128)
    max_length: int = Field(default=256, ge=1, le=4000)
    options: List[str] = Field(default_factory=list, max_length=50)

    @field_validator("key", "label", "placeholder")
    @classmethod
    def normalize_parameter_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("options")
    @classmethod
    def normalize_parameter_options(cls, values: List[str]) -> List[str]:
        options: List[str] = []
        for raw_value in values:
            value = str(raw_value).strip()
            if not value:
                raise ValueError("下拉选项不能为空")
            if len(value) > 128:
                raise ValueError("单个下拉选项不能超过128个字符")
            if value not in options:
                options.append(value)
        return options

    @model_validator(mode="after")
    def validate_default_length(self):
        if len(self.default) > self.max_length:
            raise ValueError(f"参数“{self.label}”的默认值超过最大长度")
        if "\x00" in self.default:
            raise ValueError(f"参数“{self.label}”的默认值包含非法字符")
        if self.input_type == "select":
            if not self.options:
                raise ValueError(f"参数“{self.label}”至少需要一个下拉选项")
            if self.default and self.default not in self.options:
                raise ValueError(f"参数“{self.label}”的默认值必须来自下拉选项")
        return self


class ExecutionParameterValue(BaseModel):
    key: str
    label: str
    value: str


class ScriptExecutionRequest(BaseModel):
    parameters: dict[str, str] = Field(default_factory=dict)


def _validate_parameter_definitions(definitions: List[ExecutionParameterDefinition]) -> List[ExecutionParameterDefinition]:
    if len(definitions) > 20:
        raise ValueError("每个脚本最多配置20个执行参数")
    keys = [item.key for item in definitions]
    if len(keys) != len(set(keys)):
        raise ValueError("执行参数标识不能重复")
    return definitions

class ScriptCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128, description="脚本名称")
    description: str = Field(default="", max_length=512, description="描述")
    script_type: str = Field(pattern="^(ssh|local|http)$", description="脚本类型: ssh/local/http")
    server_id: Optional[int] = Field(default=None, description="关联服务器ID(ssh类型必填)")
    remote_path: str = Field(default="", max_length=512, description="远程脚本路径(ssh类型)")
    run_as_user: str = Field(default="", max_length=64, description="以指定用户身份执行(如dmdba)")
    command: str = Field(default="", max_length=2048, description="本地执行命令(local类型)")
    http_url: str = Field(default="", max_length=1024, description="HTTP请求地址(http类型)")
    http_method: str = Field(default="GET", max_length=8, description="HTTP方法")
    http_headers: str = Field(default="{}", description="HTTP请求头(JSON字符串)")
    http_body: str = Field(default="", description="HTTP请求体")
    timeout: Optional[int] = Field(default=None, ge=1, description="超时时间(秒)，为空表示不限时")
    tags: str = Field(default="", max_length=256, description="标签(逗号分隔)")
    execution_parameters: List[ExecutionParameterDefinition] = Field(default_factory=list)

    @field_validator("execution_parameters")
    @classmethod
    def validate_execution_parameters(cls, value: List[ExecutionParameterDefinition]) -> List[ExecutionParameterDefinition]:
        return _validate_parameter_definitions(value)


class ScriptUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    script_type: Optional[str] = Field(default=None, pattern="^(ssh|local|http)$")
    server_id: Optional[int] = None
    remote_path: Optional[str] = None
    run_as_user: Optional[str] = None
    command: Optional[str] = None
    http_url: Optional[str] = None
    http_method: Optional[str] = None
    http_headers: Optional[str] = None
    http_body: Optional[str] = None
    timeout: Optional[int] = Field(default=None, ge=1)
    tags: Optional[str] = None
    execution_parameters: Optional[List[ExecutionParameterDefinition]] = None
    enabled: Optional[bool] = None

    @field_validator("execution_parameters")
    @classmethod
    def validate_execution_parameters(cls, value: Optional[List[ExecutionParameterDefinition]]) -> Optional[List[ExecutionParameterDefinition]]:
        return _validate_parameter_definitions(value) if value is not None else value


class ScriptResponse(BaseModel):
    id: int
    name: str
    description: str
    script_type: str
    server_id: Optional[int] = None
    server: Optional[ServerResponse] = None
    remote_path: str
    run_as_user: str
    command: str
    http_url: str
    http_method: str
    http_headers: str
    http_body: str
    timeout: Optional[int] = None
    tags: str
    execution_parameters: List[ExecutionParameterDefinition] = Field(default_factory=list)
    enabled: bool
    schedule_summary: str = ""
    schedule_enabled: Optional[bool] = None
    last_success_duration: Optional[float] = None
    last_success_finished_at: Optional[datetime] = None
    running_execution_id: Optional[int] = None
    running_started_at: Optional[datetime] = None
    running_status: str = ""
    pinned_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ScriptScheduleBase(BaseModel):
    name: str = Field(min_length=1, max_length=128, description="计划名称")
    enabled: bool = Field(default=True, description="是否启用")
    trigger_type: Literal["cron", "interval", "once"] = Field(description="触发类型")
    cron_expression: str = Field(default="", max_length=128, description="Cron表达式")
    interval_seconds: Optional[int] = Field(default=None, ge=1, description="间隔秒数")
    run_at: Optional[datetime] = Field(default=None, description="一次性执行时间")
    timezone: str = Field(default="Asia/Shanghai", max_length=64, description="时区")
    misfire_policy: Literal["skip", "fire_once"] = Field(default="skip", description="错过策略")
    misfire_grace_seconds: int = Field(default=300, ge=1, le=86400, description="错过宽限秒数")
    overlap_policy: Literal["skip", "queue", "parallel"] = Field(default="skip", description="并发策略")
    max_concurrent_runs: int = Field(default=1, ge=1, le=20, description="最大并发数")
    start_at: Optional[datetime] = Field(default=None, description="生效开始时间")
    end_at: Optional[datetime] = Field(default=None, description="生效结束时间")
    remark: str = Field(default="", max_length=512, description="备注")


class ScriptScheduleCreate(ScriptScheduleBase):
    script_id: int = Field(ge=1, description="关联脚本ID")


class ScriptScheduleUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    enabled: Optional[bool] = None
    trigger_type: Optional[Literal["cron", "interval", "once"]] = None
    cron_expression: Optional[str] = Field(default=None, max_length=128)
    interval_seconds: Optional[int] = Field(default=None, ge=1)
    run_at: Optional[datetime] = None
    timezone: Optional[str] = Field(default=None, max_length=64)
    misfire_policy: Optional[Literal["skip", "fire_once"]] = None
    misfire_grace_seconds: Optional[int] = Field(default=None, ge=1, le=86400)
    overlap_policy: Optional[Literal["skip", "queue", "parallel"]] = None
    max_concurrent_runs: Optional[int] = Field(default=None, ge=1, le=20)
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None
    remark: Optional[str] = Field(default=None, max_length=512)


class ScriptScheduleResponse(BaseModel):
    id: int
    script_id: int
    name: str
    enabled: bool
    trigger_type: str
    cron_expression: str
    interval_seconds: Optional[int] = None
    run_at: Optional[datetime] = None
    timezone: str
    misfire_policy: str
    misfire_grace_seconds: int
    overlap_policy: str
    max_concurrent_runs: int
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None
    pending_run_count: int
    last_run_at: Optional[datetime] = None
    next_run_at: Optional[datetime] = None
    last_execution_id: Optional[int] = None
    last_status: str
    remark: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ScheduleEventResponse(BaseModel):
    id: int
    schedule_id: int
    script_id: int
    event_type: str
    reason: str
    execution_id: Optional[int] = None
    created_at: datetime

    class Config:
        from_attributes = True


# ===================== 执行记录 =====================

class ExecutionStartResponse(BaseModel):
    execution_id: int
    message: str


class ExecutionStatusResponse(BaseModel):
    id: int
    script_name: str
    script_type: str
    trigger_source: str
    schedule_id: Optional[int] = None
    status: str
    output: str
    exit_code: Optional[int] = None
    error_message: str
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    duration: float
    artifact_count: int = 0
    created_by_username: str = ""
    stopped_by_username: str = ""
    execution_parameters: List[ExecutionParameterValue] = Field(default_factory=list)

    class Config:
        from_attributes = True


class ExecutionArtifactResponse(BaseModel):
    id: int
    execution_id: int
    file_name: str
    file_ext: str
    mime_type: str
    file_size: int
    storage_path: str
    source_path: str
    created_at: datetime

    class Config:
        from_attributes = True


class ExecutionHistoryResponse(BaseModel):
    id: int
    script_id: Optional[int] = None
    script_name: str
    display_script_name: str = ""
    script_type: str
    server_name: str
    trigger_source: str
    schedule_id: Optional[int] = None
    status: str
    exit_code: Optional[int] = None
    error_message: str
    output: str = ""
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    duration: float
    created_at: datetime
    artifact_count: int = 0
    artifacts: List[ExecutionArtifactResponse] = Field(default_factory=list)
    created_by_username: str = ""
    stopped_by_username: str = ""
    execution_parameters: List[ExecutionParameterValue] = Field(default_factory=list)

    class Config:
        from_attributes = True


class ExecutionHistoryListItem(BaseModel):
    """历史列表项（不包含output，减少数据传输量）"""
    id: int
    script_id: Optional[int] = None
    script_name: str
    display_script_name: str = ""
    script_type: str
    server_name: str
    trigger_source: str
    schedule_id: Optional[int] = None
    status: str
    exit_code: Optional[int] = None
    error_message: str
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    duration: float
    created_at: datetime
    artifact_count: int = 0
    created_by_username: str = ""
    stopped_by_username: str = ""

    class Config:
        from_attributes = True


class ExecutionHistoryPageResponse(BaseModel):
    items: List[ExecutionHistoryListItem] = Field(default_factory=list)
    total: int = 0
    page: int
    page_size: int
