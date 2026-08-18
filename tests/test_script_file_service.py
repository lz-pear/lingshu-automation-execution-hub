"""脚本文件管理关键恢复逻辑的回归测试。"""
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from pathlib import Path

from pydantic import ValidationError

from models import Script, ScriptFileResource, ScriptFileRoot
from schemas import ScriptFileResourceMetadataUpdate, ScriptFileRootCreate, ScriptFileRootUpdate
from services.script_file_service import _directory_payload_root, _local_root_contains_resources, _recover_local_operation, _replace_local, _validate_uploaded_file_name, ensure_resource_not_running, sanitize_upload_relative_path


class _FakeResult:
    def __init__(self, scripts):
        self._scripts = scripts

    def scalars(self):
        return self

    def all(self):
        return self._scripts


class _FakeSession:
    def __init__(self, scripts):
        self._scripts = scripts

    async def execute(self, _query):
        return _FakeResult(self._scripts)


class ScriptFileServiceTests(unittest.TestCase):
    def test_script_file_error_column_is_not_length_limited(self):
        self.assertNotIn("String(1024)", ScriptFileResource.__table__.c.last_error.type.compile())

    def test_local_replace_uses_operation_id_and_cleans_artifacts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.py"
            target = root / "task.py"
            source.write_text("print('new')", encoding="utf-8")
            target.write_text("print('old')", encoding="utf-8")

            warning = _replace_local(source, target, "file", "operation-123")

            self.assertEqual(warning, "")
            self.assertEqual(target.read_text(encoding="utf-8"), "print('new')")
            self.assertFalse((root / ".platform-upload-operation-123").exists())
            self.assertFalse((root / ".platform-backup-operation-123").exists())

    def test_local_recovery_restores_backup_with_recorded_operation_id(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            backup = root / ".platform-backup-operation-456"
            staging = root / ".platform-upload-operation-456"
            backup.write_text("print('old')", encoding="utf-8")
            staging.write_text("print('new')", encoding="utf-8")
            resource = ScriptFileResource(relative_path="task.py", operation_id="operation-456")

            recovered = _recover_local_operation(str(root), resource)

            self.assertTrue(recovered)
            self.assertEqual((root / "task.py").read_text(encoding="utf-8"), "print('old')")
            self.assertFalse(backup.exists())
            self.assertFalse(staging.exists())

    def test_script_file_root_name_cannot_be_blank_after_trimming(self):
        with self.assertRaises(ValidationError):
            ScriptFileRootCreate(name="   ", server_id=1, root_path="/opt/scripts")
        with self.assertRaises(ValidationError):
            ScriptFileRootUpdate(name="   ")

    def test_script_file_metadata_name_cannot_be_blank_after_trimming(self):
        with self.assertRaises(ValidationError):
            ScriptFileResourceMetadataUpdate(name="   ")
        metadata = ScriptFileResourceMetadataUpdate(name=" 日志 ", description=" 描述 ")
        self.assertEqual(metadata.name, "日志")
        self.assertEqual(metadata.description, "描述")

    def test_all_file_extensions_and_sensitive_names_are_allowed(self):
        for file_name in ("AI_CONTEXT.md", ".env", "server.pem", "tool.exe", "archive.zip"):
            _validate_uploaded_file_name(file_name)

    def test_directory_upload_sanitizes_windows_invalid_path_characters(self):
        self.assertEqual(
            sanitize_upload_relative_path("run_menu_checks/artifacts/a*bad:screen?.png"),
            "run_menu_checks/artifacts/a_bad_screen_.png",
        )
        self.assertEqual(sanitize_upload_relative_path("CON/report.txt"), "_CON/report.txt")

    def test_directory_upload_shortens_overlong_path_component(self):
        safe_path = sanitize_upload_relative_path(f"screenshots/{'异常快照' * 80}.png")
        self.assertLessEqual(len(safe_path.rsplit("/", 1)[-1]), 80)
        self.assertTrue(safe_path.endswith(".png"))

    def test_directory_upload_uses_selected_folder_as_payload_root(self):
        with tempfile.TemporaryDirectory() as temp:
            staging_root = Path(temp)
            selected_root = staging_root / "run_menu_checks"
            selected_root.mkdir()
            (selected_root / "check.py").write_text("print('ok')", encoding="utf-8")

            self.assertEqual(_directory_payload_root(staging_root), selected_root)

    def test_local_root_migration_accepts_existing_registered_resources(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "release-python").mkdir()
            (root / "run_menu_checks").mkdir()
            resources = [
                ScriptFileResource(relative_path="release-python", resource_type="directory"),
                ScriptFileResource(relative_path="run_menu_checks", resource_type="directory"),
            ]

            self.assertTrue(_local_root_contains_resources(root, resources))

    def test_local_root_migration_rejects_missing_registered_resource(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "release-python").mkdir()
            resources = [
                ScriptFileResource(relative_path="release-python", resource_type="directory"),
                ScriptFileResource(relative_path="run_menu_checks", resource_type="directory"),
            ]

            self.assertFalse(_local_root_contains_resources(root, resources))

    def test_running_local_script_blocks_all_local_file_changes(self):
        root = ScriptFileRoot(target_type="local", root_path=tempfile.gettempdir(), enabled=True)
        resource = ScriptFileResource(relative_path="task.py", resource_type="file")
        script = Script(id=1, name="其他本机任务", script_type="local", command="python unrelated.py")

        with patch("services.script_file_service.is_script_running", new=AsyncMock(return_value=True)):
            with self.assertRaisesRegex(Exception, "存在本机脚本正在执行"):
                import asyncio
                asyncio.run(ensure_resource_not_running(_FakeSession([script]), root, resource))


if __name__ == "__main__":
    unittest.main()
