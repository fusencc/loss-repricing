"""只读归档本程序真实执行和保护记录；demo/file记录不作为真实生效证明。"""
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, or_, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from app.adapters.normalization import identity_text, money, source_time
from app.domain.types import Site, VariantKey
from typing import get_args
from app.models import Batch, Execution, Item, Protection, VariantGuard
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


def capture_local_price_history(settings, capture, *, factory=None):
    source_id = 'local-price-history-' + uuid4().hex
    path = Path(settings.runtime_dir) / 'local-price-history' / (source_id + '.json')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    result = {'schema_version':'local-live-price-history-v1','source_id':source_id,
        'erp_capture_id':capture['capture_id'],'synthetic':bool(capture.get('synthetic',False)),
        'sample_only':bool(capture.get('sample_only',False)),
        'queried_at':datetime.now(timezone.utc).isoformat(),'archive_binding':None,'executions':[],'guards':[],
        'protections':[],'history_scope':'own_program_only','issues':[],'pricing_ready':False,'artifact_file':str(path.resolve())}
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
