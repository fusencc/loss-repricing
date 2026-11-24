from types import SimpleNamespace
from app.services import erp_workflow


def test_history_filter_keeps_unknown_candidates_without_collecting_sales(monkeypatch):
    events=[]
    targets=[{'erp_sku':name,'key':{'platform':'TikTok','shop_id':'S','site':'MY','product_id':'P',
        'item_id':name,'sku':name,'warehouse':'W'}} for name in ('zero','changed','missing')]
    def history(settings, keys, **kwargs):
        events.append(('history',[k.item_id for k in keys]))
        return {'source_id':'H','window_start':'2026-11-14T10:00:00+00:00','window_end':'2026-11-17T10:00:00+00:00',
            'records':{'zero':{'status':'ok','count':0},'changed':{'status':'ok','count':1}}}
    monkeypatch.setattr(erp_workflow,'capture_recent_price_changes',history)
    source,eligible,filtered=erp_workflow.filter_candidate_history(SimpleNamespace(),targets)
    assert events==[('history',['zero','changed','missing'])]
    assert source['source_id']=='H'
    assert [t['erp_sku'] for t in eligible]==['zero','missing']
    assert [t['status'] for t in filtered]==['passed','history_protected','history_unknown']


def test_empty_history_filter_skips_external_collection(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError('空候选无需查询官方历史')
    monkeypatch.setattr(erp_workflow,'capture_recent_price_changes',unexpected)
    assert erp_workflow.filter_candidate_history(SimpleNamespace(),[])==(None,[],[])
