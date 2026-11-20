"""Use the document extractor with the assigned section's onboarding contract."""
from decimal import InvalidOperation

from app.intelligence import extraction
from app.onboarding import ingest
from app.onboarding.presets import INPUTS, ITEMS


def extract(rows, model, section, settings):
    definitions = extraction.catalog(model['graph'])
    input_ids = {key for key, spec in INPUTS.items() if spec.section == section}
    scoped = {key: definitions[key] for key in sorted(input_ids)}
    # Map source terminology to the editable input, not a calculated output.
    for key, alias_key in {'cash.opening': 'cash', 'supplier.delay': 'supplier.lead_time'}.items():
        if key in scoped:
            kind, unit, label, aliases = scoped[key]
            scoped[key] = kind, unit, label, (*aliases, *definitions[alias_key][3])
    local, unresolved, issues = {}, [], []
    for index, item in enumerate(extraction.candidates(rows)):
        if not item.literals:
            continue
        known = extraction.alias_match(item.text, scoped)
        outside = extraction.alias_match(item.text, definitions)
        if known and len(item.literals) == 1:
            key, confidence = known
            local[index] = (item, key, item.literals[0], confidence, 'rules')
        elif known or not outside:
            unresolved.append((index, item))

    mapped = extraction.provider_mappings(settings, unresolved, scoped) if scoped else {}
    for index, item in unresolved:
        mapping = mapped.get(index)
        if mapping and mapping.node_id in input_ids:
            local[index] = (item, mapping.node_id, mapping.literal, mapping.confidence, 'codex')
        else:
            issues.append({'source': item.source, 'message_ar': 'قيمة محتاجة تحديد الحقل يدويًا.'})

    found = []
    for item, key, literal, confidence, provider in local.values():
        unit = scoped[key][1]
        try:
            value = extraction.normalized_value(literal, item.context, unit, {})
        except (ValueError, InvalidOperation) as error:
            issues.append({'source': item.source, 'message_ar': str(error)})
            continue
        found.append(({'key': key, 'value': value, 'date': item.date},
                      {'source': item.source, 'confidence': min(confidence, item.confidence), 'provider': provider}))

    if section in ITEMS:
        prefix, _ = ITEMS[section]
        for row in rows:
            key, *rest = row.cells
            if not rest:
                key, *rest = extraction.TYPED_VALUE.split(key, maxsplit=1)
            if rest and rest[0] and key.startswith(prefix + '.') and ingest.section_for(key) == section:
                found.append(({'key': key, 'value': rest[0]},
                              {'source': row.source, 'confidence': row.confidence, 'provider': 'rules'}))

    unique = {}
    for raw, meta in found:
        unique.setdefault((raw['key'], raw.get('date'), raw['value']), (raw, meta))
    entries = list(unique.values())
    records, errors = ingest.validate(
        ((section, i, raw) for i, (raw, _) in enumerate(entries, 1)), section,
    )
    by_key = {(raw['key'], raw.get('date')): meta for raw, meta in entries}
    evidence = {(row.key, row.date): by_key[(row.key, row.date.isoformat() if row.date else None)]
                for row in records}
    return records, errors, evidence, issues
