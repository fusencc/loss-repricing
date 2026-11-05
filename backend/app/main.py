from contextlib import asynccontextmanager
from urllib.parse import urlsplit
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from app.config import get_settings, PROJECT_ROOT
from app.db import Base, create_database
from app.adapters.demo import DemoAdapter
from app.adapters.file import FileAdapter
from app.api.routes import router
from app.services.execution import PricingService


def build_adapter(config):
    if config.adapter_mode == "demo":
        return DemoAdapter(config.runtime_dir)
    if config.adapter_mode == "file":
        return FileAdapter(config.snapshot_path)
    from app.adapters.http import HttpAdapter
    return HttpAdapter(config)


def create_app(settings=None):
    config = settings or get_settings()
    if not config.admin_password or len(config.session_secret) < 32:
        raise RuntimeError("请先运行deploy/start-local.ps1生成独立本地密码与会话密钥，或配置LR_ADMIN_PASSWORD/LR_SESSION_SECRET")
    config.runtime_dir.mkdir(parents=True, exist_ok=True)
    database_url = config.database_url
    if database_url == "sqlite:///runtime/loss-repricing.db":
        database_url = f"sqlite:///{config.runtime_dir / 'loss-repricing.db'}"
    engine, factory = create_database(database_url)
    # 本地SQLite提供免数据库服务的演示；生产MySQL由显式Alembic迁移管理，API不偷偷改表。
    if database_url.startswith("sqlite"):
        Base.metadata.create_all(engine)
    adapter = build_adapter(config)

    @asynccontextmanager
    async def lifespan(app):
        yield
        engine.dispose()

    app = FastAPI(title="亏损补亏工作台", version="0.1.0", lifespan=lifespan)
    app.state.config, app.state.engine, app.state.factory = config, engine, factory
    app.state.adapter, app.state.service = adapter, PricingService(factory, adapter, config)
    app.state.login_attempts = {}

    @app.middleware("http")
    async def same_origin_write(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get("origin"):
            origin = urlsplit(request.headers["origin"])
            if origin.netloc != request.headers.get("host") or origin.scheme not in {"http", "https"}:
                return JSONResponse({"detail": "不允许跨站修改"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.include_router(router)
    frontend = PROJECT_ROOT / "frontend" / "dist"
    if frontend.is_dir():
        app.mount("/assets", StaticFiles(directory=frontend / "assets"), name="assets")
        @app.get("/")
        def index():
            return FileResponse(frontend / "index.html")
    return app
