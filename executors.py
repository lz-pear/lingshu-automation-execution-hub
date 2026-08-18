"""执行引擎：支持SSH远程、本地进程、HTTP调用三种方式"""
import asyncio
import json
import locale
import os
import queue
import shlex
import signal
import subprocess
import threading
import time
from datetime import datetime
from typing import Optional

import paramiko
import httpx
from sqlalchemy import select

from config import LOCAL_SHELL
from database import async_session
from models import ExecutionRecord, Server
from services.artifact_service import (
    build_execution_env,
    collect_local_artifact_payloads,
    download_sftp_tree,
    ensure_execution_artifact_dir,
    get_remote_artifact_dir,
    replace_execution_artifacts,
)
from services.execution_parameter_service import build_parameter_environment


class ExecutionManager:
    """执行管理器，管理所有正在执行的脚本"""

    def __init__(self):
        self._events: dict[int, asyncio.Event] = {}
        self._output_locks: dict[int, asyncio.Lock] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._stop_requests: dict[int, bool] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._local_processes: dict[int, subprocess.Popen] = {}
        self._ssh_sessions: dict[int, tuple[paramiko.SSHClient, paramiko.Channel]] = {}
        self._http_clients: dict[int, httpx.AsyncClient] = {}
        self._execution_types: dict[int, str] = {}
        self._completion_callbacks: list = []

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        """获取或缓存事件循环引用"""
        if self._loop is None or self._loop.is_closed():
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self._loop)
        return self._loop

    async def create_execution(
        self,
        script,
        server: Optional[Server],
        *,
        trigger_source: str = "manual",
        schedule_id: Optional[int] = None,
        trigger_snapshot: str = "",
        execution_parameters: str = "[]",
        created_by_user_id: Optional[int] = None,
        created_by_username: str = "system",
    ) -> int:
        """创建执行记录，返回 execution_id"""
        self._get_loop()  # 在主协程中缓存 loop 引用
        async with async_session() as session:
            record = ExecutionRecord(
                script_id=script.id,
                script_name=script.name,
                script_type=script.script_type,
                server_name=server.name if server else "",
                trigger_source=trigger_source,
                schedule_id=schedule_id,
                trigger_snapshot=trigger_snapshot,
                execution_parameters=execution_parameters,
                created_by_user_id=created_by_user_id,
                created_by_username=created_by_username,
                status="pending",
                output="",
            )
            session.add(record)
            await session.commit()
            await session.refresh(record)

        ensure_execution_artifact_dir(record.id)
        self._events[record.id] = asyncio.Event()
        self._output_locks[record.id] = asyncio.Lock()
        self._stop_requests[record.id] = False
        self._execution_types[record.id] = script.script_type
        return record.id

    async def start_execution(
        self,
        execution_id: int,
        script,
        server: Optional[Server],
        parameter_values: Optional[dict[str, str]] = None,
    ):
        """启动执行任务"""
        task = asyncio.create_task(
            self._run_execution(execution_id, script, server, parameter_values or {})
        )
        self._tasks[execution_id] = task

    def request_stop(self, execution_id: int):
        """请求停止执行"""
        self._stop_requests[execution_id] = True
        self._terminate_local_process(execution_id)
        self._terminate_ssh_session(execution_id)
        self._terminate_http_request(execution_id)

    def is_running(self, execution_id: int) -> bool:
        return execution_id in self._tasks and not self._tasks[execution_id].done()

    def is_active(self, execution_id: int) -> bool:
        """判断执行是否仍由当前进程托管，用于过滤数据库中的陈旧运行态记录。"""
        if self.is_running(execution_id):
            return True
        return execution_id in self._events

    def register_completion_callback(self, callback):
        """注册执行完成回调，用于调度器监听执行结束事件。"""
        self._completion_callbacks.append(callback)

    async def wait_for_update(self, execution_id: int, last_output_length: int, timeout: float = 30.0):
        """等待输出更新，用于SSE推送"""
        event = self._events.get(execution_id)
        if event is None:
            return

        async with async_session() as session:
            result = await session.execute(
                select(ExecutionRecord).where(ExecutionRecord.id == execution_id)
            )
            record = result.scalar_one_or_none()
            if record and len(record.output or "") > last_output_length:
                return

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            event.clear()

    async def mark_stopping(self, execution_id: int):
        """将执行状态标记为停止中，并通知前端刷新。"""
        async with async_session() as session:
            result = await session.execute(
                select(ExecutionRecord).where(ExecutionRecord.id == execution_id)
            )
            record = result.scalar_one_or_none()
            if not record:
                return False
            if record.status in ("completed", "failed", "stopped", "stopping"):
                return False
            record.status = "stopping"
            await session.commit()

        await self._append_output(execution_id, f"\n[{time.strftime('%H:%M:%S')}] 收到停止请求，正在尝试终止...\n")
        return True

    async def _append_output(self, execution_id: int, text: str):
        """追加输出到数据库并通知SSE"""
        async def write_output():
            async with async_session() as session:
                result = await session.execute(
                    select(ExecutionRecord).where(ExecutionRecord.id == execution_id)
                )
                record = result.scalar_one_or_none()
                if record:
                    record.output = (record.output or "") + text
                    await session.commit()

        lock = self._output_locks.get(execution_id)
        if lock is None:
            await write_output()
        else:
            async with lock:
                await write_output()

        event = self._events.get(execution_id)
        if event:
            event.set()

    def _schedule_coroutine(self, coro):
        """在线程安全场景下调度协程执行。"""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(lambda: asyncio.create_task(coro))

    async def _update_status(
        self, execution_id: int, status: str, exit_code: Optional[int] = None, error_message: str = ""
    ):
        async with async_session() as session:
            result = await session.execute(
                select(ExecutionRecord).where(ExecutionRecord.id == execution_id)
            )
            record = result.scalar_one_or_none()
            if record:
                record.status = status
                if exit_code is not None:
                    record.exit_code = exit_code
                if error_message:
                    record.error_message = error_message
                if status in ("completed", "failed", "stopped"):
                    record.finished_at = datetime.now()
                    if record.started_at:
                        delta = record.finished_at - record.started_at
                        record.duration = delta.total_seconds()
                await session.commit()

        event = self._events.get(execution_id)
        if event:
            event.set()

    async def _run_execution(
        self,
        execution_id: int,
        script,
        server: Optional[Server],
        parameter_values: dict[str, str],
    ):
        """主执行逻辑"""
        async with async_session() as session:
            result = await session.execute(
                select(ExecutionRecord).where(ExecutionRecord.id == execution_id)
            )
            record = result.scalar_one_or_none()
            if record:
                record.status = "running"
                record.started_at = datetime.now()
                await session.commit()

        await self._append_output(execution_id, f"[{time.strftime('%H:%M:%S')}] 正在执行中...\n")

        try:
            artifact_count = 0
            if script.script_type == "ssh":
                exit_code, output_text, artifact_count, platform_error = await self._run_ssh(
                    execution_id,
                    server,
                    script.remote_path,
                    script.timeout,
                    script.run_as_user or "",
                    parameter_values,
                )
            elif script.script_type == "local":
                exit_code, output_text, artifact_count, platform_error = await self._run_local(
                    execution_id, script.command, script.timeout, parameter_values
                )
            elif script.script_type == "http":
                exit_code, output_text, artifact_count, platform_error = await self._run_http(execution_id, script)
            else:
                exit_code = -1
                output_text = f"未知的脚本类型: {script.script_type}"
                artifact_count = 0
                platform_error = True

            if artifact_count > 0:
                await self._append_output(
                    execution_id,
                    f"\n[{time.strftime('%H:%M:%S')}] 已收集执行产物 {artifact_count} 个，请前往「执行历史 > 产物」下载。\n",
                )

            if self._stop_requests.get(execution_id, False):
                await self._append_output(execution_id, f"\n[{time.strftime('%H:%M:%S')}] 执行已被手动停止\n")
                await self._update_status(execution_id, "stopped")
            elif platform_error:
                error_message = output_text or "平台执行失败"
                should_append_error = False

                if script.script_type in ("ssh", "http") and output_text:
                    should_append_error = script.script_type == "http"
                elif script.script_type == "local" and output_text.startswith(
                    ("错误:", "执行错误:", "执行超时", "命令执行超时")
                ):
                    should_append_error = True

                if should_append_error:
                    suffix = "" if output_text.endswith("\n") else "\n"
                    await self._append_output(execution_id, f"{output_text}{suffix}")

                await self._append_output(execution_id, f"\n[{time.strftime('%H:%M:%S')}] 平台执行失败，退出码: {exit_code}\n")
                await self._update_status(execution_id, "failed", exit_code, error_message=error_message)
            else:
                # 状态只描述平台执行生命周期。脚本的业务成败由退出码和输出自行表达。
                await self._append_output(execution_id, f"\n[{time.strftime('%H:%M:%S')}] 脚本执行已完成，退出码: {exit_code}\n")
                await self._update_status(execution_id, "completed", exit_code)

        except asyncio.CancelledError:
            if self._stop_requests.get(execution_id, False):
                await self._append_output(execution_id, f"\n[{time.strftime('%H:%M:%S')}] 执行已被手动停止\n")
                await self._update_status(execution_id, "stopped")
                return
            raise
        except Exception as e:
            error_msg = str(e)
            await self._append_output(execution_id, f"\n[{time.strftime('%H:%M:%S')}] 执行异常: {error_msg}\n")
            await self._update_status(execution_id, "failed", error_message=error_msg)
        finally:
            await self._notify_completion(execution_id)
            self._tasks.pop(execution_id, None)
            self._events.pop(execution_id, None)
            self._output_locks.pop(execution_id, None)
            self._stop_requests.pop(execution_id, None)
            self._local_processes.pop(execution_id, None)
            self._ssh_sessions.pop(execution_id, None)
            self._http_clients.pop(execution_id, None)
            self._execution_types.pop(execution_id, None)

    async def _notify_completion(self, execution_id: int):
        if not self._completion_callbacks:
            return
        for callback in list(self._completion_callbacks):
            try:
                result = callback(execution_id)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass

    def _schedule_update(self, execution_id: int, chunk: str):
        """从工作线程安全地调度输出更新到主事件循环"""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(
            self._append_output(execution_id, chunk), loop
        )

    def _terminate_local_process(self, execution_id: int):
        """优先结束整个本地进程树，避免 Windows 下子进程残留。"""
        process = self._local_processes.get(execution_id)
        if not process or process.poll() is not None:
            return

        try:
            if os.name == "nt":
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=creationflags,
                )
            else:
                os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def _terminate_ssh_session(self, execution_id: int):
        """主动关闭 SSH channel/client，尽快中断远程执行。"""
        session = self._ssh_sessions.get(execution_id)
        if not session:
            return

        client, channel = session
        try:
            if not channel.closed:
                channel.send("\x03")
        except Exception:
            pass
        for closable in (channel, client):
            try:
                closable.close()
            except Exception:
                pass

    def _terminate_http_request(self, execution_id: int):
        """关闭 HTTP 客户端并取消请求任务，尽快中断等待中的网络调用。"""
        client = self._http_clients.get(execution_id)
        if client is not None:
            self._schedule_coroutine(client.aclose())

        if self._execution_types.get(execution_id) == "http":
            task = self._tasks.get(execution_id)
            if task and not task.done():
                task.cancel()

    @staticmethod
    def _decode_output(raw: bytes) -> str:
        """尽量按当前系统编码解码控制台输出，避免中文乱码。"""
        encodings = [locale.getpreferredencoding(False), "utf-8", "gbk"]
        for encoding in encodings:
            try:
                return raw.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _build_windows_command(command: str) -> str:
        """保留管理员命令中的引号，让 cmd 正确解析带空格的可执行文件和脚本路径。"""
        return f'cmd.exe /d /s /c "{command}"'

    @staticmethod
    def _build_posix_command(command: str) -> list[str]:
        """显式调用配置的 Shell，保留管道、重定向和内建命令语义。"""
        return [LOCAL_SHELL, "-c", command]

    @staticmethod
    def validate_local_shell() -> None:
        """在 POSIX 系统启动和执行前校验本地 Shell 配置。"""
        if os.name == "nt":
            return
        if not os.path.isabs(LOCAL_SHELL):
            raise RuntimeError("PLATFORM_LOCAL_SHELL 必须配置为绝对路径")
        if not os.path.isfile(LOCAL_SHELL) or not os.access(LOCAL_SHELL, os.X_OK):
            raise RuntimeError(f"本地执行 Shell 不存在或不可执行：{LOCAL_SHELL}")

    @staticmethod
    def _build_env_prefix(env_mapping: dict[str, str]) -> str:
        return " ".join(f"{key}={shlex.quote(value)}" for key, value in env_mapping.items())

    def _build_ssh_execution_command(
        self,
        execution_id: int,
        server: Server,
        remote_path: str,
        run_as_user: str,
        remote_artifact_dir: str,
        parameter_values: Optional[dict[str, str]] = None,
    ) -> str:
        env_mapping = {
            **build_execution_env(execution_id, remote_artifact_dir),
            **build_parameter_environment(parameter_values or {}),
        }
        env_prefix = self._build_env_prefix(env_mapping)
        base_command = f"env {env_prefix} bash {shlex.quote(remote_path)}"

        if run_as_user and server.root_password:
            user_command = f"su - {shlex.quote(run_as_user)} -c {shlex.quote(base_command)}"
            return f"echo {shlex.quote(server.root_password)} | su - root -c {shlex.quote(user_command)}"
        if run_as_user:
            return f"su - {shlex.quote(run_as_user)} -c {shlex.quote(base_command)}"
        return base_command

    async def _sync_execution_artifacts(self, execution_id: int) -> int:
        artifacts = collect_local_artifact_payloads(execution_id)
        return await replace_execution_artifacts(execution_id, artifacts)

    async def _run_ssh(
        self,
        execution_id: int,
        server: Server,
        remote_path: str,
        timeout: Optional[int],
        run_as_user: str = "",
        parameter_values: Optional[dict[str, str]] = None,
    ) -> tuple[int, str, int, bool]:
        """SSH远程执行 - 支持su切换到指定用户"""
        if not server:
            return -1, "错误: SSH类型脚本需要关联服务器", 0, True

        remote_artifact_dir = get_remote_artifact_dir(execution_id)
        local_artifact_dir = ensure_execution_artifact_dir(execution_id)

        def ssh_execute() -> tuple[int, str, bool]:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            channel = None
            stop_requested = False
            try:
                client.connect(
                    hostname=server.hostname,
                    port=server.port,
                    username=server.username,
                    password=server.password or None,
                    timeout=10,
                )
                mkdir_stdin, mkdir_stdout, _ = client.exec_command(
                    f"mkdir -p {shlex.quote(remote_artifact_dir)}"
                )
                mkdir_stdin.close()
                if mkdir_stdout.channel.recv_exit_status() != 0:
                    return -1, "SSH错误: 无法创建远端产物目录", True

                channel = client.get_transport().open_session()
                self._ssh_sessions[execution_id] = (client, channel)
                channel.settimeout(timeout)
                channel.get_pty()
                command = self._build_ssh_execution_command(
                    execution_id,
                    server,
                    remote_path,
                    run_as_user,
                    remote_artifact_dir,
                    parameter_values,
                )
                channel.exec_command(command)

                output_parts = []
                started_at = time.monotonic()
                while not channel.exit_status_ready():
                    if self._stop_requests.get(execution_id, False):
                        stop_requested = True
                        channel.close()
                        break
                    if timeout is not None and time.monotonic() - started_at > timeout:
                        try:
                            channel.close()
                        except Exception:
                            pass
                        return -1, f"SSH执行超时(>{timeout}秒)", True
                    if channel.recv_ready():
                        chunk = channel.recv(4096).decode("utf-8", errors="replace")
                        if chunk:
                            output_parts.append(chunk)
                            self._schedule_update(execution_id, chunk)
                    if channel.recv_stderr_ready():
                        chunk = channel.recv_stderr(4096).decode("utf-8", errors="replace")
                        if chunk:
                            output_parts.append(f"[STDERR] {chunk}")
                            self._schedule_update(execution_id, f"[STDERR] {chunk}")
                    time.sleep(0.1)

                if not stop_requested:
                    while channel.recv_ready():
                        chunk = channel.recv(4096).decode("utf-8", errors="replace")
                        if chunk:
                            output_parts.append(chunk)
                            self._schedule_update(execution_id, chunk)

                try:
                    sftp = client.open_sftp()
                except Exception:
                    sftp = None
                if sftp is not None:
                    try:
                        download_sftp_tree(sftp, remote_artifact_dir, local_artifact_dir)
                    finally:
                        try:
                            sftp.close()
                        except Exception:
                            pass

                if stop_requested:
                    return -1, "stopped", False

                exit_status = channel.recv_exit_status()
                return exit_status, "".join(output_parts), False
            except paramiko.AuthenticationException:
                try:
                    client.close()
                except Exception:
                    pass
                return -1, "SSH认证失败，请检查用户名和密码", True
            except Exception as e:
                try:
                    client.close()
                except Exception:
                    pass
                return -1, f"SSH错误: {str(e)}", True
            finally:
                self._ssh_sessions.pop(execution_id, None)

        loop = asyncio.get_running_loop()
        exit_code, output, platform_error = await loop.run_in_executor(None, ssh_execute)
        artifact_count = await self._sync_execution_artifacts(execution_id)
        if output == "stopped":
            return -1, "", artifact_count, False
        return exit_code, output, artifact_count, platform_error

    async def _run_local(
        self,
        execution_id: int,
        command: str,
        timeout: Optional[int],
        parameter_values: Optional[dict[str, str]] = None,
    ) -> tuple[int, str, int, bool]:
        """本地进程执行 - 实时消费输出，避免大日志时管道阻塞。"""
        def run_process() -> tuple[int, str, bool]:
            process = None
            try:
                # Windows 与 POSIX 都保留管理员配置的 Shell 命令语义。
                if os.name == "nt":
                    # list 参数会被 subprocess 再次转义，导致命令内的路径引号被当成文件名字符。
                    cmd_args = self._build_windows_command(command)
                    creationflags = (
                        getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
                        | getattr(subprocess, 'CREATE_NO_WINDOW', 0)
                    )
                    popen_kwargs = {"creationflags": creationflags}
                else:
                    self.validate_local_shell()
                    cmd_args = self._build_posix_command(command)
                    popen_kwargs = {"start_new_session": True}

                process = subprocess.Popen(
                    cmd_args,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
                    text=False,
                    env={
                        **os.environ,
                        **build_execution_env(execution_id, str(ensure_execution_artifact_dir(execution_id))),
                        **build_parameter_environment(parameter_values or {}),
                    },
                    **popen_kwargs,
                )
                self._local_processes[execution_id] = process

                chunk_queue: queue.Queue[tuple[str, Optional[str]]] = queue.Queue()
                output_parts: list[str] = []
                open_streams = 2

                def stream_reader(stream, stream_name: str, prefix: str = ""):
                    try:
                        while True:
                            chunk = stream.readline()
                            if not chunk:
                                break
                            text = f"{prefix}{self._decode_output(chunk)}"
                            chunk_queue.put(("data", text))
                    finally:
                        chunk_queue.put(("end", stream_name))

                stdout_thread = threading.Thread(
                    target=stream_reader, args=(process.stdout, "stdout"), daemon=True
                )
                stderr_thread = threading.Thread(
                    target=stream_reader, args=(process.stderr, "stderr", "[STDERR] "), daemon=True
                )
                stdout_thread.start()
                stderr_thread.start()

                start = time.time()
                while True:
                    if self._stop_requests.get(execution_id, False):
                        self._terminate_local_process(execution_id)
                        return -1, "stopped", False

                    while True:
                        try:
                            item_type, payload = chunk_queue.get_nowait()
                        except queue.Empty:
                            break

                        if item_type == "data" and payload:
                            output_parts.append(payload)
                            self._schedule_update(execution_id, payload)
                        elif item_type == "end":
                            open_streams -= 1

                    if process.poll() is not None and open_streams <= 0:
                        break

                    if timeout is not None and time.time() - start > timeout:
                        self._terminate_local_process(execution_id)
                        return -1, f"执行超时(>{timeout}秒)", True

                    time.sleep(0.1)

                # 进程退出后，先把队列里已经积压的尾部输出全部取完，避免丢最后几行。
                while True:
                    try:
                        item_type, payload = chunk_queue.get_nowait()
                    except queue.Empty:
                        break

                    if item_type == "data" and payload:
                        output_parts.append(payload)
                        self._schedule_update(execution_id, payload)
                    elif item_type == "end":
                        open_streams -= 1

                while open_streams > 0:
                    try:
                        item_type, payload = chunk_queue.get(timeout=0.2)
                    except queue.Empty:
                        if process.poll() is not None and open_streams <= 0:
                            break
                        continue

                    if item_type == "data" and payload:
                        output_parts.append(payload)
                        self._schedule_update(execution_id, payload)
                    elif item_type == "end":
                        open_streams -= 1

                stdout_thread.join(timeout=1)
                stderr_thread.join(timeout=1)

                while True:
                    try:
                        item_type, payload = chunk_queue.get_nowait()
                    except queue.Empty:
                        break

                    if item_type == "data" and payload:
                        output_parts.append(payload)
                        self._schedule_update(execution_id, payload)
                    elif item_type == "end":
                        open_streams -= 1

                return process.returncode, "".join(output_parts), False

            except FileNotFoundError:
                return -1, f"错误: 命令未找到 - {command}", True
            except subprocess.TimeoutExpired:
                if process:
                    self._terminate_local_process(execution_id)
                return -1, "命令执行超时", True
            except Exception as e:
                if process and process.poll() is None:
                    self._terminate_local_process(execution_id)
                return -1, f"执行错误: {str(e)}", True
            finally:
                self._local_processes.pop(execution_id, None)

        loop = asyncio.get_running_loop()
        exit_code, output, platform_error = await loop.run_in_executor(None, run_process)
        artifact_count = await self._sync_execution_artifacts(execution_id)

        if output == "stopped":
            return -1, "", artifact_count, False

        return exit_code, output, artifact_count, platform_error

    async def _run_http(self, execution_id: int, script) -> tuple[int, str, int, bool]:
        """HTTP接口调用"""
        method = script.http_method.upper()
        url = script.http_url
        if not url:
            return -1, "错误: HTTP类型脚本需要填写URL", 0, True

        headers = {}
        try:
            if script.http_headers:
                headers = json.loads(script.http_headers)
        except json.JSONDecodeError:
            return -1, "错误: HTTP请求头JSON格式错误", 0, True

        body = None
        if script.http_body:
            try:
                body = json.loads(script.http_body)
            except json.JSONDecodeError:
                body = script.http_body

        await self._append_output(
            execution_id,
            f"[{time.strftime('%H:%M:%S')}] 发起 {method} 请求: {url}\n"
            f"请求头: {json.dumps(headers, ensure_ascii=False)}\n"
            f"请求体: {json.dumps(body, ensure_ascii=False) if isinstance(body, (dict, list)) else body}\n",
        )

        try:
            async with httpx.AsyncClient(timeout=script.timeout) as client:
                self._http_clients[execution_id] = client
                response = await client.request(
                    method=method, url=url, headers=headers,
                    json=body if isinstance(body, (dict, list)) else None,
                    data=body if not isinstance(body, (dict, list)) else None,
                )
                status_code = response.status_code
                response_text = response.text
                await self._append_output(
                    execution_id,
                    f"[{time.strftime('%H:%M:%S')}] 响应状态码: {status_code}\n"
                    f"响应内容:\n{response_text[:5000]}\n",
                )
                if 200 <= status_code < 300:
                    return 0, response_text, 0, False
                return status_code, f"HTTP {status_code}: {response_text[:1000]}", 0, False
        except asyncio.CancelledError:
            if self._stop_requests.get(execution_id, False):
                return -1, "stopped", 0, False
            raise
        except httpx.TimeoutException:
            timeout_desc = f">{script.timeout}秒" if script.timeout is not None else "未设置超时"
            return -1, f"HTTP请求超时({timeout_desc})", 0, True
        except httpx.RequestError as e:
            if self._stop_requests.get(execution_id, False):
                return -1, "stopped", 0, False
            return -1, f"HTTP请求失败: {str(e)}", 0, True
        except Exception as e:
            if self._stop_requests.get(execution_id, False):
                return -1, "stopped", 0, False
            return -1, f"HTTP执行错误: {str(e)}", 0, True
        finally:
            self._http_clients.pop(execution_id, None)


# 全局单例
execution_manager = ExecutionManager()
