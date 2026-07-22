"""FastAPI 主应用"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from shell_agent.web.api import router
from shell_agent.web.runtime import init_runtime


@asynccontextmanager
async def lifespan(app: FastAPI):
    """生命周期：启动/关闭"""
    config_path = app.state.config_path
    rt = init_runtime(config_path)
    await rt.start()
    app.state.runtime = rt
    yield
    await rt.stop()


def create_app(config_path: str = "config/agent.yaml") -> FastAPI:
    """创建 FastAPI 应用"""
    app = FastAPI(
        title="Opsane",
        description="Opsane 智能运维工作台",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.config_path = config_path

    # CORS（开发时允许前端跨域）
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^https?://(?:127\.0\.0\.1|localhost)(?::\d+)?$",
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # API 路由
    app.include_router(router)

    # 统一用户入口到模块化 Vue 工作台，避免旧版单文件页面与新版功能并存造成混淆。
    @app.get("/", include_in_schema=False)
    async def workbench_redirect() -> RedirectResponse:
        response = RedirectResponse(url="/next/#/chat", status_code=307)
        response.headers["Cache-Control"] = "no-store"
        return response

    # 静态前端文件
    static_dir = Path(__file__).parent / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    # 如果 index.html 存在则挂载静态文件
    if (static_dir / "index.html").exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app
