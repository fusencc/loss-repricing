from contextlib import asynccontextmanager
import time
from uuid import uuid4
from urllib.parse import urlsplit
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from app.config import get_settings, PROJECT_ROOT
from app.db import Base, create_database
from app.adapters.file import FileAdapter
from app.api.routes import router
from app.services.execution import PricingService
from app.observability import configure_logging, bind_context, bind_sink, event, valid_id


def build_adapter(config):
    # 这是定价任务的Snapshot来源选择，与管理员主动ERP采集入口不是同一条链路。
    # erp_bulk_capture保存的groups/details采集JSON不会在此自动变成Snapshot或上传价格。
    if config.adapter_mode == "file":
        return FileAdapter(config.snapshot_path)
    from app.adapters.http import HttpAdapter
    return HttpAdapter(config)


def create_app(settings=None, *, log_role="api"):
    config = settings or get_settings()
    sink = configure_logging(config, log_role)
    with bind_sink(sink):
        try:
            return _create_app(config, sink)
        except Exception as exc:
            event('app.start_failed', exception=exc)
            # 初始化错误可能带数据库连接或认证信息，仅安全日志保存技术栈。
            raise RuntimeError("程序初始化失败，请检查脱敏运行日志") from None


def _create_app(config, sink):
    if not config.admin_password or len(config.session_secret) < 32:
        raise RuntimeError("请先运行deploy/start-local.ps1生成独立本地密码与会话密钥，或配置LR_ADMIN_PASSWORD/LR_SESSION_SECRET")
    config.runtime_dir.mkdir(parents=True, exist_ok=True)
    database_url = config.database_url
    engine, factory = create_database(database_url)
    # 本地SQLite提供免数据库服务的演示；生产MySQL由显式Alembic迁移管理，API不偷偷改表。
    if database_url.startswith("sqlite"):
        Base.metadata.create_all(engine)
    adapter = build_adapter(config)

    @asynccontextmanager
    async def lifespan(app):
        with bind_sink(sink):
            try:
                yield
            finally:
                engine.dispose()
                event('app.stopped')

    app = FastAPI(title="亏损补亏工作台", version="0.1.0", lifespan=lifespan)
    app.state.config, app.state.engine, app.state.factory = config, engine, factory
    app.state.adapter, app.state.service = adapter, PricingService(factory, adapter, config)
    app.state.login_attempts = {}
    app.state.log_sink = sink

    @app.exception_handler(RequestValidationError)
    async def validation_failed(request: Request, exc: RequestValidationError):
        # Pydantic错误对象可能内含原输入，只记录错误数及安全技术栈；原响应合同不变。
        request.state.error_id = event('http.rejected', exception=exc, count=len(exc.errors()), http_status=422)
        return await request_validation_exception_handler(request, exc)

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

    @app.middleware("http")
    async def request_log(request: Request, call_next):
        # 编号由服务端创建；线程池会复制上下文，持久任务另显式保留关联元数据。
        request_id, started = uuid4().hex, time.monotonic()
        with bind_sink(sink), bind_context(request_id=request_id):
            try:
                response = await call_next(request)
            except Exception as exc:
                recorded = getattr(request.state, 'error_id', None)
                error_id = recorded if valid_id(recorded) else event('http.failed', exception=exc)
                response = JSONResponse({'detail': '服务处理异常，请使用排查编号查询运行日志',
                                         'error_id': error_id}, status_code=500)
                if error_id:
                    response.headers['X-Error-ID'] = error_id
            route = request.scope.get('route')
            known_error_id = response.headers.get('X-Error-ID') or getattr(request.state, 'error_id', None)
            new_error_id = uuid4().hex if response.status_code >= 400 and not known_error_id else None
            # 已有异常日志的编号不在摘要上重复，确保按error_id首先找到带栈的原始事件。
            if new_error_id:
                response.headers['X-Error-ID'] = new_error_id
            event_name = ('http.failed' if response.status_code >= 500 and new_error_id else
                          'http.rejected' if 400 <= response.status_code < 500 else 'http.completed')
            event(event_name, error_id=new_error_id,
                  method=request.method if request.method in {'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'} else 'OTHER',
                  route=getattr(route, 'path', 'unmatched'), http_status=response.status_code,
                  duration_ms=(time.monotonic() - started) * 1000)
            response.headers['X-Request-ID'] = request_id
            if request.url.path.startswith('/api/'):
                response.headers['Cache-Control'] = 'no-store'
            response.headers['X-Content-Type-Options'] = 'nosniff'
            response.headers['X-Frame-Options'] = 'DENY'
            response.headers['Referrer-Policy'] = 'same-origin'
            if getattr(request.state, 'error_id', None):
                response.headers['X-Error-ID'] = request.state.error_id
            return response

    app.include_router(router)
    from app.api.logs import router as logs_router
    app.include_router(logs_router)
    from app.api.erp_rates import router as erp_rates_router
    app.include_router(erp_rates_router)
    from app.api.coordination import router as coordination_router
    app.include_router(coordination_router)
    from app.api.notifications import router as notifications_router
    app.include_router(notifications_router)
    frontend = PROJECT_ROOT / "frontend" / "dist"
    if frontend.is_dir():
        app.mount("/assets", StaticFiles(directory=frontend / "assets"), name="assets")
        @app.get("/")
        def index():
            return FileResponse(frontend / "index.html")
    event('app.started')
    return app
