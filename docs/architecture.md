# 架构说明

## 组件关系

```text
浏览器管理界面
      │
      ▼
FastAPI 路由层 ──> 服务层 ──> PostgreSQL
      │                 │
      │                 ├── 本地进程执行器
      │                 ├── SSH 执行器
      │                 └── HTTP 执行器
      │
      └── 执行产物存储与脚本文件管理
```

## 分层职责

- `routers/`：认证、脚本、服务器、执行、调度、产物和审计 API。
- `services/`：认证、参数解析、执行辅助、产物管理、文件管理和调度服务。
- `executors.py`：本地、SSH 和 HTTP 任务执行生命周期。
- `scheduler_manager.py`：APScheduler 调度器与任务触发协调。
- `models.py`、`schemas.py`：数据库模型和 API 数据结构。
- `templates/index.html`：Vue 与 Element Plus 实现的单页管理界面。

## 数据与执行流

1. 管理员维护脚本、服务器与定时任务配置。
2. 手工执行或定时调度创建执行记录。
3. 执行器根据脚本类型启动本地进程、SSH 命令或 HTTP 请求。
4. 标准输出、状态和执行产物归档至存储目录与 PostgreSQL。
5. 管理界面通过 API 查询状态、审计记录和可下载产物。
