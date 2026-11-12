"""隔离网页验收：临时demo数据库、合成诊断和本机HTTP，不读取.env或访问公司接口。"""
from pathlib import Path
from secrets import token_urlsafe
from tempfile import TemporaryDirectory
from threading import Thread
import json
import socket
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))

import uvicorn
from playwright.sync_api import expect, sync_playwright
from app.adapters.base import SourceUnavailable
from app.adapters.erp_validation import build_sample_validation
from app.config import Settings
from app.main import create_app
import app.api.erp_rates as routes


def main():
    output = ROOT / 'runtime' / 'source-ui-check'
    output.mkdir(parents=True, exist_ok=True)
    calls, errors = [], []
    synthetic = {
        'source_version': 'synthetic-ui-v1', 'queried_at': '2026-11-05T09:00:00+00:00',
        'pending_confirmations': ['合成样本：亏损字段和币种未确认'],
        'groups': [
            {'kind': kind, 'status': status, 'endpoint': '/synthetic/list',
             'filters': {'page': 1, 'limit': 2, 'status': status},
             'rows': [], 'total': 0, 'error': None}
            for kind in ('loss', 'blocked') for status in (4, 3)
        ],
        'details': [],
    }
    synthetic['groups'][0].update(rows=[{'orderid': 'O-1', 'shopid': 'S-1'}], total=100)
    synthetic['groups'][1].update(error='ERP拒绝认证或授权', error_kind='access_denied', http_status=403)
    report = build_sample_validation(synthetic)
    fail = False

    def capture(settings, *, limit):
        calls.append(limit)
        if fail:
            raise SourceUnavailable('合成连接失败，未访问公司系统')
        return {'validation': report}

    original = routes.capture_erp_sample
    routes.capture_erp_sample = capture
    try:
        with TemporaryDirectory(prefix='source-ui-', dir=output) as temporary:
            password = token_urlsafe(24)
            config = Settings(_env_file=None, runtime_dir=Path(temporary),
                              database_url=f"sqlite:///{Path(temporary) / 'smoke.db'}",
                              admin_password=password, session_secret=token_urlsafe(48))
            app = create_app(config)
            listener = socket.socket()
            listener.bind(('127.0.0.1', 0))
            listener.listen(128)
            base = f'http://127.0.0.1:{listener.getsockname()[1]}'
            server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False))
            thread = Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 20
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        raise AssertionError('本机验收服务未启动')
                    time.sleep(.05)
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    try:
                        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        page.route('**/*', lambda route: route.continue_() if route.request.url.startswith(base + '/') else route.abort())
                        page.goto(base, wait_until='networkidle')
                        page.get_by_placeholder('请输入账号').fill('admin')
                        page.get_by_placeholder('请输入密码').fill(password)
                        page.get_by_role('button', name='登录工作台', exact=True).click()
                        page.get_by_role('link', name='运行配置', exact=True).click()
                        expect(page.get_by_role('heading', name='ERP 数据获取验证', exact=True)).to_be_visible()
                        assert calls == [], '页面打开不应自动读取ERP'
                        page.get_by_role('button', name='运行真实只读验证', exact=True).click()
                        expect(page.get_by_role('heading', name='仍需确认的数据与公式', exact=True)).to_be_visible()
                        expect(page.get_by_text('HTTP 403：该请求被拒绝', exact=True)).to_be_visible()
                        expect(page.get_by_text('仅第一页，仍有未读取记录', exact=True)).to_be_visible()
                        assert calls == [2]
                        with page.expect_download() as download:
                            page.get_by_role('button', name='下载诊断报告', exact=True).click()
                        downloaded = download.value
                        target = output / 'synthetic-validation.json'
                        downloaded.save_as(target)
                        content = json.loads(target.read_text(encoding='utf-8'))
                        assert content['pricing_ready'] is content['permission_scope_verified'] is False
                        assert content['source_version'] == 'synthetic-ui-v1'
                        page.screenshot(path=str(output / 'desktop.png'), full_page=True)
                        page.set_viewport_size({'width': 390, 'height': 844})
                        expect(page.get_by_role('heading', name='ERP 数据获取验证', exact=True)).to_be_visible()
                        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                        page.screenshot(path=str(output / 'mobile.png'), full_page=True)
                        fail = True
                        with page.expect_response(
                            lambda response: response.url.startswith(base + '/api/sources/erp/validation?')
                            and response.status == 503
                        ) as failure:
                            page.get_by_role('button', name='运行真实只读验证', exact=True).click()
                        error_id = failure.value.headers['x-error-id']
                        expect(page.get_by_text(
                            f'合成连接失败，未访问公司系统（排查编号：{error_id}）', exact=True
                        )).to_be_visible()
                        expect(page.get_by_role('button', name='下载诊断报告', exact=True)).to_have_count(0)
                        assert calls == [2, 2]
                        assert not errors, errors
                    finally:
                        browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=20)
                listener.close()
    finally:
        routes.capture_erp_sample = original
    print('PASS: 主动读取、部分拒绝、抽样边界、报告下载、窄屏及失败清除旧报告；仅合成数据。')


if __name__ == '__main__':
    main()
