"""Version-level edge validation and server-owned approval stamps."""
from datetime import datetime

from app.access import active_principal, require_role
from app.enterprise.edge_contracts import (
    EdgeContractError, EdgeProvenance, contract_unit, conversion, needs_coefficient,
)
from app.enterprise.impact_graph import CausalGraph, ImpactError
from app.enterprise.whatif import compilation_inputs
from app.schemas import api_error

DEMO_STAMP = '2026-11-20T00:00:00+00:00'


def edge_data(edge):
    return edge.model_dump(exclude={'provenance', 'status'})


def materialize(graph, timestamp, *, previous=None, origin='legacy', review=None, reviewer=None):
    """Upgrade legacy snapshots on read and stamp only changed edges on write."""
    template, _, bound, drivers, methods = compilation_inputs(graph)
    nodes = {node.id: node for node in graph.nodes}
    old = {(edge.source, edge.target): edge for edge in previous.edges} if previous else {}
    old_units = {node.id: node.unit for node in previous.nodes} if previous else {}
    actor = active_principal.get()
    now = datetime.fromisoformat(DEMO_STAMP if origin == 'demo' else timestamp)
    edges = []
    for edge in graph.edges:
        pair = edge.source, edge.target
        method = methods.get(pair)
        legacy = edge.unit is None and edge.status is None
        missing = needs_coefficient(edge, nodes, method)
        expected = contract_unit(nodes[edge.source].unit, nodes[edge.target].unit, edge.rule, method)
        candidate = edge.model_copy(update={'unit': edge.unit or expected})
        prior = old.get(pair)
        if origin == 'legacy' and edge.provenance is not None:
            # Stored JSON already carries the authoritative stamp.
            edges.append(edge)
            continue
        if (prior and all(old_units[key] == nodes[key].unit for key in pair)
                and edge_data(candidate) == edge_data(prior) and (edge.status or prior.status) == prior.status):
            edges.append(prior)
            continue

        source = origin
        if origin == 'legacy':
            source = 'onboarding_template' if graph.onboarding else 'demo' if template else 'legacy'
            status = edge.status or ('draft' if missing else 'approved')
        elif origin == 'builder':
            status = edge.status or ('approved' if legacy and (method or not actor or actor.role == 'admin')
                                     and not missing else 'draft')
            if status == 'approved' and actor:
                require_role(actor)
        else:
            status = edge.status or ('draft' if missing else 'approved')
        if origin != 'legacy':
            if candidate.unit != expected or (missing and not legacy and prior is None):
                raise EdgeContractError(f'العلاقة {edge.source} → {edge.target} محتاجة وحدة {expected} ومعامل صالح.')
            if status == 'approved':
                conversion(candidate, nodes, method)
            if method and status != 'approved':
                raise EdgeContractError('العلاقات المدمجة معتمدة كجزء من القالب.')
        name = review['created_by'] if review else actor.username if actor and origin not in ('legacy', 'demo') else 'system'
        approver = reviewer or name
        if origin == 'edge_approval' and prior:
            provenance = prior.provenance.model_copy(update={'approved_by': reviewer, 'approved_at': now})
            edges.append(candidate.model_copy(update={'status': status, 'provenance': provenance}))
            continue
        provenance = EdgeProvenance(
            source=source, actor=name, recorded_at=datetime.fromisoformat(review['created_at']) if review else now,
            review_id=review['id'] if review else None, evidence=edge.evidence,
            approved_by=approver if status == 'approved' else None,
            approved_at=now if status == 'approved' else None,
        )
        edges.append(candidate.model_copy(update={'status': status, 'provenance': provenance}))
    updated = graph.model_copy(update={'edges': edges})
    if origin != 'legacy':
        CausalGraph(updated, built_in_edges=methods, bound=bound, drivers=drivers)
        # Drafts are inert. Validate their combined topology too so review cannot
        # turn a latent loop into an executable graph.
        proposed = [edge for edge in edges if edge.status == 'approved' or not needs_coefficient(
            edge, nodes, methods.get((edge.source, edge.target)))]
        CausalGraph(graph.model_copy(update={'edges': [
            edge.model_copy(update={'status': 'approved'}) for edge in proposed
        ]}), built_in_edges=methods, bound=bound, drivers=drivers)
    return updated


def prepare_write(graph, timestamp, **options):
    try:
        return materialize(graph, timestamp, **options)
    except (EdgeContractError, ImpactError) as error:
        raise api_error(422, 'invalid_edge_contract', str(error)) from None
