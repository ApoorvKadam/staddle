from core.tilden import playbooks


def test_unit_readiness_playbook_is_data_with_a_record_based_finish():
    book = playbooks.get('make-unit-ready')
    assert {'lease', 'walk_notes', 'work_orders', 'readiness', 'availability'} <= set(book['reads'])
    assert {'open_work_order', 'link_reports', 'hold_unit', 'release_unit', 'request_inspection', 'hand_to_staff'} <= set(book['tools'])
    assert book['done'] == 'readiness'
    assert book['steps']
    book['tools'].clear()
    assert playbooks.get('make-unit-ready')['tools']
