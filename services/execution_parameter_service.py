"""执行参数定义、校验、快照和环境变量转换。"""
import json
from typing import Any, Mapping, Optional


class ExecutionParameterError(ValueError):
    """执行参数不符合脚本定义。"""


def load_parameter_definitions(raw_value: Any) -> list[dict]:
    if isinstance(raw_value, list):
        return raw_value
    if not raw_value:
        return []
    try:
        parsed = json.loads(raw_value)
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def dump_parameter_definitions(definitions: Any) -> str:
    payload = []
    for item in definitions or []:
        payload.append(item.model_dump() if hasattr(item, "model_dump") else dict(item))
    return json.dumps(payload, ensure_ascii=False)


def resolve_execution_parameters(
    definitions_raw: Any,
    submitted: Optional[Mapping[str, str]] = None,
    *,
    defaults_only: bool = False,
) -> tuple[dict[str, str], list[dict]]:
    """按定义生成本次参数；定时执行时只允许使用管理员默认值。"""
    definitions = load_parameter_definitions(definitions_raw)
    supplied = dict(submitted or {})
    allowed_keys = {str(item.get("key", "")) for item in definitions}
    unknown_keys = sorted(set(supplied) - allowed_keys)
    if unknown_keys:
        raise ExecutionParameterError(f"包含未开放的执行参数：{', '.join(unknown_keys)}")

    values: dict[str, str] = {}
    snapshot: list[dict] = []
    for definition in definitions:
        key = str(definition.get("key", "")).strip()
        label = str(definition.get("label", key)).strip() or key
        default = str(definition.get("default", ""))
        raw_value = default if defaults_only or key not in supplied else supplied[key]
        value = str(raw_value)
        max_length = int(definition.get("max_length", 256) or 256)

        if "\x00" in value:
            raise ExecutionParameterError(f"参数“{label}”包含非法字符")
        if len(value) > max_length:
            raise ExecutionParameterError(f"参数“{label}”不能超过 {max_length} 个字符")
        if definition.get("required") and not value.strip():
            message = f"定时执行缺少参数“{label}”的默认值" if defaults_only else f"请填写参数“{label}”"
            raise ExecutionParameterError(message)
        if definition.get("input_type") == "select":
            options = [str(item) for item in definition.get("options") or []]
            if value and value not in options:
                raise ExecutionParameterError(f"参数“{label}”不是允许的选项")
        values[key] = value
        snapshot.append({"key": key, "label": label, "value": value, "required": bool(definition.get("required"))})
    return values, snapshot


def build_parameter_environment(values: Mapping[str, str]) -> dict[str, str]:
    return {f"PLATFORM_PARAM_{key.upper()}": str(value) for key, value in values.items()}


def load_execution_parameter_snapshot(raw_value: Any) -> list[dict]:
    return load_parameter_definitions(raw_value)


def parameter_audit_summary(snapshot: list[dict], limit: int = 300) -> str:
    if not snapshot:
        return ""
    summary = "，".join(f"{item.get('label', item.get('key', '参数'))}={item.get('value', '')}" for item in snapshot)
    return summary if len(summary) <= limit else f"{summary[:limit]}…"


def build_execution_display_name(script_name: str, snapshot_raw: Any, definitions_raw: Any = None) -> str:
    snapshot = load_execution_parameter_snapshot(snapshot_raw)
    if not snapshot:
        return script_name

    required_keys = {
        str(item.get("key", "")).strip()
        for item in load_parameter_definitions(definitions_raw)
        if item.get("required")
    }
    required_values: list[str] = []
    for item in snapshot:
        key = str(item.get("key", "")).strip()
        value = str(item.get("value", "")).strip()
        if not value:
            continue
        if item.get("required") or (key and key in required_keys):
            required_values.append(value)

    return "｜".join([script_name, *required_values]) if required_values else script_name
