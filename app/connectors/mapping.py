from collections import defaultdict
from datetime import date, datetime
from typing import Annotated

from pydantic import Field, StringConstraints

from app.enterprise.schema import ModelUpdate, NodeId, Scalar, Series, StrictFields
from app.enterprise.store import update
from app.ingestion.validators import convert
from app.schemas import FieldError, ValidationResult, api_error

MAX_ROWS = 5000
Column = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Baseline = Annotated[Scalar | Series, Field(discriminator='kind')]


class ColumnMapping(StrictFields):
    node: Column
    value: Column
    date: Column | None = None

    def columns(self):
        return [column for column in (self.node, self.value, self.date) if column is not None]


class BaselineUpdate(StrictFields):
    node_id: NodeId
    baseline: Baseline


class ApplyRequest(StrictFields):
    expected_version: int = Field(ge=1, strict=True)
    updates: list[BaselineUpdate] = Field(min_length=1, max_length=MAX_ROWS)


def report(issues, rows=0, updates=0):
    return ValidationResult(ok=not issues, errors=issues, summary={'rows': rows, 'nodes': updates})


def observation_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip())


def preview(snapshot, sheet_name, sheet, mapping):
    nodes = {node.id: node for node in snapshot['graph'].nodes}
    issues = []
    updates = []
    count = len(sheet.rows)

    def issue(row, column, message):
        issues.append(FieldError(sheet=sheet_name, row=row, column=column, msg_ar=message))

    columns = mapping.columns()
    if len(columns) != len(set(columns)):
        issue(1, '', 'اختار عمود مختلف لكل حقل.')
    for column in columns:
        if sheet.headers.count(column) != 1:
            issue(1, column, 'العمود مش موجود أو عنوانه مكرر.')
    if not count:
        issue(0, '', 'المصدر مفيهوش صفوف بيانات.')
    if issues:
        return {'version': snapshot['version'], 'validation': report(issues, count), 'updates': []}

    indexes = {column: sheet.headers.index(column) for column in columns}
    values_by_node = defaultdict(dict)
    for row, values in sheet.rows:
        node_id = str(values[indexes[mapping.node]]).strip()
        node = nodes.get(node_id)
        if node is None:
            issue(row, mapping.node, 'معرّف العنصر مش موجود في النموذج.')
            continue
        raw_value = values[indexes[mapping.value]]
        try:
            value = convert(raw_value, 'float')
        except ValueError as error:
            issue(row, mapping.value, str(error))
            continue
        raw_date = values[indexes[mapping.date]] if mapping.date else None
        has_date = raw_date is not None and bool(str(raw_date).strip())
        when = None
        if isinstance(node.baseline, Series):
            try:
                when = observation_date(raw_date)
            except ValueError:
                issue(row, mapping.date or '', 'أدخل تاريخ صالح للسلسلة بصيغة YYYY-MM-DD.')
                continue
        elif has_date:
            issue(row, mapping.date, 'سيب التاريخ فاضي للعنصر اللي له قيمة واحدة.')
            continue
        if when in values_by_node[node_id]:
            issue(row, mapping.date if when else mapping.node, 'العنصر وتاريخه مكررين.' if when else 'العنصر مكرر.')
            continue
        values_by_node[node_id][when] = value

    for node_id, values in values_by_node.items():
        old = nodes[node_id].baseline
        new = (Series(frequency=old.frequency, points=[{'date': when, 'value': values[when]} for when in sorted(values)])
               if isinstance(old, Series) else Scalar(value=values[None]))
        if new != old:
            updates.append(BaselineUpdate(node_id=node_id, baseline=new))
    return {'version': snapshot['version'], 'validation': report(issues, count, len(updates)),
            'updates': updates if not issues else []}


def apply(connection, model_id, snapshot, body):
    if snapshot['version'] != body.expected_version:
        raise api_error(409, 'enterprise_version_conflict', 'النموذج اتعدّل. حمّل الإصدار الحالي وحاول تاني.')
    graph = snapshot['graph']
    nodes = {node.id: node for node in graph.nodes}
    updates = {item.node_id: item.baseline for item in body.updates}
    if len(updates) != len(body.updates) or not updates.keys() <= nodes.keys():
        raise api_error(422, 'invalid_import_nodes', 'راجع معرّفات العناصر وخلي كل عنصر يظهر مرة واحدة.')
    row_count = 0
    for node_id, new in updates.items():
        old = nodes[node_id].baseline
        row_count += len(new.points) if isinstance(new, Series) else 1
        if (old.kind != new.kind or isinstance(old, Series) and old.frequency != new.frequency
                or isinstance(new, Scalar) and new.value is None):
            raise api_error(422, 'invalid_import_baseline', 'القيمة لازم تطابق نوع العنصر وتكراره الزمني.')
    if row_count > MAX_ROWS:
        raise api_error(422, 'too_many_rows', 'عدد الصفوف أكبر من الحد المسموح.')
    changed = {node_id: baseline for node_id, baseline in updates.items() if baseline != nodes[node_id].baseline}
    if not changed:
        return snapshot
    new_graph = graph.model_copy(update={'nodes': [
        node.model_copy(update={'baseline': changed[node.id]}) if node.id in changed else node
        for node in graph.nodes
    ]})
    return update(connection, model_id, ModelUpdate(name=snapshot['name'], graph=new_graph,
                                                   expected_version=body.expected_version))
