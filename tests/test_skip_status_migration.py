"""状态替换只迁移展示字段，执行证据、锁与原始来源不得变更。"""
import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import JSON, Column, MetaData, String, Table, create_engine, select


def test_skip_status_migration_updates_projection_and_summary_only(tmp_path):
    path = Path(__file__).parents[1] / 'backend/migrations/versions/f74a6e921c30_rename_skip_status.py'
    spec = importlib.util.spec_from_file_location('rename_skip_status', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(f'sqlite:///{tmp_path / "migration.db"}')
    metadata = MetaData()
    items = Table('items', metadata, Column('id', String, primary_key=True), Column('status', String),
                  Column('reason', String), Column('decision', JSON))
    batches = Table('batches', metadata, Column('id', String, primary_key=True), Column('summary', JSON))
    executions = Table('executions', metadata, Column('id', String, primary_key=True), Column('state', String),
                       Column('request', JSON), Column('response', JSON), Column('evidence', JSON))
    metadata.create_all(engine)
    old = '本轮不调'
    trace = {'source_rows': [{'reason': old}], 'source_versions': {'version': old}}
    receipt = {'status': 'accepted', 'raw_text': old}
    with engine.begin() as connection:
        connection.execute(items.insert(), [
            {'id': 'old', 'status': old, 'reason': f'近72小时保护，{old}',
             'decision': {'status': old, 'reason': old, 'trace': trace}},
            {'id': 'pending', 'status': '待核验', 'reason': '结果未知', 'decision': None}])
        connection.execute(batches.insert(), [{'id': 'batch', 'summary': {old: 2, '不调价': 1, '待核验': 1}},
                                               {'id': 'zero', 'summary': {old: 0}}])
        connection.execute(executions.insert(), {'id': 'execution', 'state': 'unknown',
            'request': receipt, 'response': receipt, 'evidence': receipt})
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        # 再跑数据转换没有重复计数；Alembic 正常只应用一次。
        migration.upgrade()
        result = {r.id: r for r in connection.execute(select(items))}
        assert result['old'].status == '不调价'
        assert result['old'].reason == '近72小时保护，不调价'
        assert result['old'].decision == {'status': '不调价', 'reason': '不调价', 'trace': trace}
        assert result['pending'].status == '待核验' and result['pending'].decision is None
        assert dict(connection.execute(select(batches.c.id, batches.c.summary)).all()) == {
            'batch': {'不调价': 3, '待核验': 1}, 'zero': {'不调价': 0}}
        saved = connection.execute(select(executions)).one()
        assert saved.state == 'unknown'
        assert saved.request == saved.response == saved.evidence == receipt
    engine.dispose()
