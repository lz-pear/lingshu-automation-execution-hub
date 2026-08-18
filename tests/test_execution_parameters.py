"""执行参数安全边界与快照逻辑回归测试。"""
import unittest
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from pydantic import ValidationError

from schemas import ScriptCreate
from services.execution_parameter_service import (
    build_execution_display_name,
    ExecutionParameterError,
    build_parameter_environment,
    resolve_execution_parameters,
)
from executors import ExecutionManager


DEFINITIONS = [
    {
        "key": "project",
        "label": "项目名称",
        "input_type": "text",
        "required": True,
        "default": "",
        "placeholder": "请输入项目名称",
        "max_length": 20,
    },
    {
        "key": "known_risk",
        "label": "已知风险",
        "input_type": "textarea",
        "required": False,
        "default": "无",
        "placeholder": "",
        "max_length": 100,
    },
]


class ExecutionParameterTests(unittest.TestCase):
    def test_manual_values_use_submitted_value_and_default(self):
        values, snapshot = resolve_execution_parameters(DEFINITIONS, {"project": "HX"})
        self.assertEqual(values, {"project": "HX", "known_risk": "无"})
        self.assertEqual(snapshot[0], {"key": "project", "label": "项目名称", "value": "HX", "required": True})
        self.assertEqual(snapshot[1], {"key": "known_risk", "label": "已知风险", "value": "无", "required": False})

    def test_display_name_only_appends_required_parameter_values(self):
        _, snapshot = resolve_execution_parameters(DEFINITIONS, {"project": "HX"})
        self.assertEqual(build_execution_display_name("示例回归测试", snapshot), "示例回归测试｜HX")

    def test_display_name_can_fallback_to_current_definitions_for_old_snapshot(self):
        old_snapshot = [
            {"key": "project", "label": "项目名称", "value": "HX"},
            {"key": "known_risk", "label": "已知风险", "value": "无"},
        ]
        self.assertEqual(build_execution_display_name("示例回归测试", old_snapshot, DEFINITIONS), "示例回归测试｜HX")

    def test_display_name_keeps_original_name_without_required_values(self):
        snapshot = [{"key": "known_risk", "label": "已知风险", "value": "无", "required": False}]
        self.assertEqual(build_execution_display_name("示例回归测试", snapshot), "示例回归测试")

    def test_unknown_parameter_is_rejected(self):
        with self.assertRaisesRegex(ExecutionParameterError, "未开放"):
            resolve_execution_parameters(DEFINITIONS, {"project": "HX", "command": "del *"})

    def test_required_parameter_is_rejected_when_blank(self):
        with self.assertRaisesRegex(ExecutionParameterError, "请填写"):
            resolve_execution_parameters(DEFINITIONS, {"project": "  "})

    def test_schedule_requires_admin_default(self):
        with self.assertRaisesRegex(ExecutionParameterError, "定时执行缺少"):
            resolve_execution_parameters(DEFINITIONS, defaults_only=True)

    def test_parameter_environment_uses_fixed_prefix(self):
        self.assertEqual(
            build_parameter_environment({"project": "HX", "known_risk": "无"}),
            {"PLATFORM_PARAM_PROJECT": "HX", "PLATFORM_PARAM_KNOWN_RISK": "无"},
        )

    def test_select_parameter_accepts_only_configured_options(self):
        definitions = [{
            "key": "top_nav",
            "label": "执行范围",
            "input_type": "select",
            "required": True,
            "default": "",
            "placeholder": "请选择",
            "max_length": 20,
            "options": ["文档管理", "项目管理", "全部模块"],
        }]

        values, _ = resolve_execution_parameters(definitions, {"top_nav": "项目管理"})
        self.assertEqual(values["top_nav"], "项目管理")
        with self.assertRaisesRegex(ExecutionParameterError, "不是允许的选项"):
            resolve_execution_parameters(definitions, {"top_nav": "服务器管理"})

    def test_select_parameter_schema_requires_options_and_valid_default(self):
        base = {
            "key": "top_nav",
            "label": "执行范围",
            "input_type": "select",
            "required": True,
            "max_length": 20,
        }
        with self.assertRaises(ValidationError):
            ScriptCreate(name="测试", script_type="local", command="echo ok", execution_parameters=[base])
        with self.assertRaises(ValidationError):
            ScriptCreate(
                name="测试",
                script_type="local",
                command="echo ok",
                execution_parameters=[{**base, "options": ["文档管理"], "default": "项目管理"}],
            )

    def test_schema_rejects_duplicate_or_unsafe_keys(self):
        with self.assertRaises(ValidationError):
            ScriptCreate(
                name="测试",
                script_type="local",
                command="echo ok",
                execution_parameters=[DEFINITIONS[0], DEFINITIONS[0]],
            )
        with self.assertRaises(ValidationError):
            ScriptCreate(
                name="测试",
                script_type="local",
                command="echo ok",
                execution_parameters=[{**DEFINITIONS[0], "key": "bad-key"}],
            )

    @unittest.skipUnless(os.name == "nt", "仅验证 Windows cmd 引号规则")
    def test_windows_command_supports_script_path_with_spaces(self):
        with tempfile.TemporaryDirectory(prefix="platform command ") as temp_dir:
            script_path = Path(temp_dir) / "parameter probe.py"
            script_path.write_text("print('quoted-path-ok')", encoding="utf-8")
            command = f'"{sys.executable}" "{script_path}"'

            result = subprocess.run(
                ExecutionManager._build_windows_command(command),
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "quoted-path-ok")


if __name__ == "__main__":
    unittest.main()
