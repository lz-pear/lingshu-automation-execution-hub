"""灵枢自动化执行中枢 - 应用主入口"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware

from config import APP_HOST, APP_PORT, APP_RELOAD, CORS_ORIGINS
from database import init_db
from executors import execution_manager
from scheduler_manager import scheduler_manager
from services.artifact_service import purge_artifact_trash


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("=" * 50)
    print("  灵枢 · 自动化执行中枢 v2.0")
    print("=" * 50)
    execution_manager.validate_local_shell()
    print("  🚀 初始化数据库...")
    await init_db()
    pending_artifact_cleanup = purge_artifact_trash()
    if pending_artifact_cleanup:
        print(f"  ⚠️ 仍有 {len(pending_artifact_cleanup)} 个产物回收目录等待清理")
    print("  ✅ 数据库初始化完成")
    print("  ⏰ 启动定时调度器...")
    await scheduler_manager.start()
    print("  ✅ 调度器已启动")
    yield
    await scheduler_manager.shutdown()
    print("  👋 应用已关闭")


app = FastAPI(
    title="灵枢",
    description="灵枢自动化执行中枢，支持 SSH 远程、本地进程与 HTTP 调用",
    version="2.0.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 静态文件服务（用于前端库）
static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# 注册路由
from routers.scripts_router import router as scripts_router
from routers.servers_router import router as servers_router
from routers.execution_router import router as execution_router
from routers.schedules_router import router as schedules_router
from routers.artifacts_router import router as artifacts_router
from routers.auth_router import router as auth_router
from routers.users_router import router as users_router
from routers.audit_router import router as audit_router
from routers.script_files_router import router as script_files_router

app.include_router(auth_router)
app.include_router(users_router)
app.include_router(audit_router)
app.include_router(scripts_router)
app.include_router(servers_router)
app.include_router(execution_router)
app.include_router(schedules_router)
app.include_router(artifacts_router)
app.include_router(script_files_router)


# 前端页面
TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")


@app.get("/", include_in_schema=False)
async def serve_frontend():
    index_path = os.path.join(TEMPLATES_DIR, "index.html")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            content = f.read()
        return Response(content=content, media_type="text/html", headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        })
    return {"error": "前端页面文件不存在"}


@app.get("/health", tags=["系统"])
async def health_check():
    return {"status": "ok", "version": "2.0.0"}


if __name__ == "__main__":
    import uvicorn
    print("=" * 50)
    print("  灵枢 · 自动化执行中枢 v2.0")
    print("  已启用登录、固定角色权限与执行归属")
    print("=" * 50)
    print(f"  访问地址: http://127.0.0.1:{APP_PORT}")
    print(f"  API文档:  http://127.0.0.1:{APP_PORT}/docs")
    print("=" * 50)
    uvicorn.run("main:app", host=APP_HOST, port=APP_PORT, reload=APP_RELOAD)
