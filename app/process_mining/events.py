"""Read the supported CSV and XES event-log contracts."""
import csv
from dataclasses import dataclass
from datetime import UTC, datetime
from io import StringIO

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from app.enterprise.living_map import DEPARTMENTS
from app.schemas import api_error

MAX_BYTES = 5 * 1024 * 1024
MAX_EVENTS = 20_000
MAX_TRACE = 250
COLUMNS = ('case_id', 'activity', 'timestamp', 'resource', 'department')
DEPARTMENT_IDS = {label: key for key, (label, _) in DEPARTMENTS.items()}


@dataclass(frozen=True)
class Event:
    case_id: str
    activity: str
    timestamp: datetime
    resource: str
    department: str


def invalid(message):
    return api_error(422, 'invalid_event_log', message)


def csv_rows(payload):
    reader = csv.DictReader(StringIO(payload.decode('utf-8-sig'), newline=''), strict=True)
    fields = reader.fieldnames or []
    if len(fields) != len(set(fields)) or not set(COLUMNS).issubset(fields):
        raise invalid('ملف CSV محتاج الأعمدة case_id, activity, timestamp, resource, department من غير تكرار.')
    for row in reader:
        if None in row:
            raise invalid(f'عدد القيم في صف CSV رقم {reader.line_num} أكبر من عدد الأعمدة.')
        yield reader.line_num, [row[key] for key in COLUMNS]


def xes_rows(payload):
    root = ElementTree.fromstring(payload, forbid_dtd=True)
    if root.tag.rsplit('}', 1)[-1] != 'log':
        raise invalid('ملف XES محتاج عنصر log.')
    namespace = root.tag[:-3]

    def attributes(element):
        return {child.get('key'): child.get('value') for child in element if child.get('key')}

    index = 0
    for trace in root.findall(f'{namespace}trace'):
        case = attributes(trace)
        for entry in trace.findall(f'{namespace}event'):
            index += 1
            attrs = attributes(entry)
            yield index, [
                attrs.get('case_id') or attrs.get('case:concept:name') or case.get('case_id') or case.get('concept:name'),
                attrs.get('activity') or attrs.get('concept:name'),
                attrs.get('timestamp') or attrs.get('time:timestamp'),
                attrs.get('resource') or attrs.get('org:resource'),
                attrs.get('department') or attrs.get('org:group') or attrs.get('org:department'),
            ]


def parse(payload, filename):
    if len(payload) > MAX_BYTES:
        raise api_error(413, 'event_log_too_large', 'الحد الأقصى لحجم السجل 5 ميجابايت.')
    extension = filename.lower().rpartition('.')[2] if '.' in filename else ''
    if extension not in ('csv', 'xes'):
        raise api_error(415, 'unsupported_event_log', 'اختار سجل CSV أو XES.')
    rows = csv_rows(payload) if extension == 'csv' else xes_rows(payload)
    events = []
    try:
        for index, row in rows:
            if len(events) >= MAX_EVENTS:
                raise invalid('الحد الأقصى للسجل 20,000 حدث.')
            values = [(value or '').strip() for value in row]
            if any(not value or len(value) > 160 or any(ord(char) < 32 for char in value) for value in values):
                raise invalid(f'راجع القيم المطلوبة في صف أو حدث رقم {index}.')
            case, activity, raw_time, resource, department = values
            # Offset-free source timestamps share the same UTC clock.
            try:
                if len(raw_time) < 16:
                    raise ValueError('Missing time')
                stamp = datetime.fromisoformat(raw_time)
                stamp = stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)
            except (OverflowError, ValueError):
                raise invalid(f'التوقيت في صف أو حدث رقم {index} محتاج تاريخ ووقت بصيغة ISO 8601.') from None
            events.append(Event(case, activity, stamp, resource, DEPARTMENT_IDS.get(department, department)))
    except (UnicodeError, csv.Error, ElementTree.ParseError, DefusedXmlException):
        raise invalid('السجل غير قابل للقراءة. استخدم UTF-8 لملف CSV أو XES صالح.') from None
    if not events:
        raise invalid('السجل محتاج حدث واحد على الأقل.')
    return events, extension
