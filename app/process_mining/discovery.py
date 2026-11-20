"""Discover and summarize observed traces without retaining case or resource IDs."""
from collections import Counter, defaultdict
from math import log1p
from statistics import median

from app.enterprise.living_map import DEPARTMENTS
from app.process_mining.events import MAX_TRACE, invalid

DAY = 86400
WEEK = 7 * DAY


def department_label(key):
    return DEPARTMENTS.get(key, (key,))[0]


def process_tree(node):
    if node.operator is None:
        return {'activity': node.label}
    children = [process_tree(child) for child in node.children]
    # Commutative operators may be discovered in a different order across processes.
    operator = node.operator.name
    if operator in {'XOR', 'PARALLEL', 'OR', 'INTERLEAVING', 'PARTIALORDER'}:
        children.sort(key=str)
    return {'operator': operator, 'children': children}


def discover(events):
    import pm4py
    from pm4py.objects.log.obj import Event, EventLog, Trace

    by_case = defaultdict(list)
    resources = defaultdict(set)
    for event in events:
        by_case[event.case_id].append(event)
        resources[event.department].add(event.resource)
    if len(resources) > 40 or len({event.activity for event in events}) > 150:
        raise invalid('الحد الأقصى 40 قسم و150 نشاط مختلف في السجل.')
    traces = [sorted(by_case[key], key=lambda event: event.timestamp) for key in sorted(by_case)]
    if max(map(len, traces)) > MAX_TRACE:
        raise invalid(f'الحد الأقصى {MAX_TRACE} حدث للحالة الواحدة.')

    def log(field):
        return EventLog([Trace([Event({'concept:name': getattr(event, field), 'time:timestamp': event.timestamp})
                                for event in trace]) for trace in traces])

    department_log = log('department')
    dfg, starts, ends = pm4py.discover_dfg(department_log)
    department_tree = pm4py.discover_process_tree_inductive(department_log)
    tree = pm4py.discover_process_tree_inductive(log('activity'))
    gaps, repeats, service = defaultdict(list), Counter(), defaultdict(list)
    variants = Counter()
    for trace in traces:
        variants[tuple(event.department for event in trace)] += 1
        seen_departments = {trace[0].department}
        seen_steps = {(trace[0].department, trace[0].activity)}
        for left, right in zip(trace, trace[1:]):
            pair = left.department, right.department
            elapsed = (right.timestamp - left.timestamp).total_seconds()
            gaps[pair].append(elapsed)
            if elapsed > 0:
                service[left.department].append(elapsed)
            step = right.department, right.activity
            returned = left.department != right.department and right.department in seen_departments
            repeated_step = step in seen_steps
            if returned or repeated_step:
                repeats[pair] += 1
            seen_departments.add(right.department)
            seen_steps.add(step)

    max_volume = max((count for (left, right), count in dfg.items() if left != right), default=0)
    transitions = [
        {'from_department': left, 'to_department': right, 'volume': count,
         'median_duration_days': round(median(gaps[left, right]) / DAY, 6), 'rework_count': repeats[left, right],
         'weight': log1p(count) / log1p(max_volume) if left != right else 0}
        for (left, right), count in sorted(dfg.items())
    ]

    # A point event has no processing interval. Use the positive time until the
    # next recorded event as a service proxy and disclose the terminal fallback.
    pooled = [sample for samples in service.values() for sample in samples]
    fallback = median(pooled) if pooled else DAY
    first = min(trace[0].timestamp for trace in traces)
    last = max(event.timestamp for event in events)
    observed_weeks = (last - first).total_seconds() // WEEK + 1
    cases_per_week = len(traces) / observed_weeks
    departments = []
    for key, people in sorted(resources.items()):
        duration = median(service[key]) if service[key] else fallback
        visits = sum(route.count(key) * count for route, count in variants.items())
        load = visits / observed_weeks * duration / (WEEK * len(people))
        departments.append({
            'id': key, 'name_ar': department_label(key), 'capacity': len(people),
            'service_seconds': duration, 'duration_source': 'handoff' if service[key] else 'fallback',
            'visits': visits, 'estimated_load': round(load, 6),
        })
    return {
        'event_count': len(events), 'case_count': len(traces),
        'observed_weeks': int(observed_weeks), 'cases_per_week': cases_per_week,
        'departments': departments, 'transitions': transitions, 'starts': starts, 'ends': ends,
        'process_tree': process_tree(tree), 'department_tree': process_tree(department_tree),
        'bottlenecks': sorted(departments, key=lambda row: (-row['estimated_load'], row['id'])),
        'variants': [{'route': route, 'count': count} for route, count in sorted(variants.items())],
    }
