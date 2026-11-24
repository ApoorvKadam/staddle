"""The records and permitted work for each objective."""
from copy import deepcopy

PLAYBOOKS = {
    'make-unit-ready': {
        'reads': ['lease', 'walk_notes', 'work_orders', 'readiness', 'availability'],
        'steps': [
            'Read the lease, walk notes and open work orders.',
            'Link reports of the same fault and open work orders where needed.',
            'Keep the unit off the rentable list while work is open.',
            'Request an inspection and wait for repair and inspection records.',
            'Check readiness and release the unit when the records prove it is ready.',
            'Hand conflicting records or work outside authority to staff with evidence.',
        ],
        'tools': ['open_work_order', 'link_reports', 'hold_unit', 'release_unit',
                  'request_inspection', 'hand_to_staff'],
        'done': 'readiness',
    },
}


def get(name: str) -> dict:
    return deepcopy(PLAYBOOKS[name])
