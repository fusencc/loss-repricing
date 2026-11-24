from datetime import datetime, timezone
import json
from pathlib import Path

from app.config import Settings
from app.db import Base, create_database
from app.models import Batch, Item, Execution, Protection


def test_local_archive_excludes_demo_and_file_and_keeps_live_pending(tmp_path):
    from app.services.local_price_history import capture_local_price_history
    engine,factory=create_database('sqlite://')
    Base.metadata.create_all(engine)
    stamp='2026-11-11T06:00:00+00:00'
    key={'platform':'TikTok','shop_id':'SHOP','site':'PH','product_id':'P','item_id':'I','sku':'S','warehouse':'WH'}
    with factory.begin() as session:
        for mode,state in [('demo','verified'),('file','verified'),('live','verified'),('live','unknown')]:
            identity=mode+'-'+state
            session.add(Batch(id=identity,run_key=identity,business_date='2026-11-11',mode=mode,status='done',created_at=stamp,snapshot={},summary={}))
            session.flush()
            session.add(Item(id=identity,batch_id=identity,variant_id=identity,key=key,status='待处理',phase=state,reason='',decision={},updated_at=stamp))
            session.flush()
            session.add(Execution(id=identity,item_id=identity,variant_id=identity,state=state,request={},response={},
                evidence={'mode':mode,'effective_at':stamp,'actual_price':'10','price_basis':'actual_including_vat'},created_at=stamp,updated_at=stamp))
        session.add(Protection(id='PROTECT',shop_id='SHOP',site='PH',main_sku='MAIN',active=True,reason='人工保护',created_at=stamp))
    result=capture_local_price_history(Settings(_env_file=None,runtime_dir=tmp_path),
        {'capture_id':'C1','synthetic':True,'sample_only':False},factory=factory)
    assert result['acquisition_complete']
    assert {r['execution_id'] for r in result['executions']}=={'live-verified','live-unknown'}
    assert result['executions'][0]['mode']=='live'
    assert len(result['protections'])==1
    assert json.loads(Path(result['artifact_file']).read_text())==result
    engine.dispose()


def test_missing_database_is_not_an_empty_verified_archive(tmp_path):
    from app.services.local_price_history import capture_local_price_history
    target=tmp_path/'missing.db'
    result=capture_local_price_history(Settings(_env_file=None,runtime_dir=tmp_path,database_url='sqlite:///'+str(target)),
        {'capture_id':'C1','synthetic':False,'sample_only':False})
    assert not target.exists() and not result['acquisition_complete']
    assert result['issues'] and result['executions']==[]


def test_demo_database_cannot_prove_real_archive_absence(tmp_path):
    from app.services.local_price_history import capture_local_price_history
    target=tmp_path/'demo.db';engine,_=create_database('sqlite:///'+str(target));Base.metadata.create_all(engine);engine.dispose()
    settings=Settings(_env_file=None,runtime_dir=tmp_path,database_url='sqlite:///'+str(target),adapter_mode='file')
    result=capture_local_price_history(settings,{'capture_id':'C1','synthetic':False,'sample_only':False})
    assert not result['acquisition_complete']
    assert any(row['code']=='local_live_archive_not_bound' for row in result['issues'])


def test_explicit_authoritative_archive_is_read_without_enabling_live_submission(tmp_path):
    from app.services.local_price_history import capture_local_price_history
    target=tmp_path/'real.db';url='sqlite:///'+str(target)
    engine,_=create_database(url);Base.metadata.create_all(engine);engine.dispose()
    settings=Settings(_env_file=None,runtime_dir=tmp_path,adapter_mode='file',live_submit_enabled=False,
        database_url='sqlite:///'+str(tmp_path/'unused-demo.db'),live_history_database_url=url)
    result=capture_local_price_history(settings,{'capture_id':'C1','synthetic':False,'sample_only':False})
    assert result['acquisition_complete'] and result['archive_binding']=='configured_live_history_database'
    assert result['executions']==[] and result['protections']==[]
    assert not (tmp_path/'unused-demo.db').exists() and settings.live_submit_enabled is False
