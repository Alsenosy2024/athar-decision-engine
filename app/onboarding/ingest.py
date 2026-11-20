"""Shared section validation for manual, Excel and saved ERP inputs."""
from io import BytesIO

from openpyxl import Workbook
from openpyxl.comments import Comment
from pydantic import ValidationError

from app.connectors.mapping import observation_date
from app.ingestion.validators import convert
from app.onboarding.presets import INPUTS, ITEMS, SECTIONS, section_specs
from app.onboarding.projection import record_key
from app.onboarding.records import Record
from app.schemas import FieldError

COLUMNS = ('key', 'value', 'date', 'source', 'label')
MAX_ROWS = 5000


def blank(value):
    return value is None or isinstance(value, str) and not value.strip()


def section_for(key):
    if key in INPUTS:
        return INPUTS[key].section
    return next((section for section, (prefix, _) in ITEMS.items()
                 if key.startswith(prefix + '.') and len(key) > len(prefix) + 1), None)


def validate(rows, section=None, *, allow_template_blanks=False, filter_sections=False):
    issues, records, seen = [], [], set()
    for sheet, row, raw in rows:
        key = str(raw.get('key') or '').strip()
        section_id = section_for(key)
        if filter_sections and section_id and section_id != section:
            continue
        raw_value, raw_date, raw_source = (raw.get(column) for column in COLUMNS[1:4])
        if allow_template_blanks and all(map(blank, (raw_value, raw_date, raw_source))):
            continue
        if section_id is None or (section or sheet) != section_id:
            issues.append(FieldError(sheet=sheet, row=row, column='key',
                                     msg_ar='معرّف الحقل مش موجود في القسم ده.'))
            continue
        try:
            if key in INPUTS:
                value = convert(raw_value, 'float')
                low = -1e12 if key == 'cash.opening' else 0
                high = 100 if key == 'risk.exposure' else 1e12
                if not low <= value <= high:
                    raise ValueError('القيمة خارج النطاق المسموح للحقل.')
            elif isinstance(raw_value, str) and raw_value.strip():
                value = raw_value.strip()
            else:
                raise ValueError('اكتب اسم القسم أو المنتج أو الخدمة.')
        except ValueError as error:
            issues.append(FieldError(sheet=sheet, row=row, column='value', msg_ar=str(error)))
            continue
        try:
            date = observation_date(raw_date) if not blank(raw_date) else None
            if date and key not in INPUTS:
                raise ValueError
        except (ValueError, OverflowError):
            issues.append(FieldError(sheet=sheet, row=row, column='date',
                                     msg_ar='التاريخ للحقول الرقمية فقط. استخدم YYYY-MM-DD أو سيبه فاضي.'))
            continue
        source = 'actual' if blank(raw_source) else str(raw_source).strip()
        if source not in ('actual', 'estimated'):
            issues.append(FieldError(sheet=sheet, row=row, column='source',
                                     msg_ar='المصدر لازم يكون actual أو estimated.'))
            continue
        try:
            record = Record(key=key, date=date, source=source, value=value)
        except ValidationError as error:
            column = error.errors()[0]['loc'][0]
            issues.append(FieldError(sheet=sheet, row=row, column=column,
                                     msg_ar='راجع صيغة المعرّف وطول الاسم.'))
            continue
        identity = record_key(record)
        if identity in seen:
            issues.append(FieldError(sheet=sheet, row=row, column='key',
                                     msg_ar='الحقل مكرر لنفس التاريخ في المعاينة.'))
            continue
        seen.add(identity)
        records.append(record)
    if not records and not issues:
        issues.append(FieldError(sheet=section or 'Excel', row=0, column='value',
                                 msg_ar='مفيش قيم جديدة للمراجعة في المصدر.'))
    return records, issues


def table_rows(tables, section=None):
    rows, errors = [], []
    for sheet_id, sheet in tables.items():
        if sheet_id not in SECTIONS or (section and section != sheet_id):
            errors.append(FieldError(sheet=sheet_id, row=1, column='', msg_ar='اسم الورقة خارج قالب التجهيز.'))
            continue
        if (len(sheet.headers) != len(set(sheet.headers)) or not set(sheet.headers) <= set(COLUMNS)
                or not {'key', 'value'} <= set(sheet.headers)):
            errors.append(FieldError(sheet=sheet_id, row=1, column='', msg_ar='راجع عناوين الأعمدة حسب القالب.'))
            continue
        rows.extend((sheet_id, number, dict(zip(sheet.headers, values))) for number, values in sheet.rows)
    return rows, errors


def workbook(sector, section=None):
    book = Workbook()
    book.remove(book.active)
    book.properties.title = 'أثر | Athar'
    book.properties.subject = 'كل قرار.. وأثره على شركتك كلها'
    for spec in section_specs(sector):
        key = spec['id']
        if section and key != section:
            continue
        sheet = book.create_sheet(key)
        sheet.sheet_view.rightToLeft = True
        sheet.freeze_panes = 'A2'
        sheet.append(COLUMNS)
        help_text = (
            'معرّف الحقل. سيب المعرّفات زي ما هي. للقسم أو المنتج ضيف سطر بمعرّف مميز.',
            'اكتب قيمة الحقل. الخانات الفاضية بتسيب البيانات المحفوظة زي ما هي.',
            'اختياري للحقول الرقمية. تاريخ القراءة بصيغة YYYY-MM-DD.',
            'actual للقيمة الفعلية (الافتراضي) أو estimated للمُقدرة.',
            spec['name'],
        )
        for cell, note, width in zip(sheet[1], help_text, (28, 26, 16, 16, 62)):
            cell.comment = Comment(note, 'أثر')
            sheet.column_dimensions[cell.column_letter].width = width
        for item in spec['fields']:
            sheet.append([item['key'], None, None, None, f"{item['label']} · {item['unit']}"])
        if spec['item']:
            prefix, label = spec['item']
            sheet.append([prefix + '.example', None, None, None, label + ' · غيّر example لمعرّف فريد'])
    buffer = BytesIO()
    book.save(buffer)
    return buffer.getvalue()
