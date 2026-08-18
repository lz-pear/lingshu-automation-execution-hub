import asyncio
import os
from pathlib import Path
from unittest.mock import patch

from executors import ExecutionManager


def test_nonzero_exit_code_is_a_completed_script_execution():
    async def run_case():
        manager = ExecutionManager()

        async def sync_artifacts(_: int) -> int:
            return 0

        manager._sync_execution_artifacts = sync_artifacts
        command = "exit /b 7" if os.name == "nt" else "exit 7"
        with patch("executors.ensure_execution_artifact_dir", return_value=Path.cwd()):
            return await manager._run_local(999999, command, timeout=5)

    exit_code, _, artifact_count, platform_error = asyncio.run(run_case())

    assert exit_code == 7
    assert artifact_count == 0
    assert platform_error is False


def test_posix_command_uses_configured_shell_without_login_mode():
    manager = ExecutionManager()

    with patch("executors.LOCAL_SHELL", "/custom/bin/bash"):
        command = "cd /tmp && printf 'ok' | tee output.log; exit 7"
        assert manager._build_posix_command(command) == [
            "/custom/bin/bash",
            "-c",
            command,
        ]
