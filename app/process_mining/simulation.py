"""Replay sampled case routes through SimPy FIFO departments in simulated seconds."""
from collections import Counter, defaultdict
from statistics import median
from typing import Annotated

import numpy as np
import simpy
from pydantic import Field, FiniteFloat

from app.enterprise.schema import StrictFields
from app.process_mining.discovery import DAY, WEEK
from app.process_mining.shifts import ShiftSchedule
from app.schemas import api_error

DepartmentID = Annotated[str, Field(min_length=1, max_length=160)]
Capacity = Annotated[int, Field(ge=1, le=20_000, strict=True)]
ServiceChange = Annotated[FiniteFloat, Field(ge=-90, le=900)]
MAX_VISITS = 500_000


class Scenario(StrictFields):
    demand_pct: FiniteFloat = Field(default=0, ge=-100, le=900)
    capacities: dict[DepartmentID, Capacity] = Field(default_factory=dict, max_length=40)
    service_pct: dict[DepartmentID, ServiceChange] = Field(default_factory=dict, max_length=40)
    shifts: dict[DepartmentID, ShiftSchedule] = Field(default_factory=dict, max_length=40)


class SimulationRequest(StrictFields):
    company_id: int = Field(ge=1, lt=2**63, strict=True)
    weeks: int = Field(default=13, ge=1, le=104, strict=True)
    seed: int = Field(default=42, ge=0, le=2**32 - 1, strict=True)
    revision: str | None = Field(default=None, pattern=r'^[0-9a-f]{32}$')
    scenario: Scenario = Field(default_factory=Scenario)
    replications: int = Field(default=1, ge=1, le=10, strict=True)


def arrivals(profile, weeks, scenario, seed, visit_budget=MAX_VISITS):
    rng = np.random.default_rng(seed)
    count = profile['cases_per_week'] * (1 + scenario.demand_pct / 100)
    variants = profile['variants']
    weights = np.array([row['count'] for row in variants])
    counts = rng.poisson(count, weeks)
    if counts.sum() > visit_budget:
        raise api_error(422, 'simulation_budget', 'الحمل كبير على المحاكاة. قلّل عدد الأسابيع أو نسبة الطلب.')
    visits = 0
    schedule = []
    for week, amount in enumerate(counts):
        times = np.sort(rng.uniform(week * WEEK, (week + 1) * WEEK, amount)).tolist()
        routes = rng.choice(len(variants), amount, p=weights / weights.sum())
        for start, variant in zip(times, routes):
            route = variants[variant]['route']
            visits += len(route)
            if visits > visit_budget:
                raise api_error(422, 'simulation_budget', 'الحمل كبير على المحاكاة. قلّل عدد الأسابيع أو نسبة الطلب.')
            schedule.append((start, route))
    return schedule


def simulate(profile, weeks, scenario, seed, *, visit_budget=MAX_VISITS):
    departments = {row['id']: row for row in profile['departments']}
    if unknown := (scenario.capacities.keys() | scenario.service_pct.keys() | scenario.shifts.keys()) - departments.keys():
        raise api_error(422, 'unknown_simulation_department', f'القسم {min(unknown)} مش موجود في السجل.')
    schedule = arrivals(profile, weeks, scenario, seed, visit_budget)
    env = simpy.Environment()
    resources = {key: simpy.Resource(env, scenario.capacities.get(key, row['capacity']))
                 for key, row in departments.items()}
    open_seconds = {key: scenario.shifts[key].seconds_open if key in scenario.shifts else WEEK
                    for key in resources}
    durations = {key: row['service_seconds'] * (1 + scenario.service_pct.get(key, 0) / 100)
                 for key, row in departments.items()}

    arrived = [0] * weeks
    lead = [[] for _ in range(weeks)]
    flows = [Counter() for _ in range(weeks)]
    busy = [defaultdict(float) for _ in range(weeks)]
    finished = [Counter() for _ in range(weeks)]
    horizon = weeks * WEEK

    def serve(route):
        entered = env.now
        arrived[int(entered // WEEK)] += 1
        for index, key in enumerate(route):
            with resources[key].request() as request:
                yield request
                remaining = durations[key]
                calendar = scenario.shifts.get(key)
                while remaining > 0:
                    start, stop = calendar.next_window(env.now) if calendar else (env.now, float('inf'))
                    if start > env.now:
                        yield env.timeout(start - env.now)
                    duration = min(remaining, stop - start)
                    end = min(horizon, start + duration)
                    for week in range(int(start // WEEK), min(weeks, int(end // WEEK) + 1)):
                        busy[week][key] += max(0, min(end, (week + 1) * WEEK) - max(start, week * WEEK))
                    yield env.timeout(duration)
                    remaining -= duration
            week = int(env.now // WEEK)
            finished[week][key] += 1
            if index + 1 < len(route):
                flows[week][key, route[index + 1]] += 1
        lead[int(env.now // WEEK)].append((env.now - entered) / DAY)

    def admit():
        for start, route in schedule:
            yield env.timeout(start - env.now)
            env.process(serve(route))

    env.process(admit())
    frames = []
    for week in range(weeks):
        env.run(until=(week + 1) * WEEK)
        breakdown = [
            {'id': key, 'waiting': len(resource.queue), 'in_service': resource.count,
             'completed_visits': finished[week][key], 'capacity': resource.capacity,
             'utilization_pct': round(100 * busy[week][key] / (open_seconds[key] * resource.capacity), 4)}
            for key, resource in resources.items()
        ]
        waiting = sum(row['waiting'] for row in breakdown)
        in_service = sum(row['in_service'] for row in breakdown)
        frames.append({
            'week': week + 1, 'arrivals': arrived[week], 'completed': len(lead[week]),
            'backlog': waiting + in_service, 'waiting': waiting,
            'lead_time_days': round(median(lead[week]), 4) if lead[week] else None,
            'utilization_pct': round(100 * sum(busy[week].values()) /
                                     (sum(open_seconds[key] * resource.capacity for key, resource in resources.items())), 4),
            'departments': breakdown,
            'flows': [{'from_department': left, 'to_department': right, 'volume': count}
                      for (left, right), count in sorted(flows[week].items())],
        })
    return frames


def repeat(profile, request):
    from app.forecasting.methods import quantiles

    seeds = [(request.seed + index) % 2**32 for index in range(request.replications)]
    trials = [simulate(profile, request.weeks, request.scenario, seed,
                       visit_budget=MAX_VISITS // request.replications) for seed in seeds]
    first = trials[0]
    if len(seeds) == 1:
        return first, {}
    metrics = ('completed', 'backlog', 'waiting', 'lead_time_days', 'utilization_pct')
    bands = []
    for week, frames in enumerate(zip(*trials), 1):
        summaries = {}
        for key in metrics:
            values = [frame[key] for frame in frames if frame[key] is not None]
            summaries[key] = {'n': len(values), **(quantiles(values) if values else {})}
        bands.append({'week': week, 'metrics': summaries})
    return first, {'replication_seeds': seeds, 'replication_bands': bands}
