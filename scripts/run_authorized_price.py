"""单商品人工授权入口。--check只读；--execute复用修正服务；重复运行只核验原执行。"""
import argparse
import json
from pathlib import Path
from decimal import Decimal
from uuid import uuid4
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from sqlalchemy import select
from app.adapters.manual_live import ManualLiveAdapter
from app.config import get_settings
from app.db import create_database, transaction
from app.domain.money import MoneyRatio
from app.models import Batch, Item, Execution, Audit
from app.models.coordination import Correction
from app.services.execution import PricingService
from app.services.corrections import manual_decision, run_correction
from app.services.price_control import lock_control, price_baseline
from app.services.store import utcnow, stamp, BEIJING, settings_value, update_settings, write_decisions
from app.services.erp_capture_store import write_verified


def run(path, execute=False):
    path = Path(path).resolve()
    authorization = json.loads(path.read_text(encoding='utf-8-sig'))
    settings = get_settings()
    database = settings.live_history_database_url.get_secret_value()
    if not database: raise ValueError('缺少独立真实执行档案绑定')
    cfg = settings.model_copy(update={'adapter_mode': 'live', 'database_url': database, 'live_submit_enabled': execute})
    engine, factory = create_database(database)
    adapter = ManualLiveAdapter(cfg, authorization, path.parent)
    service = PricingService(factory, adapter, cfg)
    correction_id = authorization['id']
    with factory() as session:
        correction = session.get(Correction, correction_id)
        existing = correction is not None
    if not existing:
        snapshot = service._enrich(adapter.collect(utcnow().astimezone(BEIJING).date()))
        online = snapshot.online[0]
        desired = (MoneyRatio(Decimal(authorization['upload_price'])) * MoneyRatio(online.marketing_price) / MoneyRatio(online.retail_price)).decimal()
        decision = manual_decision(snapshot, online, desired, authorization['reason'], correction_id, utcnow())
        if decision.upload_price != Decimal(authorization['upload_price']): raise ValueError('指定上传价与修正模型结果不一致')
        check = {'decision': decision.model_dump(mode='json'), 'authorization': authorization,
            'full_profit': str(online.profit_check.model.profit_exact(MoneyRatio(desired)).decimal()),
            'full_margin': str((online.profit_check.model.profit_exact(MoneyRatio(desired)) / MoneyRatio(desired)).decimal()),
            'submitted': False}
        write_verified(path.parent / 'pre-submit-check.json', check)
        print(json.dumps({'stage': 'preflight_passed', 'upload': str(decision.upload_price), 'expected_actual': str(desired),
            'full_profit': check['full_profit'], 'full_margin': check['full_margin']}, ensure_ascii=False), flush=True)
        if not execute:
            engine.dispose()
            return
        with transaction(factory) as session:
            control = lock_control(session, online.key)
            if control.mode not in ('auto','manual'): raise ValueError('已有价格冲突，不能接管执行')
            control.mode, control.version = 'manual', control.version+1
            control.reason, control.changed_by, control.updated_at = authorization['reason'], 'admin', stamp()
            control.baseline = price_baseline(online)
            batch_id = str(uuid4())
            session.add(Batch(id=batch_id, run_key='manual-authorization:'+correction_id, mode='live',
                business_date=authorization['business_date'], status='completed', created_at=stamp(),
                snapshot=snapshot.model_dump(mode='json'), summary={'manual_authorization': correction_id}))
            session.flush()
            write_decisions(session, batch_id, [decision])
            item = session.scalar(select(Item).where(Item.batch_id==batch_id))
            session.add(Correction(id=correction_id, target_id=online.key.target_identity(), item_id=item.id,
                desired_actual=str(desired), reason=authorization['reason'], requested_by='admin', approved_by='admin',
                state='queued', control_version=control.version, baseline=decision.trace,
                created_at=stamp(), updated_at=stamp(), result={}))
            session.add(Audit(id=str(uuid4()), action='manual_price_authorized', actor='user_authorized_codex',
                detail={'authorization': authorization, 'archive': str(path.parent)}, created_at=stamp()))
    elif not execute:
        print(json.dumps({'stage': 'already_recorded', 'id': correction_id}, ensure_ascii=False))
        engine.dispose()
        return
    prior = settings_value(factory)
    try:
        # 当次进程只运行这一项；仍保留全局暂停/站点暂停，自动调价不在本次授权内。
        update_settings(factory, {'pilot_shops': [adapter.key.shop_id], 'site_enabled': [adapter.key.site],
            'autonomous_enabled': False, 'schedule_enabled': False}, actor='user_authorized_codex')
        batch_id = run_correction(service, correction_id)
        with factory() as session:
            correction = session.get(Correction, correction_id)
            execution = session.get(Execution, correction.execution_id) if correction.execution_id else None
            result = {'authorization_id': correction_id, 'batch_id': batch_id, 'correction_state': correction.state,
                'correction_result': correction.result, 'execution_id': correction.execution_id,
                'execution_state': execution.state if execution else None,
                'response': execution.response if execution else None, 'evidence': execution.evidence if execution else None,
                'archive': str(path.parent)}
        write_verified(path.parent/'result.json', result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        update_settings(factory, prior, actor='user_authorized_codex')
        engine.dispose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('authorization')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    run(args.authorization, args.execute)
