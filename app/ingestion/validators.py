from decimal import Decimal, InvalidOperation
from math import isfinite

from app.ingestion.loader import demo_sheets
from app.schemas import FieldError, ValidationResult

POSITIVE_FIELDS = {'duration_weeks', 'planned_weeks', 'actual_weeks', 'orders.qty', 'units', 'volume'}
NUMERIC_DIGITS = str.maketrans('٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫', '01234567890123456789.')


def convert(value, kind):
    if isinstance(value, str):
        value = value.strip()
    if kind == 'str':
        if isinstance(value, (str, int, float)) and type(value) is not bool:
            return str(value)
        raise ValueError('أدخل نصًا أو معرّفًا صالحًا.')
    if isinstance(value, str):
        value = value.translate(NUMERIC_DIGITS)
    if kind == 'bool':
        if str(value).lower() in ('true', '1', 'نعم'):
            return True
        if str(value).lower() in ('false', '0', 'لا'):
            return False
        raise ValueError('أدخل نعم أو لا، أو استخدم 1 أو 0.')
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            raise InvalidOperation
        if kind == 'int':
            if number != number.to_integral_value() or abs(number) > 2**63 - 1:
                raise InvalidOperation
            return int(number)
        result = float(number)
        if isfinite(result):
            return result
    except (InvalidOperation, OverflowError):
        pass
    raise ValueError('أدخل عددًا صحيحًا.' if kind == 'int' else 'أدخل رقمًا صالحًا.')


def validate_dataset(template, sheets, parameters=None):
    if parameters is None:
        parameters = template.model_defaults()['parameters']
    horizon = parameters[template.horizon_key]
    errors = []
    records = {name: [] for name in template.entities}

    def report(sheet, row, column, message):
        errors.append(FieldError(sheet=sheet, row=row, column=column, msg_ar=message))

    for name, entity in template.entities.items():
        if name not in sheets:
            report(name, 0, '', 'الجدول المطلوب غير موجود.')
            continue
        sheet = sheets[name]
        headers = [header.strip() if isinstance(header, str) else header for header in sheet.headers]
        positions = {}
        for spec in entity.fields:
            if headers.count(spec.name) > 1:
                report(name, 1, spec.name, 'عنوان العمود مكرر.')
            elif spec.name in headers:
                positions[spec.name] = headers.index(spec.name)
            elif spec.required:
                report(name, 1, spec.name, f'العمود المطلوب «{spec.name_ar}» غير موجود.')
        seen = set()
        for row_number, values in sheet.rows:
            row = {}
            for spec in entity.fields:
                column = spec.name
                if column not in positions:
                    continue
                pos = positions[column]
                value = values[pos] if pos < len(values) else None
                if value is None or isinstance(value, str) and not value.strip():
                    if spec.required:
                        report(name, row_number, column, 'القيمة مطلوبة.')
                    continue
                try:
                    value = convert(value, spec.type)
                    if spec.type in ('int', 'float'):
                        if value < 0 or value == 0 and (column in POSITIVE_FIELDS or f'{name}.{column}' in POSITIVE_FIELDS):
                            raise ValueError('أدخل قيمة موجبة.' if column in POSITIVE_FIELDS or f'{name}.{column}' in POSITIVE_FIELDS else 'أدخل قيمة صفر أو أكبر.')
                        if column == 'due_day' and value >= horizon:
                            raise ValueError(f'يوم التسليم لازم يكون بين 0 و{horizon - 1}.')
                    row[column] = value
                except ValueError as error:
                    report(name, row_number, column, str(error))
            keys = entity.key_fields
            if keys and all(key in row for key in keys):
                identity = tuple(row[key] for key in keys)
                if identity in seen:
                    report(name, row_number, ', '.join(keys), 'المعرّف مكرر.')
                seen.add(identity)
            if {'nta_week', 'deadline_week'} <= row.keys() and row['deadline_week'] < row['nta_week']:
                report(name, row_number, 'deadline_week', 'أسبوع التسليم لازم يساوي أو يلي أول أسبوع للبدء.')
            records[name].append((row_number, row))

    for relation in template.relations:
        source_table, source_column = relation.source.split('.')
        target_table, target_column = relation.target.split('.')
        known = {row[target_column] for _, row in records[target_table] if target_column in row}
        for row_number, row in records[source_table]:
            if source_column in row and row[source_column] not in known:
                report(source_table, row_number, source_column, f'المعرّف غير موجود في جدول «{template.entities[target_table].name_ar}».')

    payload = {name: [row for _, row in rows] for name, rows in records.items()}
    result = ValidationResult(ok=not errors, errors=errors, summary={name: len(rows) for name, rows in payload.items()})
    return payload, result


def validate_demo(template, parameters=None):
    return validate_dataset(template, demo_sheets(template), parameters)
