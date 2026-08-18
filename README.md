# 灵枢自动化执行中枢

灵枢是一个基于 FastAPI 的任务执行与调度平台，提供本地进程、SSH 远程命令与 HTTP 调用的统一管理能力，以及脚本、服务器、执行记录、执行产物、定时任务和审计日志的 Web 管理界面。

## 功能概览

- 支持 `local`、`ssh` 与 `http` 三种脚本执行类型。
- 提供执行参数校验、环境变量注入、执行中止和执行历史追踪。
- 支持 Cron 定时任务、并发策略与调度事件记录。
- 支持执行产物收集、在线预览、下载与受控清理。
- 支持账号登录、会话管理、管理员权限和审计日志。
- 支持受控根目录内的本地及 SSH 脚本文件管理。

## 技术栈

- 后端：Python、FastAPI、SQLAlchemy、asyncpg、APScheduler。
- 前端：Vue 3、Element Plus。
- 数据库：PostgreSQL。
- 容器化：Docker、Docker Compose。

## 运行要求

- Python 3.12 或兼容版本。
- PostgreSQL 14 或更高版本。
- Docker 与 Docker Compose（容器化部署）。

## 配置

应用从系统环境变量读取运行配置，也会读取仓库根目录的 `deploy.env`。系统环境变量优先。

| 变量 | 说明 |
| --- | --- |
| `PLATFORM_DATABASE_URL` | PostgreSQL 异步连接串，必填。 |
| `PLATFORM_ADMIN_PASSWORD` | 固定管理员密码，必填且不提供默认值。 |
| `PLATFORM_SESSION_COOKIE_SECURE` | HTTPS 部署时设为 `true`。 |
| `PLATFORM_LOCAL_SCRIPT_ROOT` | 本地脚本受控根目录。 |
| `PLATFORM_ARTIFACT_STORAGE_ROOT` | 执行产物存储目录。 |

完整变量列表见 [`deploy.env.example`](deploy.env.example)。`deploy.env`、密钥文件、运行数据和本地脚本目录均已被 Git 忽略。

## 容器化部署

```bash
cp deploy.env.example deploy.env
docker compose up --build -d
```

服务默认监听 `5002` 端口，健康检查地址为 `GET /health`，OpenAPI 文档地址为 `/docs`。

## 本地开发与测试

```powershell
Copy-Item deploy.env.example deploy.env
python -m pip install -r requirements.txt
python -m pip install pytest
$env:PLATFORM_DATABASE_URL = "postgresql+asyncpg://user:pass@localhost/test"
$env:PLATFORM_ADMIN_PASSWORD = "local-development-password"
python main.py
```

```powershell
$env:PLATFORM_DATABASE_URL = "postgresql+asyncpg://user:pass@localhost/test"
$env:PLATFORM_ADMIN_PASSWORD = "test-only-password"
pytest
```

## 目录说明

```text
routers/       HTTP API 路由
services/      业务服务层
templates/     单页 Web 管理界面
static/lib/    本地前端第三方依赖
storage/       运行时脚本、产物与临时文件目录
tests/         自动化测试
```

## 安全边界

灵枢具备执行本地和远程命令的能力，应部署在受控网络中，并仅向受信任的管理员开放。生产部署基线、凭据处理和漏洞报告流程见 [`SECURITY.md`](SECURITY.md)。

## 开源治理

- 许可证：MIT，见 [`LICENSE`](LICENSE)。
- 贡献指南：[`CONTRIBUTING.md`](CONTRIBUTING.md)。
- 社区行为准则：[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)。
- 变更记录：[`CHANGELOG.md`](CHANGELOG.md)。
- 第三方依赖声明：[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。
- 架构说明：[`docs/architecture.md`](docs/architecture.md)。
