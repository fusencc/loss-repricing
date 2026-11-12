from copy import deepcopy
from datetime import datetime,timezone
from app.config import Settings
from app.db import Base,create_database
from test_company_price_history import case


def archive(tmp_path):
    engine,factory=create_database('sqlite://');Base.metadata.create_all(engine)
    settings=Settings(_env_file=None,runtime_dir=tmp_path)
    capture={'capture_id':'C1','synthetic':True,'sample_only':False}
    return engine,factory,settings,capture


def observation():
    from app.adapters.company_price_history import classify_company_event
    t,r,p,c=case()
    return classify_company_event(r,p,target=t,control=c)['observation']


def test_repeat_company_confirmation_preserves_first_actual_confirmation(tmp_path):
    from app.services.local_price_history import record_company_observations,capture_local_price_history
    engine,factory,settings,capture=archive(tmp_path);first=observation()
    record_company_observations(settings,capture,[first],factory=factory)
    later=deepcopy(first);later['first_confirmed_at']='2026-11-11T03:00:00+00:00'
    record_company_observations(settings,capture,[later],factory=factory)
    restored=capture_local_price_history(settings,{'capture_id':'C2','synthetic':True,'sample_only':False},factory=factory)
    assert restored['acquisition_complete'] and len(restored['company_observations'])==1
    row=restored['company_observations'][0]
    assert datetime.fromisoformat(row['first_confirmed_at'])==datetime(2026,11,11,2,tzinfo=timezone.utc)
    assert restored['executions']==[]  # 公司观察不冒充本程序执行。
    engine.dispose()


def test_company_unverified_observation_survives_query_window_and_can_be_confirmed_later(tmp_path):
    from app.services.local_price_history import record_company_observations,capture_local_price_history
    engine,factory,settings,capture=archive(tmp_path);pending=observation();pending.update(state='unverified',first_confirmed_at=None)
    record_company_observations(settings,capture,[pending],factory=factory)
    current=capture_local_price_history(settings,{'capture_id':'NEW-WINDOW','synthetic':True,'sample_only':False},factory=factory)
    assert current['company_observations'][0]['state']=='unverified'
    confirmed=deepcopy(pending);confirmed.update(state='confirmed',first_confirmed_at='2026-11-11T03:00:00+00:00')
    record_company_observations(settings,capture,[confirmed],factory=factory)
    current=capture_local_price_history(settings,capture,factory=factory)
    assert current['company_observations'][0]['state']=='confirmed'
    assert current['company_observations'][0]['first_confirmed_at']=='2026-11-11T03:00:00+00:00'
    engine.dispose()


def test_sample_never_writes_to_authoritative_live_database(tmp_path):
    from app.services.local_price_history import record_company_observations
    settings=Settings(_env_file=None,runtime_dir=tmp_path,live_history_database_url='sqlite:///'+str(tmp_path/'must-not-create.db'))
    result=record_company_observations(settings,{'capture_id':'C1','synthetic':False,'sample_only':True},[observation()])
    assert result['status']=='not_requested' and not (tmp_path/'must-not-create.db').exists()


def test_out_of_order_confirmation_retains_valid_first_observation(tmp_path):
    from app.services.local_price_history import record_company_observations,capture_local_price_history
    engine,factory,settings,capture=archive(tmp_path)
    pending=observation();pending.update(state='unverified',first_confirmed_at=None,first_observed_at='2026-11-11T03:00:00+00:00')
    record_company_observations(settings,capture,[pending],factory=factory)
    record_company_observations(settings,capture,[observation()],factory=factory)
    restored=capture_local_price_history(settings,capture,factory=factory)
    assert restored['acquisition_complete']
    assert restored['company_observations'][0]['first_observed_at']=='2026-11-11T02:00:00+00:00'
    engine.dispose()
