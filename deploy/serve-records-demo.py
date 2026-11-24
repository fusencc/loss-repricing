"""独立临时 demo 数据库供记录页浏览器验收；不读取 .env、不启动 worker。"""
import argparse
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from app.config import Settings
from app.main import create_app
from app.models import Batch, Item, Execution, UserAccount, Ownership
from app.services.coordination import scope_key
import uvicorn


def seed(factory):
    products = ['ORG-102', 'CUP-205', 'BOX-318']
    with factory.begin() as session:
        for di, (dept, names) in enumerate([('Tiktok一部', ['林晓', '陈雨', '周宁', '许安']),
                                           ('Tiktok五部', ['苏晴', '江悦'])]):
            for si, name in enumerate(names):
                owner = f'demo-{di}-{si}'
                session.add(UserAccount(id=owner, username=owner, display_name=name, role='sales', active=True,
                    password_hash='unused-demo-account', created_at='2026-11-17T00:00:00+00:00',
                    updated_at='2026-11-17T00:00:00+00:00'))
                shop = f'Mori Living {di + 1}{si + 1}'
                session.add(Ownership(id=owner, scope_key=scope_key('TikTok Shop', shop, 'MY'),
                    platform='TikTok Shop', shop_id=shop, site='MY', owner_id=owner, autonomous=False))
                for ri in range(3):
                    ident = f'demo-{di}-{si}-{ri}'
                    day = f'2026-11-{17-ri}'
                    moment = f'{day}T02:{42-si*7:02}:00+00:00'
                    key = {'platform': 'TikTok Shop', 'shop_id': shop, 'site': 'MY', 'product_id': f'P-00{si}',
                        'item_id': f'00{ri}', 'sku': f'{products[ri]}-{di+1}{si+1}', 'warehouse': 'local'}
                    status = ['已核验', '待核验', '待处理'][ri] if si % 2 == 0 else '已核验'
                    reason = {'已核验': '有效价已回读，与目标一致', '待核验': '已提交，等待实际售价回读',
                              '待处理': '活动价未变化，需销售员跟进'}[status]
                    session.add(Batch(id=ident, run_key=ident, business_date=day, mode='demo', status='completed',
                        created_at=moment, snapshot={}, summary={}))
                    session.flush()
                    decision = {'current_actual': '32.90', 'target_actual': '36.90', 'upload_price': '43.41',
                        'trace': {'station_currency': 'MYR', 'online_rows': [{'key': key, 'profit_check': {'department': dept}}]}}
                    session.add(Item(id=ident, batch_id=ident, variant_id=ident, key=key, status=status,
                        phase='done', reason=reason, decision=decision, updated_at=moment))
                    session.flush()
                    if status != '待处理':
                        session.add(Execution(id=ident, item_id=ident, variant_id=ident,
                            state='verified' if status == '已核验' else 'unknown', task_id='TASK-'+ident,
                            request={'upload_price': '43.41'}, response={},
                            evidence={'actual_price': '36.90', 'confirmed_at': moment,
                                'effective_at': moment, 'effective_time_source': 'confirmed_at',
                                'price_record': {'shop_name': shop, 'prices': {
                                    'uploaded_tax_inclusive': {'amount': '43.41', 'currency': 'MYR', 'observed_at': moment},
                                    'tax_exclusive': {'amount': '40.00', 'currency': 'MYR', 'observed_at': moment},
                                    'actual_tax_inclusive': {'amount': '43.41', 'currency': 'MYR', 'observed_at': moment},
                                    'marketing': {'amount': '36.90', 'currency': 'MYR', 'observed_at': moment},
                                }}} if status == '已核验' else {},
                            created_at=moment, updated_at=moment))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18827)
    args = parser.parse_args()
    with TemporaryDirectory(prefix='records-glass-demo-') as temp:
        root = Path(temp)
        cfg = Settings(adapter_mode='file', database_url=f'sqlite:///{root / "demo.db"}', runtime_dir=root,
            admin_password='isolated-demo-only', session_secret='isolated-records-demo-session-secret',
            live_submit_enabled=False, coordination_required=False, _env_file=None)
        app = create_app(cfg)
        seed(app.state.factory)
        print(f'Isolated records demo: http://127.0.0.1:{args.port}', flush=True)
        uvicorn.run(app, host='127.0.0.1', port=args.port, log_level='warning', access_log=False)
