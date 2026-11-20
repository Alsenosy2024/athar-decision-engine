"""Deterministic, editable column discovery and source-grain merging."""
from collections import defaultdict
from hashlib import sha256
from math import fsum
from pathlib import PurePosixPath
import re
import unicodedata

from pydantic import TypeAdapter, ValidationError

from app.company_data.schema import DIMENSIONS, Entity, Mapping, Measure, Observation, Source
from app.connectors.files import read_tables
from app.connectors.mapping import observation_date
from app.db import encode
from app.ingestion.validators import convert
from app.schemas import FieldError, api_error

DIMENSION_HEADERS = {
    'branch': ('branch', 'branch_id', 'branch_code', 'store', 'store_id', 'store_code', 'الفرع', 'كود الفرع'),
    'department': ('department', 'department_id', 'dept', 'dept_code', 'القسم', 'كود القسم'),
    'account': ('account', 'account_id', 'account_code', 'gl_account', 'الحساب', 'كود الحساب'),
    'cost_center': ('cost_center', 'cost_center_id', 'cost_center_code', 'مركز التكلفة'),
    'product': ('product', 'product_id', 'product_code', 'sku', 'sku_id', 'الصنف', 'المنتج'),
    'supplier': ('supplier', 'supplier_code', 'supplier_id', 'المورد'),
}
DIMENSION_NAMES = {
    'branch': 'الفروع', 'department': 'الأقسام', 'account': 'الحسابات',
    'cost_center': 'مراكز التكلفة', 'product': 'المنتجات', 'supplier': 'الموردون',
}
MEASURES = {
    'sales': ('صافي المبيعات', 'SAR', 'sum', ('net_sales_sar', 'sales_sar', 'net_sales', 'revenue_sar', 'صافي المبيعات')),
    'units_sold': ('الوحدات المباعة', 'units', 'sum', ('units_sold', 'qty_sold', 'sold_units', 'الوحدات المباعة')),
    'stock': ('رصيد المخزون', 'units', 'closing', ('stock_units', 'inventory_units', 'رصيد المخزون')),
    'cost': ('التكلفة', 'SAR', 'sum', ('cost_sar', 'cogs_sar', 'التكلفة')),
    'amount': ('الحركة المحاسبية', 'SAR', 'sum', ('amount_sar', 'gl_amount_sar', 'المبلغ بالريال')),
    'cash': ('رصيد النقدية', 'SAR', 'closing', ('cash_balance_sar', 'closing_cash_sar', 'رصيد النقدية')),
    'expenses': ('مصروفات التشغيل', 'SAR', 'sum', ('operating_expenses_sar', 'opex_sar', 'مصروفات التشغيل')),
    'financing': ('مصروفات التمويل', 'SAR', 'sum', ('financing_expenses_sar', 'مصروفات التمويل')),
    'receivables': ('الذمم المدينة', 'SAR', 'closing', ('receivables_sar', 'الذمم المدينة')),
    'payables': ('الذمم الدائنة', 'SAR', 'closing', ('payables_sar', 'الذمم الدائنة')),
}
DAY_HEADERS = ('txn_day', 'transaction_date', 'date', 'day', 'posting_date', 'التاريخ', 'تاريخ الحركة')
MAX_OBSERVATIONS = 20_000
mapping_adapter = TypeAdapter(dict[str, Mapping])


def normalized(value):
    return re.sub(r'[\s-]+', '_', unicodedata.normalize('NFKC', value).casefold().strip())


def schema_key(headers):
    return sha256(encode(sorted(header for header in headers if header)).encode()).hexdigest()


def suggest(headers):
    by_key = defaultdict(list)
    for header in headers:
        by_key[normalized(header)].append(header)

    def find(aliases):
        matched = [header for alias in dict.fromkeys(map(normalized, aliases)) for header in by_key[alias]]
        return matched[0] if len(matched) == 1 else None

    def dimension_fields(aliases):
        return {kind: column for kind in DIMENSIONS if (column := find(aliases(kind)))}

    return Mapping(
        day=find(DAY_HEADERS), dimensions=dimension_fields(DIMENSION_HEADERS.get),
        parents=dimension_fields(lambda kind: (f'{kind}_parent', f'parent_{kind}', f'parent_{kind}_id')),
        names=dimension_fields(lambda kind: (f'{kind}_name',)),
        measures=[Measure(column=column, key=key, name=name, unit=unit, aggregation=aggregation)
                  for key, (name, unit, aggregation, aliases) in MEASURES.items() if (column := find(aliases))],
    )


def mapping_columns(mapping):
    return [*([mapping.day] if mapping.day else []), *mapping.dimensions.values(),
            *mapping.parents.values(), *mapping.names.values(), *(item.column for item in mapping.measures)]


def code(value):
    if value is None or value == '':
        return None
    text = unicodedata.normalize('NFC', convert(value, 'str')).strip()
    if len(text) > 160:
        raise ValueError('المعرّف أو الاسم أطول من 160 حرف.')
    return text or None


def entity_id(kind, key):
    return kind + '.' + sha256(key.encode()).hexdigest()[:24]


def check_hierarchy(entities):
    verified = set()
    for identity in entities:
        path = set()
        while identity and identity not in verified:
            if identity in path:
                raise ValueError('فيه دورة في الهيكل التنظيمي.')
            path.add(identity)
            identity = entities[identity].parent_id
        verified.update(path)


def fact_key(row):
    return row.day, row.entities, row.measure


def measure_contract(item):
    return item.name, item.unit, item.aggregation


def prepare(current, contents, overrides, *, selected_sheet=None):
    issues, discovered, entities, staged = [], [], current.entities.copy(), {}
    pending_parents, pending_names = {}, {}
    mappings = current.mappings.copy()
    contracts = {item.key: measure_contract(item) for mapping in mappings.values() for item in mapping.measures}
    input_keys, batch_mappings = set(), {}
    rows_seen = 0

    def report(sheet, row, column, message):
        if len(issues) < 80:
            issues.append(FieldError(sheet=sheet, row=row, column=column, msg_ar=message))

    if selected_sheet is not None and len(contents) != 1:
        raise api_error(422, 'company_select_one_workbook', 'اختار ملف واحد عشان تحدد ورقة بعينها.')
    for index, (filename, raw) in enumerate(contents):
        filename = PurePosixPath(filename.replace(chr(92), '/')).name[:160]
        sheets = read_tables(filename, raw, selected=selected_sheet, load_rows=True)
        if selected_sheet is not None and selected_sheet not in sheets:
            raise api_error(422, 'unknown_sheet', 'الورقة المختارة مش موجودة في الملف.')
        digest = sha256(raw).hexdigest()
        for sheet_name, table in sheets.items():
            source_key = f'{index}:{sheet_name}'
            input_keys.add(source_key)
            location = f'{filename} / {sheet_name}'
            signature = schema_key(table.headers)
            binding = overrides.get(source_key) or mappings.get(signature) or suggest(table.headers)
            used = mapping_columns(binding)
            discovered.append({'id': source_key, 'file': filename, 'sheet': sheet_name,
                               'columns': [header for header in table.headers if header],
                               'mapping': binding, 'reused': signature in current.mappings and source_key not in overrides,
                               'rows': len(table.rows), 'unmapped_columns': [h for h in table.headers if h and h not in used]})
            if not table.rows or not (binding.dimensions or binding.measures):
                report(location, 1, '', 'اربط على الأقل كيان أو مؤشر في الورقة، واختار ورقة فيها بيانات.')
                continue
            if (len(used) != len(set(used)) or any(table.headers.count(col) != 1 for col in used)
                    or not (binding.parents.keys() | binding.names.keys()) <= binding.dimensions.keys()
                    or len({item.key for item in binding.measures}) != len(binding.measures)
                    or binding.measures and binding.day is None):
                report(location, 1, '', 'راجع تفرّد الأعمدة، وربط التاريخ للمؤشرات ومعرّفات الكيانات للهيكل.')
                continue
            if any(item.key in contracts and contracts[item.key] != measure_contract(item) for item in binding.measures):
                report(location, 1, '', 'المؤشر موجود بوحدة أو تجميع أو اسم مختلف في ربط معتمد.')
                continue
            if batch_mappings.setdefault(signature, binding) != binding:
                report(location, 1, '', 'استخدم نفس الربط للأوراق اللي لها نفس عناوين الأعمدة.')
                continue
            mappings[signature] = binding
            contracts.update({item.key: measure_contract(item) for item in binding.measures})
            rows_seen += len(table.rows)
            if rows_seen > MAX_OBSERVATIONS:
                raise api_error(422, 'company_import_too_large', 'عدد الصفوف أكبر من حد الإدخال للشركة.')
            values = defaultdict(list)
            for row_no, row in table.rows:
                data = dict(zip(table.headers, row))
                grain = []
                try:
                    for kind, column in binding.dimensions.items():
                        key = code(data[column])
                        parent = code(data[binding.parents[kind]]) if kind in binding.parents else None
                        label = code(data[binding.names[kind]]) if kind in binding.names else None
                        if not key:
                            if parent or label:
                                raise ValueError('فيه اسم أو مرجع أب بدون معرّف كيان.')
                            continue
                        identity = entity_id(kind, key)
                        grain.append(identity)
                        entities.setdefault(identity, Entity(id=identity, kind=kind, code=key, name=key))
                        if parent:
                            parent_id = entity_id(kind, parent)
                            entities.setdefault(parent_id, Entity(id=parent_id, kind=kind, code=parent, name=parent))
                            if pending_parents.setdefault(identity, parent_id) != parent_id:
                                raise ValueError('نفس الكيان مرتبط بأكتر من أب في المعاينة.')
                        if label and pending_names.setdefault(identity, label) != label:
                            raise ValueError('نفس المعرّف مرتبط بأكتر من اسم في المعاينة.')
                    row_measures = [item for item in binding.measures if data[item.column] not in (None, '')]
                    if not row_measures and not grain:
                        raise ValueError('الصف مفيهوش قيمة أو معرّف من الأعمدة المربوطة.')
                    if not row_measures:
                        continue
                    try:
                        day = observation_date(data[binding.day])
                    except (ValueError, TypeError):
                        raise ValueError('أدخل تاريخ حركة صالح بصيغة YYYY-MM-DD.') from None
                    for item in row_measures:
                        amount = convert(data[item.column], 'float')
                        if abs(amount) > 1e12:
                            raise ValueError('القيمة أكبر من حد الإدخال.')
                        values[day, tuple(sorted(grain)), item.key].append((row_no, amount))
                except ValueError as error:
                    report(location, row_no, '', str(error))

            by_key = {item.key: item for item in binding.measures}
            for key, readings in values.items():
                day, grain, metric = key
                item = by_key[metric]
                amounts = [amount for _, amount in readings]
                if key in staged or item.aggregation == 'closing' and len(set(amounts)) > 1:
                    report(location, readings[0][0], item.column, 'فيه قراءات متعارضة أو مكررة لنفس المؤشر والتاريخ والكيانات.')
                    continue
                staged[key] = Observation(day=day, entities=grain, measure=metric,
                                          value=fsum(amounts) if item.aggregation == 'sum' else amounts[0],
                                          source=Source(file=filename, sha256=digest, sheet=sheet_name,
                                                        column=item.column, rows=[row for row, _ in readings]))

    if not contents or not input_keys or overrides.keys() - input_keys:
        report('', 0, '', 'اختار ملفات CSV أو XLSX وراجع ربط الأوراق المختارة.')
    for identity in pending_parents.keys() | pending_names.keys():
        entity = entities[identity]
        entities[identity] = entity.model_copy(update={
            'parent_id': pending_parents.get(identity, entity.parent_id),
            'name': pending_names.get(identity, entity.name),
        })
    try:
        check_hierarchy(entities)
    except ValueError as error:
        report('', 0, '', str(error))
    if issues:
        return None, discovered, issues, []

    stored = {fact_key(item): item for item in current.observations}
    changed = [item for key, item in staged.items() if stored.get(key) != item]
    stored.update(staged)
    if len(stored) > MAX_OBSERVATIONS or len(entities) > 2_000 or len(mappings) > 100:
        raise api_error(422, 'company_import_too_large', 'نموذج الشركة وصل لحد حجم الإدخال.')
    updated = current.model_copy(update={'entities': entities, 'mappings': mappings,
                                         'observations': [stored[key] for key in sorted(stored)]})
    return updated, discovered, issues, changed


def parse_mappings(raw):
    try:
        return mapping_adapter.validate_json(raw)
    except ValidationError:
        raise api_error(422, 'invalid_company_mapping', 'راجع صيغة ربط أعمدة الشركة.') from None
