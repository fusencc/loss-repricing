"""本机真实提价日志，只读打开权威 live 档案，不提供管理或执行入口。"""
from contextlib import asynccontextmanager
from threading import Lock
import sqlite3
import time
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
import uvicorn
from app.config import get_settings
from app.services.records import department_records
from app.services.records_stream import encode_records, stream_response


def create_records_app(database_path: Path, bindings_path: Path):
    # mode=ro 在 SQLite 层禁止写入；不初始化表、不创建 worker、不加载上传适配器。
    engine = create_engine('sqlite:///file:' + str(database_path.resolve()) + '?mode=ro&uri=true')
    # data_version changes on commits made by other connections. Compare it on
    # one read-only connection, shared under the same lock as the encoded views.
    version_db = sqlite3.connect(database_path.resolve().as_uri() + '?mode=ro',
                                 uri=True, check_same_thread=False)
    cache = {}
    cache_lock = Lock()

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            version_db.close()
            engine.dispose()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.middleware('http')
    async def no_store(request, call_next):
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/api/health')
    def health():
        return {'mode': 'live', 'read_only': True}

    def read_records(department):
        with Session(engine) as session:
            data = department_records(session, department, mode='live')
        # 仅应用用户明确确认的展示归属，不写 Ownership 或更改任何账号权限。
        bindings = json.loads(bindings_path.read_text()) if bindings_path.exists() else []
        for item in data['items']:
            # 名单批次保存的 ERP 销售员优先，不能被历史商品展示绑定改写。
            if (item['seller']['id'] or '').startswith('erp-roster:'):
                continue
            for binding in bindings:
                if item['key'] == binding['key']:
                    item['seller'] = binding['seller']
                    break
        return data

    def payload(department):
        with cache_lock:
            version = version_db.execute('PRAGMA data_version').fetchone()[0]
            binding_time = bindings_path.stat().st_mtime_ns if bindings_path.exists() else None
            stamp = (version, binding_time)
            entry = cache.get(department)
            now = time.monotonic()
            if entry and entry[0] == stamp and now - entry[1] < 30:
                return entry[2]
            result = encode_records(read_records(department))
            if len(cache) >= 16 and department not in cache:
                cache.pop(next(iter(cache)))
            cache[department] = (stamp, now, result)
            return result

    @app.get('/api/records')
    def records(department: str | None = None):
        try:
            return Response(payload(department), media_type="application/json")
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get('/api/records/stream')
    async def records_stream(request: Request, department: str | None = Query(None, max_length=200)):
        return await stream_response(request, lambda: payload(department))

    @app.get('/')
    def index():
        return FileResponse(ROOT / 'frontend/dist/index.html')

    app.mount('/assets', StaticFiles(directory=ROOT / 'frontend/dist/assets'), name='assets')
    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18827)
    args = parser.parse_args()
    cfg = get_settings()
    url = make_url(cfg.live_history_database_url.get_secret_value())
    if url.get_backend_name() != 'sqlite' or not url.database:
        raise SystemExit('此本机只读入口要求 SQLite live 档案')
    database = Path(url.database)
    if not database.is_file():
        raise SystemExit('权威 live 档案不存在')
    app = create_records_app(database, ROOT / 'runtime/records-display-owners.json')
    uvicorn.run(app, host='127.0.0.1', port=args.port, access_log=False)
