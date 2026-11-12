"""只读归档本程序真实执行和保护记录；demo/file记录不作为真实生效证明。"""
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import re

from sqlalchemy import create_engine, or_, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError, IntegrityError
from sqlalchemy.orm import sessionmaker

from app.adapters.normalization import identity_text, money, source_time
from app.domain.types import Site, VariantKey
from app.domain.engine import SITE_CURRENCIES
from typing import get_args
from app.models import Batch, Execution, Item, Protection, VariantGuard, CompanyPriceObservation
from app.adapters.base import SourceUnavailable
from app.services.erp_capture_store import write_verified



def _open_authoritative_engine(settings):
    explicit_url=settings.live_history_database_url.get_secret_value().strip()
    if not explicit_url and settings.adapter_mode!='live':
        raise ValueError('local_live_archive_not_bound')
    database_url=explicit_url or settings.database_url
    url=make_url(database_url)
    if url.drivername.startswith('sqlite') and (
            not url.database or url.database==':memory:' or not Path(url.database).is_file()):
        raise ValueError('local_archive_database_missing')
    binding='configured_live_history_database' if explicit_url else 'live_runtime_database'
    return create_engine(database_url,pool_pre_ping=True),binding


def record_company_observations(settings,capture,observations,*,factory=None):
    """只向权威真实档案登记外部观察；不会伪造Execution或移动首次确认时点。"""
    if not observations or (factory is None and (capture.get('synthetic',False) or capture.get('sample_only',False))):
        return {'status':'not_requested','observations':0}
    if factory is not None and not capture.get('synthetic',False):
        raise ValueError('注入的公司观察档案只可用于合成测试')
    engine=None
    try:
        if factory is None:
            engine,_=_open_authoritative_engine(settings)
            factory=sessionmaker(bind=engine,expire_on_commit=False)
        with factory.begin() as session:
            for observation in observations:
                key=VariantKey.model_validate(observation['key'])
                if (not re.fullmatch(r'[a-f0-9]{64}',observation['event_key'])
                        or observation['variant_id']!=key.target_identity()
                        or observation['state'] not in ('confirmed','unverified')
                        or observation['currency']!=SITE_CURRENCIES[key.site]
                        or money(observation['new_price'])<=money(observation['old_price'])
                        or money(observation['old_price'])<=0):
                    raise ValueError('公司价格观察身份或金额无效')
                first=source_time(observation['first_observed_at'])
                logged=source_time(observation['logged_complete_at'])
                confirmed=source_time(observation['first_confirmed_at']) if observation['first_confirmed_at'] else None
                if (logged>first or first>datetime.now(timezone.utc)
                        or (confirmed is not None and (confirmed<first or confirmed>datetime.now(timezone.utc)))
                        or (observation['state']=='confirmed')!=(confirmed is not None)):
                    raise ValueError('公司实价观察时序无效')
                existing=session.get(CompanyPriceObservation,observation['event_key'])
                if existing is None:
                    proposed=CompanyPriceObservation(event_key=observation['event_key'],log_id=observation['log_id'],
                        variant_id=key.target_identity(),key=key.model_dump(mode='json'),state=observation['state'],
                        currency=observation['currency'],old_price=observation['old_price'],new_price=observation['new_price'],
                        first_observed_at=first.astimezone(timezone.utc).isoformat(),
                        first_confirmed_at=confirmed.astimezone(timezone.utc).isoformat() if confirmed else None,
                        logged_complete_at=logged.astimezone(timezone.utc).isoformat(),
                        company_context=observation['company_context'],first_evidence=observation['evidence'],
                        confirmation_evidence=observation['evidence'] if confirmed else None,
                        created_at=datetime.now(timezone.utc).isoformat())
                    try:
                        with session.begin_nested():session.add(proposed);session.flush()
                        existing=proposed
                    except IntegrityError:
                        existing=session.scalar(select(CompanyPriceObservation).where(
                            CompanyPriceObservation.event_key==observation['event_key']).with_for_update())
                        if existing is None:raise
                if (existing.variant_id!=key.target_identity() or existing.log_id!=observation['log_id']
                        or existing.currency!=observation['currency']
                        or money(existing.old_price)!=money(observation['old_price'])
                        or money(existing.new_price)!=money(observation['new_price'])
                        or source_time(existing.logged_complete_at)!=logged):
                    raise ValueError('已存公司价格事件与本次证据冲突')
                if existing.state=='unverified' and confirmed is not None:
                    if first<source_time(existing.first_observed_at):
                        existing.first_observed_at=first.astimezone(timezone.utc).isoformat()
                        existing.first_evidence=observation['evidence']
                    existing.state='confirmed'
                    existing.first_confirmed_at=confirmed.astimezone(timezone.utc).isoformat()
                    existing.confirmation_evidence=observation['evidence']
        return {'status':'saved','observations':len(observations)}
    except SQLAlchemyError:
        raise SourceUnavailable('公司实价确认档案登记失败，须修复真实档案后重跑') from None
    finally:
        if engine is not None:engine.dispose()

def capture_local_price_history(settings, capture, *, factory=None):
    source_id = 'local-price-history-' + uuid4().hex
    path = Path(settings.runtime_dir) / 'local-price-history' / (source_id + '.json')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    result = {'schema_version':'local-live-price-history-v1','source_id':source_id,
        'erp_capture_id':capture['capture_id'],'synthetic':bool(capture.get('synthetic',False)),
        'sample_only':bool(capture.get('sample_only',False)),
        'queried_at':datetime.now(timezone.utc).isoformat(),'archive_binding':None,'executions':[],'guards':[],
        'protections':[],'company_observations':[],'issues':[],'pricing_ready':False,'artifact_file':str(path.resolve())}
    engine = None
    try:
        if factory is None:
            if result['synthetic']:
                raise ValueError('synthetic_archive_not_bound')
            try:
                engine,binding=_open_authoritative_engine(settings)
            except ValueError as error:
                if str(error)=='local_live_archive_not_bound':
                    result['issues'].append({'code':'local_live_archive_not_bound'})
                raise
            result['archive_binding']=binding
            factory = sessionmaker(bind=engine, expire_on_commit=False)
        if factory is not None and engine is None:
            if not result['synthetic']:
                raise ValueError('injected_archive_requires_synthetic_capture')
            result['archive_binding'] = 'injected_synthetic_test'
        with factory() as session:
            records = session.execute(select(Execution,Item.key,Batch.id)
                .join(Item,Execution.item_id==Item.id).join(Batch,Item.batch_id==Batch.id)
                .where(Batch.mode=='live').order_by(Execution.id)).all()
            for execution, key, batch_id in records:
                try:
                    key = VariantKey.model_validate(key).model_dump(mode='json')
                    evidence = execution.evidence
                    if not isinstance(evidence, dict):
                        raise ValueError()
                    if execution.state == 'verified':
                        if (evidence.get('mode') != 'live' or evidence.get('price_basis') != 'actual_including_vat'
                                or money(evidence.get('actual_price')) <= 0
                                or source_time(evidence.get('effective_at')) > datetime.now(timezone.utc)):
                            raise ValueError()
                    result['executions'].append({'execution_id':execution.id,'batch_id':batch_id,
                        'mode':'live','key':key,'state':execution.state,
                        'evidence':{name:evidence.get(name) for name in (
                            'mode','effective_at','confirmed_at','actual_price','price_basis')},
                        'created_at':execution.created_at,'updated_at':execution.updated_at})
                except (ValueError, TypeError, ArithmeticError):
                    result['issues'].append({'code':'local_execution_evidence_invalid','execution_id':execution.id})
            for row in session.scalars(select(CompanyPriceObservation)).all():
                try:
                    key=VariantKey.model_validate(row.key)
                    first=source_time(row.first_observed_at)
                    confirmed=source_time(row.first_confirmed_at) if row.first_confirmed_at else None
                    if (row.variant_id!=key.target_identity() or row.state not in ('confirmed','unverified')
                            or row.currency!=SITE_CURRENCIES[key.site]
                            or money(row.new_price)<=money(row.old_price) or money(row.old_price)<=0
                            or source_time(row.logged_complete_at)>first or first>datetime.now(timezone.utc)
                            or (row.state=='confirmed')!=(confirmed is not None)
                            or (confirmed is not None and (confirmed<first or confirmed>datetime.now(timezone.utc)))):
                        raise ValueError()
                    result['company_observations'].append({
                        'event_key':row.event_key,'log_id':row.log_id,'variant_id':row.variant_id,
                        'key':key.model_dump(mode='json'),'state':row.state,'currency':row.currency,
                        'old_price':row.old_price,'new_price':row.new_price,'first_observed_at':row.first_observed_at,
                        'first_confirmed_at':row.first_confirmed_at,'logged_complete_at':row.logged_complete_at,
                        'company_context':row.company_context,'evidence':{'first_observation':row.first_evidence,
                            'first_confirmation':row.confirmation_evidence}})
                except (ValueError,TypeError,ArithmeticError):
                    result['issues'].append({'code':'company_observation_invalid','event_key':row.event_key})
            guards = session.execute(select(VariantGuard.variant_id,VariantGuard.execution_id,
                    VariantGuard.last_effective_at,Batch.mode)
                .outerjoin(Execution,VariantGuard.execution_id==Execution.id)
                .outerjoin(Item,Execution.item_id==Item.id).outerjoin(Batch,Item.batch_id==Batch.id)
                .where(or_(VariantGuard.execution_id.is_not(None),VariantGuard.last_effective_at.is_not(None)))).all()
            result['guards'] = [{'variant_id':identity,'execution_id':execution_id,
                'last_effective_at':effective,'linked_mode':mode}
                for identity,execution_id,effective,mode in guards if mode in (None,'live')]
            for row in session.scalars(select(Protection).where(Protection.active.is_(True))).all():
                try:
                    identity_text(row.shop_id)
                    identity_text(row.main_sku)
                    if row.site not in get_args(Site):
                        raise ValueError()
                    result['protections'].append({'id':row.id,'shop_id':row.shop_id,'site':row.site,
                        'main_sku':row.main_sku,'reason':row.reason,'created_at':row.created_at})
                except ValueError:
                    result['issues'].append({'code':'local_protection_scope_invalid','protection_id':row.id})
    except (SQLAlchemyError, ValueError):
        result['issues'].append({'code':'local_archive_read_failed'})
    finally:
        if engine is not None:
            engine.dispose()
    result['acquisition_complete'] = not result['issues']
    result['completed_at'] = datetime.now(timezone.utc).isoformat()
    write_verified(path,result)
    return result
