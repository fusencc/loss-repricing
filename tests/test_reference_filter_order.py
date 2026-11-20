from types import SimpleNamespace
from app.services import erp_workflow


def test_history_filter_precedes_sales_and_unknown_history_candidates_are_queried(monkeypatch):
    events=[]
    targets=[{'erp_sku':name,'key':{'platform':'TikTok','shop_id':'S','site':'MY','product_id':'P',
        'item_id':name,'sku':name,'warehouse':'W'}} for name in ('zero','changed','missing')]
    def history(settings, keys):
        events.append(('history',[k.item_id for k in keys]))
        return {'source_id':'H','window_start':'2026-11-14T10:00:00+00:00','window_end':'2026-11-17T10:00:00+00:00',
            'records':{'zero':{'status':'ok','count':0},'changed':{'status':'ok','count':1}}}
    def sales(settings,capture,targets,**kwargs):
        events.append(('sales',[t['erp_sku'] for t in targets]));return {'acquisition_complete':False}
    monkeypatch.setattr(erp_workflow,'capture_recent_price_changes',history)
    monkeypatch.setattr(erp_workflow,'capture_goods_sales',sales)
    _,eligible,reference,filtered=erp_workflow.capture_candidate_references(SimpleNamespace(),{},targets)
    assert events==[('history',['zero','changed','missing']),('sales',['zero','missing'])]
    assert [t['erp_sku'] for t in eligible]==['zero','missing']
    assert [t['status'] for t in filtered]==['passed','history_protected','history_unknown']
    assert reference['acquisition_complete'] is False  # 参考缺失不移除通过历史的目标。
