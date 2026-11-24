from app.models import Execution, Item
from test_records_api import client


def test_record_projection_exposes_only_same_execution_status_evidence(client):
    with client.app.state.factory.begin() as session:
        item = session.get(Item, 'i0')
        item.phase = 'failed'
        session.add(Execution(id='e0', item_id='i0', variant_id=item.variant_id, state='failed',
            request={}, response={'status': 'accepted', 'message': 'private submit details'},
            evidence={'row_status': 'rejected', 'message': 'private lookup details'},
            created_at=item.updated_at, updated_at=item.updated_at))
    rows = {row['id']: row for row in client.get('/api/records').json()['items']}
    assert rows['i0']['phase'] == 'failed'
    assert rows['i0']['execution']['submit_status'] == 'accepted'
    assert rows['i0']['execution']['row_status'] == 'rejected'
    assert 'private' not in str(rows['i0']['execution'])
    assert rows['i3']['execution'] is None
