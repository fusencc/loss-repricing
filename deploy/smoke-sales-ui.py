"""Isolated real-HTTP/browser sales smoke; synthetic SQLite, no .env or live sends.
Run: python deploy/smoke-sales-ui.py (requires playwright Chromium).
"""
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from secrets import token_urlsafe
import json
import socket
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
import httpx
import uvicorn
from playwright.sync_api import sync_playwright, expect
from app.config import Settings
from app.main import create_app
from app.worker import Worker


def check(response):
    assert response.is_success, f'HTTP {response.status_code}: {response.text}'
    return response.json()


def wait_until(fn, message, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(.15)
    raise AssertionError(message)


def main():
    output = ROOT / 'runtime' / 'sales-ui-check'
    output.mkdir(parents=True, exist_ok=True)
    report = {'mode': 'isolated demo', 'checks': [], 'javascript_errors': []}
    password = token_urlsafe(24)
    with TemporaryDirectory(prefix='loss-sales-ui-') as temporary:
        config = Settings(
            _env_file=None, environment='development', adapter_mode='demo',
            database_url=f"sqlite:///{Path(temporary) / 'smoke.sqlite'}",
            runtime_dir=Path(temporary), admin_username='smoke-admin',
            admin_password=password, session_secret=token_urlsafe(48),
            secure_cookie=False, live_submit_enabled=False, coordination_required=True,
            ding_enabled=False, ding_corp_id='', ding_client_id='', ding_client_secret='',
            ding_agent_id='', notification_mode='demo', vendor_token='', erp_token='',
        )
        app = create_app(config)
        worker = Worker(app.state.factory, app.state.service)
        stop = Event()
        failures = []
        def work():
            while not stop.wait(.15):
                try:
                    worker.tick()
                except Exception as exc:
                    failures.append(type(exc).__name__)
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(128)
        port = listener.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False))
        server_thread = Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
        worker_thread = Thread(target=work, daemon=True)
        server_thread.start()
        wait_until(lambda: server.started, 'HTTP server did not start')
        worker_thread.start()
        try:
            with httpx.Client(base_url=base, timeout=30) as admin:
                check(admin.post('/api/auth/login', json={'username':config.admin_username,'password':password}))
                accounts = {}
                for username, name, role in [('sales-a','销售甲','sales'),('sales-b','销售乙','sales'),('manager-a','演示主管','manager')]:
                    accounts[username] = check(admin.post('/api/users', json={
                        'username':username,'display_name':name,'password':password,
                        'role':role,'can_correct':username == 'sales-a',
                    }))
                for site, owner, supervisor in [('MY','sales-a','manager-a'),('PH','sales-b',None)]:
                    check(admin.post('/api/ownerships', json={
                        'platform':'TikTok','shop_id':f'DEMO-{site}','site':site,
                        'owner_id':accounts[owner]['id'],
                        'supervisor_id':accounts[supervisor]['id'] if supervisor else None,
                        'autonomous':True,
                    }))
                job = check(admin.post('/api/demo/run'))
                def complete_job():
                    data = check(admin.get(f"/api/jobs/{job['job_id']}"))
                    assert data['state'] != 'failed', data
                    return data if data['state'] == 'done' else None
                wait_until(complete_job, 'Demo job did not finish')
                all_items = check(admin.get('/api/items'))
                own_item = next(i for i in all_items if i['key']['sku'] == 'DEMO-SKU-000')
                other_item = next(i for i in all_items if i['key']['sku'] == 'DEMO-SKU-001')
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    contexts = []
                    def session(username):
                        context = browser.new_context(viewport={'width':1440,'height':1000})
                        contexts.append(context)
                        page = context.new_page()
                        page.on('pageerror', lambda error: report['javascript_errors'].append(str(error)))
                        page.on('console', lambda msg: report['javascript_errors'].append(msg.text) if msg.type == 'error' and 'Failed to load resource' not in msg.text else None)
                        page.goto(base, wait_until='networkidle')
                        page.get_by_placeholder('请输入账号').fill(username)
                        page.get_by_placeholder('请输入密码').fill(password)
                        page.get_by_role('button', name='登录工作台', exact=True).click()
                        expect(page.get_by_role('heading',name='运行概览',exact=True)).to_be_visible()
                        return page
                    admin_page = session(config.admin_username)
                    admin_page.get_by_role('link',name='账号与归属',exact=True).click()
                    expect(admin_page.get_by_role('heading',name='独立账号',exact=True)).to_be_visible()
                    expect(admin_page.get_by_role('heading',name='商品归属',exact=True)).to_be_visible()
                    expect(admin_page.get_by_role('cell',name='sales-a',exact=True)).to_be_visible()
                    # Select labels are names; administrator is a valid default-scope owner.
                    owner_select = admin_page.locator('.coord-form').nth(1).get_by_role('combobox',name='负责人',exact=True)
                    owner_select.click()
                    expect(admin_page.get_by_role('option',name='管理员',exact=True)).to_be_visible()
                    expect(admin_page.get_by_role('option',name='销售甲（sales-a）',exact=True)).to_be_visible()
                    admin_page.keyboard.press('Escape')
                    admin_page.screenshot(path=str(output/'admin-ownership.png'),full_page=True)
                    print('CHECK', len(report['checks']) + 1, flush=True); report['checks'].append('admin accounts/ownership/name dropdowns')
                    sales = session('sales-a')
                    expect(sales.get_by_role('heading',name='我的负责范围',exact=True)).to_be_visible()
                    for hidden in ['账号与归属','运行配置','保护名单']:
                        expect(sales.get_by_role('link',name=hidden,exact=True)).to_have_count(0)
                    for hidden in ['运行演示批次','回读核验']:
                        expect(sales.get_by_role('button',name=hidden,exact=True)).to_have_count(0)
                    own_list = sales.request.get(base+'/api/items').json()
                    assert own_list and all(i['key']['shop_id']=='DEMO-MY' for i in own_list)
                    assert sales.request.get(base+f"/api/items/{other_item['id']}").status in (403,404)
                    for forbidden in ['/api/settings','/api/contracts','/api/users','/api/ownerships']:
                        assert sales.request.get(base+forbidden).status == 403, forbidden
                    print('CHECK', len(report['checks']) + 1, flush=True); report['checks'].append('sales scope isolation and privileged endpoint denial')
                    sales.get_by_role('link',name='异常处理',exact=True).click()
                    sales.locator('.inline-filter .el-select').click()
                    sales.get_by_role('option',name='全部状态',exact=True).click()
                    sales.get_by_role('row').filter(has_text='DEMO-SKU-000').get_by_role('button',name='查看详情').click()
                    expect(sales.get_by_role('heading',name='负责与接管',exact=True)).to_be_visible()
                    expect(sales.get_by_text('销售甲 / 演示主管',exact=True)).to_be_visible()
                    reason = sales.get_by_placeholder('填写接管、暂停、交回或修正原因（必填）')
                    reason.fill('合成验收：销售员主动接管')
                    sales.get_by_role('button',name='人工接管',exact=True).click()
                    expect(reason).to_have_value('')
                    assert check(admin.get(f"/api/items/{own_item['id']}"))['control']['mode']=='manual'
                    reason.fill('合成验收：目标实际售价12')
                    sales.get_by_placeholder('目标实际售价，例如 12.50').fill('12')
                    sales.get_by_role('button',name='保存修正方案',exact=True).click()
                    approve = sales.get_by_role('button',name='批准并排队',exact=True)
                    expect(approve).to_be_visible()
                    approve.click()
                    def correction_verified():
                        rows=check(admin.get(f"/api/items/{own_item['id']}/corrections"))
                        assert not any(r['state'] in ('failed','stale') for r in rows), rows
                        return rows if rows and rows[-1]['state']=='verified' else None
                    rows=wait_until(correction_verified,'Correction did not verify')
                    assert rows[-1]['desired_actual']=='12'
                    evidence=check(admin.get(f"/api/items/{own_item['id']}"))
                    assert evidence['status']=='已核验', evidence
                    reason.fill('尚未提交的下一条原因')
                    sales.get_by_placeholder('目标实际售价，例如 12.50').fill('13')
                    expect(sales.locator('.el-drawer').get_by_role('row').filter(has_text='12').get_by_text('已核验',exact=True)).to_be_visible(timeout=15000)
                    expect(reason).to_have_value('尚未提交的下一条原因')
                    expect(sales.get_by_placeholder('目标实际售价，例如 12.50')).to_have_value('13')
                    sales.screenshot(path=str(output/'sales-correction.png'),full_page=True)
                    print('CHECK', len(report['checks']) + 1, flush=True); report['checks'].append('actual UI takeover/correction desired12/approval/worker simulated verification')
                    sales.keyboard.press('Escape')
                    sales.get_by_role('link',name='通知收件箱',exact=True).click()
                    wait_until(lambda: any(n['delivery_state']=='demo' for n in sales.request.get(base+'/api/notifications').json()),'Demo notice missing')
                    sales.get_by_role('button',name='刷新通知',exact=True).click()
                    expect(sales.get_by_text('演示投递',exact=True).first).to_be_visible()
                    sales.get_by_role('button',name='确认已知悉',exact=True).first.click()
                    wait_until(lambda: any(n['ack_at'] for n in sales.request.get(base+'/api/notifications').json()),'Notice acknowledgment missing')
                    expect(sales.get_by_role('button',name='重试明确失败')).to_have_count(0)
                    sales.get_by_role('button',name='查看涉及商品',exact=True).first.click()
                    expect(sales.get_by_role('heading',name='通知涉及商品',exact=True)).to_be_visible()
                    sales.get_by_role('row').filter(has_text='DEMO-SKU-000').get_by_role('button',name='查看详情').click()
                    expect(sales.locator('.el-drawer').get_by_role('heading',name='DEMO-SKU-000',exact=True)).to_be_visible()
                    expect(sales.locator('.el-drawer').get_by_text('销售甲 / 演示主管',exact=True)).to_be_visible()
                    sales.keyboard.press('Escape')
                    sales.screenshot(path=str(output/'sales-notifications.png'),full_page=True)
                    print('CHECK', len(report['checks']) + 1, flush=True); report['checks'].append('demo evidence, business acknowledgment, scoped related-items detail link')
                    other_sales=session('sales-b')
                    assert all(i['key']['shop_id']=='DEMO-PH' for i in other_sales.request.get(base+'/api/items').json())
                    assert other_sales.request.get(base+f"/api/items/{own_item['id']}").status in (403,404)
                    manager=session('manager-a')
                    manager_items=manager.request.get(base+'/api/items').json()
                    assert manager_items and all(i['key']['shop_id']=='DEMO-MY' for i in manager_items)
                    assert manager.request.get(base+f"/api/items/{other_item['id']}").status in (403,404)
                    assert manager.request.get(base+'/api/settings').status==403
                    expect(manager.get_by_role('link',name='运行配置',exact=True)).to_have_count(0)
                    print('CHECK', len(report['checks']) + 1, flush=True); report['checks'].append('second sales and explicitly assigned manager scope isolation')
                    for name,page in [('admin',admin_page),('sales',sales),('manager',manager)]:
                        page.set_viewport_size({'width':390,'height':844})
                        page.screenshot(path=str(output/f'mobile-{name}.png'),full_page=True)
                        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 2'), '390px horizontal overflow'
                    print('CHECK', len(report['checks']) + 1, flush=True); report['checks'].append('390px admin ownership/sales notifications/manager overview layouts')
                    assert not report['javascript_errors'], report['javascript_errors']
                    assert not failures, failures
                    browser.close()
            report['result']='PASS'
        finally:
            stop.set()
            worker_thread.join(timeout=10)
            server.should_exit=True
            server_thread.join(timeout=10)
            listener.close()
            (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print('SALES UI PASS: '+ '; '.join(report['checks']) + '; no JavaScript errors; synthetic demo only')


if __name__=='__main__':
    main()
