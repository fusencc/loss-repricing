"""本机真实提价日志，只读打开权威 live 档案，不提供管理或执行入口。"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
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
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

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
            for binding in bindings:
                if item['key'] == binding['key']:
                    item['seller'] = binding['seller']
                    break
        return data

    @app.get('/api/records')
    def records(department: str | None = None):
        try:
            return read_records(department)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get('/api/records/stream')
    async def records_stream(request: Request, department: str | None = Query(None, max_length=200)):
        return await stream_response(request, lambda: encode_records(read_records(department)))

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
