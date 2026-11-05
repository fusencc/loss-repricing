"""Canonical execution targets; preserve every legacy pending request and cooldown.

Run with workers stopped. This data migration deliberately keeps legacy guard rows
and all original Item keys/Decision JSON for audit; only execution guard identity moves.
"""
from datetime import datetime, timezone
from hashlib import sha256
import json
from alembic import op
import sqlalchemy as sa

revision = 'c41a62f0b931'
down_revision = '3e2856f6b847'
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    metadata = sa.MetaData()
    items = sa.Table('items', metadata, autoload_with=connection)
    executions = sa.Table('executions', metadata, autoload_with=connection)
    guards = sa.Table('variant_guards', metadata, autoload_with=connection)
    item_rows = {row['id']: dict(row) for row in connection.execute(sa.select(items)).mappings()}
    execution_rows = [dict(row) for row in connection.execute(sa.select(executions)).mappings()]
    guard_rows = {row['variant_id']: dict(row) for row in connection.execute(sa.select(guards)).mappings()}
    identities, targets, migrated = {}, {}, {}

    def target_for(key):
        fields = tuple(key[name] for name in ('platform', 'shop_id', 'site', 'product_id', 'item_id'))
        if not all(isinstance(value, str) and value.strip() for value in fields):
            raise RuntimeError('历史执行目标不完整，停止迁移以保留旧锁')
        return sha256(json.dumps(fields, ensure_ascii=False).encode('utf-8')).hexdigest()

    def remember(old, target):
        if old in identities and identities[old] != target:
            raise RuntimeError('历史身份对应多个实际目标，停止迁移')
        identities[old] = target
        targets.setdefault(target, {'pending': [], 'effective': []})

    def effective(target, value):
        if value:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                raise RuntimeError('历史生效时间缺少时区，停止迁移')
            targets[target]['effective'].append(parsed.astimezone(timezone.utc).isoformat())

    for item in item_rows.values():
        remember(item['variant_id'], target_for(item['key']))
    for execution in execution_rows:
        target = target_for(execution['request']['key'])
        if target != target_for(item_rows[execution['item_id']]['key']):
            raise RuntimeError('历史请求与记录目标不一致，停止迁移')
        remember(execution['variant_id'], target)
        migrated[execution['id']] = target
        if execution['state'] not in {'verified', 'failed'}:
            targets[target]['pending'].append((execution['created_at'], execution['id']))
        elif execution['state'] == 'verified':
            # 无精确生效时间时取确认时间；不使用上传受理时间启动72小时保护。
            evidence = execution['evidence'] or {}
            effective(target, evidence.get('effective_at') or execution['updated_at'])
    for old, guard in guard_rows.items():
        target = identities.get(old)
        if target is None and old in targets:
            target = old  # 兼容空库建表后已产生的新版目标锁。
        if target is None:
            if guard['execution_id'] or guard['last_effective_at']:
                raise RuntimeError('无法还原历史锁目标，停止迁移；不得清空未知锁或生效历史')
            continue
        if guard['execution_id']:
            if migrated.get(guard['execution_id']) != target:
                raise RuntimeError('历史锁请求缺失或目标不一致，停止迁移')
        effective(target, guard['last_effective_at'])

    # 全部映射核验通过后才写入。一个目标保留所有Execution，锁指针仅代表其中一个；
    # 核验器每次从全部未终结请求重选指针，不能因一个旧请求结束而遗漏其余请求。
    for execution in execution_rows:
        connection.execute(executions.update().where(executions.c.id == execution['id'])
                           .values(variant_id=migrated[execution['id']]))
    for target, history in targets.items():
        pending = sorted(history['pending'])
        values = dict(execution_id=pending[0][1] if pending else None,
                      last_effective_at=max(history['effective']) if history['effective'] else None)
        if target in guard_rows:
            connection.execute(guards.update().where(guards.c.variant_id == target).values(**values))
        else:
            connection.execute(guards.insert().values(variant_id=target, **values))


def downgrade():
    raise RuntimeError('执行身份安全迁移不支持自动降级；降级可能恢复SKU改名绕过锁的问题')
