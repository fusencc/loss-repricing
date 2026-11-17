"""已核验真实执行的只读补档：不调价、不改变历史确认时间和72小时保护。"""
from copy import deepcopy
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from app.db import transaction
from app.adapters.normalization import money
from app.domain.engine import SITE_CURRENCIES
from app.domain.types import VariantKey
from app.models import Execution, Item, Batch, Audit
from app.services.erp_capture_store import write_verified
from app.services.store import stamp


def validate_price_record_binding(record, execution_id, key, uploaded_price, marketing_price):
    """外部证据入库前核对执行身份及四价，避免正确执行关联到别的商品档案。"""
    if (record.get('schema_version')!='execution-price-record-v1' or record.get('execution_id')!=execution_id
            or record.get('key')!=key.model_dump(mode='json')):
        raise ValueError('价格记录与本次执行身份不一致')
    prices=record['prices']
    if set(prices)!={'uploaded_tax_inclusive','tax_exclusive','actual_tax_inclusive','marketing'}:
        raise ValueError('价格记录缺少四种独立口径')
    for value in prices.values():
        if money(value['amount'])<=0 or value['currency']!=SITE_CURRENCIES[key.site] or not value.get('source_field'):
            raise ValueError('价格记录的金额、币种或来源无效')
    if money(prices['uploaded_tax_inclusive']['amount'])!=uploaded_price or money(prices['marketing']['amount'])!=marketing_price:
        raise ValueError('价格记录与原始上传意图或实际回读不一致')


def archive_verified_price_record(factory, adapter, execution_id, directory):
    with factory() as session:
        execution = session.get(Execution, execution_id)
        if execution is None: raise ValueError('执行记录不存在')
        item = session.get(Item, execution.item_id)
        batch = session.get(Batch, item.batch_id)
        if batch.mode!='live' or adapter.mode!='live' or execution.state!='verified':
            raise ValueError('只读补档仅用于已核验的真实执行')
        key = VariantKey.model_validate(execution.request['key'])
        original = deepcopy(execution.evidence)
        request = deepcopy(execution.request)
        task_id = execution.task_id
    # 唯一外部动作是lookup；不经过run_correction或submit，不临时开启提交开关。
    result = adapter.lookup(execution_id, task_id, key)
    record = result.price_record
    if (result.status!='accepted' or record is None or record.get('execution_id')!=execution_id
            or record.get('key')!=key.model_dump(mode='json')
            or record.get('schema_version')!='execution-price-record-v1'):
        raise ValueError('未取得与原执行绑定的四价及官方日志证据')
    validate_price_record_binding(record,execution_id,key,Decimal(request['upload_price']),result.actual_price)
    prices = record['prices']
    archive_id = 'price-record-'+uuid4().hex
    archive = Path(directory)/archive_id
    archive.mkdir(parents=True)
    write_verified(archive/'price-record.json',record)
    write_verified(archive/'original-verification.json',{k:v for k,v in original.items() if k!='price_record'})
    official = record['official_queries']
    write_verified(archive/'official-queries.json',official)
    rows = [('上传含税价','uploaded_tax_inclusive'),('日志税前价','tax_exclusive'),
            ('平台实际含税零售价','actual_tax_inclusive'),('营销价','marketing')]
    lines = ['官方提价日志及四种价格存档', '执行ID：'+execution_id, '店铺：'+record['shop_name'],
             '产品ID：'+key.product_id, '子商品ID：'+key.item_id, '子SKU：'+key.sku,
             '记录时间：'+record['recorded_at'], '']
    for label,name in rows:
        p=prices[name]
        lines.extend([label+'：'+p['amount']+' '+p['currency'], '来源字段：'+p['source_field'],
                      '观察时间：'+p['observed_at']])
    lines.extend(['', '官方日志数量查询状态：'+official['count_query']['status'],
        '官方查询数量：'+str(official['count_query'].get('count')),
        '官方最近成功价查询状态：'+official['latest_success_query']['status'],
        '官方最近成功金额：'+str(official['latest_success_query'].get('amount')),
        '与同目标日志税前价比较：'+record['official_comparison'],
        '官方单值接口没有返回币种/任务ID/生效时间，金额相同不能单独证明执行绑定。',
        '本次为只读补充观察，原执行状态、生效确认时间及72小时保护保持原记录。'])
    (archive/'价格日志存档.txt').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    manifest=[{'file':p.name,'bytes':p.stat().st_size,'sha256':sha256(p.read_bytes()).hexdigest()} for p in sorted(archive.iterdir())]
    write_verified(archive/'sha256-manifest.json',manifest)
    with transaction(factory) as session:
        current=session.get(Execution,execution_id,with_for_update=True)
        if current.state!='verified' or current.request!=request or current.evidence!=original:
            raise ValueError('执行证据在查询期间变化，本次未回写数据库')
        current.evidence={**current.evidence,'price_record':record}
        session.add(Audit(id=str(uuid4()),action='official_price_log_archived',actor='user_authorized_readonly',
            created_at=stamp(),detail={'execution_id':execution_id,'archive':str(archive.resolve()),
                'price_record_sha256':sha256((archive/'price-record.json').read_bytes()).hexdigest(),
                'recorded_at':record['recorded_at']}))
    return {'execution_id':execution_id,'execution_state':'verified','archive_directory':archive_id,
            'archive_path':str(archive.resolve()),'prices':prices,'official_comparison':record['official_comparison']}
