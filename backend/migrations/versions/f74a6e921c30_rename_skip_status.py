"""统一不调价状态及展示原因；原始来源和执行回执保持不变。"""
from alembic import op
import sqlalchemy as sa

revision = 'f74a6e921c30'
down_revision = 'e63f8c2d05b1'
branch_labels = None
depends_on = None


def _rename(old, new):
    connection = op.get_bind()
    items = sa.table('items', sa.column('id', sa.String), sa.column('status', sa.String),
                     sa.column('reason', sa.Text), sa.column('decision', sa.JSON))
    batches = sa.table('batches', sa.column('id', sa.String), sa.column('summary', sa.JSON))
    # 仅改规范状态和展示原因，不能递归替换 trace、原始响应或 Execution 请求/回执。
    for row in connection.execute(sa.select(items)).mappings().all():
        values = {}
        if row['status'] == old:
            values['status'] = new
        if isinstance(row['reason'], str) and old in row['reason']:
            values['reason'] = row['reason'].replace(old, new)
        decision = row['decision']
        if isinstance(decision, dict):
            updated = dict(decision)
            if updated.get('status') == old:
                updated['status'] = new
            if isinstance(updated.get('reason'), str):
                updated['reason'] = updated['reason'].replace(old, new)
            if updated != decision:
                values['decision'] = updated
        if values:
            connection.execute(items.update().where(items.c.id == row['id']).values(**values))
    # 批次详情直接消费持久 summary 状态计数；一次性合并旧键，不保留运行时双轨逻辑。
    for row in connection.execute(sa.select(batches)).mappings().all():
        summary = row['summary']
        if isinstance(summary, dict) and old in summary:
            updated = dict(summary)
            updated[new] = updated.get(new, 0) + updated.pop(old)
            connection.execute(batches.update().where(batches.c.id == row['id']).values(summary=updated))


def upgrade():
    _rename('本轮不调', '不调价')


def downgrade():
    _rename('不调价', '本轮不调')
