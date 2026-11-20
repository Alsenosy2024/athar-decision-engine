"""Keep observed process routes independent of causal enterprise edges."""
from app.process_mining import store


def with_process_flow(connection, payload, company_id):
    record = store.read(connection, company_id)
    if record is None:
        return {**payload, 'flow_source': 'default', 'flow_roads': None, 'process_mining': None}

    report = store.report(record)
    departments = {row['id']: row for row in payload.get('departments', [])}
    for row in report['departments']:
        departments.setdefault(row['id'], {'id': row['id'], 'name_ar': row['name_ar'],
                                          'node_ids': [], 'headline_id': None})
    return {
        **payload, 'departments': list(departments.values()), 'flow_source': report['source'],
        'flow_roads': [row for row in report['transitions'] if row['from_department'] != row['to_department']],
        'process_mining': {key: value for key, value in report.items()
                           if key not in {'process_tree', 'department_tree', 'starts', 'ends'}},
    }
