import csv
import json
from dataclasses import dataclass, field
from io import BytesIO, StringIO
from pathlib import Path
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from app.config import DATA_DIR
from app.schemas import FieldError


@dataclass
class Sheet:
    headers: list
    rows: list[tuple[int, list]] = field(default_factory=list)


def load_demo(template_id):
    return json.loads((DATA_DIR / 'demo' / f'{template_id}.json').read_text(encoding='utf-8'))


def demo_sheets(template):
    payload = load_demo(template.template_id)
    return {
        name: Sheet(columns, [
            (row_no, [row.get(column) for column in columns])
            for row_no, row in enumerate(payload[name], 2)
        ])
        for name, columns in template.upload_sheets.items()
    }


def load_upload(filename, contents, template):
    suffix = Path(filename).suffix.lower()
    try:
        if suffix == '.xlsx':
            book = load_workbook(BytesIO(contents), read_only=True, data_only=True)
            try:
                sheets = {}
                for name in template.upload_sheets:
                    if name not in book:
                        continue
                    rows = book[name].iter_rows(values_only=True)
                    sheets[name] = Sheet(list(next(rows, ())), [
                        (row_no, list(values)) for row_no, values in enumerate(rows, 2)
                        if any(value is not None for value in values)
                    ])
                return sheets, []
            finally:
                book.close()
        if suffix == '.csv':
            return load_csv(contents, template)
    except (BadZipFile, InvalidFileException, ParseError, ValueError, KeyError, OSError, csv.Error):
        return {}, [FieldError(sheet='الملف', row=0, column='', msg_ar='تعذّر قراءة الملف. تأكد من الصيغة والترميز.')]
    return {}, [FieldError(sheet='الملف', row=0, column='', msg_ar='اختر ملف Excel بصيغة XLSX أو ملف CSV.')]


def load_csv(contents, template):
    reader = csv.reader(StringIO(contents.decode('utf-8-sig'), newline=''), strict=True)
    headers = [value.strip() for value in next(reader, [])]
    if headers.count('sheet') != 1:
        return {}, [FieldError(sheet='الملف', row=1, column='sheet', msg_ar='أضف عمود sheet واحدًا لتحديد الجدول لكل صف.')]
    key_index = headers.index('sheet')
    columns = headers[:key_index] + headers[key_index + 1:]
    sheets = {}
    errors = []
    for row in reader:
        if not any(value.strip() for value in row):
            continue
        if len(row) != len(headers):
            errors.append(FieldError(sheet='الملف', row=reader.line_num, column='', msg_ar='عدد الخانات لا يطابق العناوين.'))
            continue
        name = row[key_index].strip()
        if name not in template.entities:
            errors.append(FieldError(sheet='الملف', row=reader.line_num, column='sheet', msg_ar='اسم الجدول غير معروف.'))
            continue
        sheets.setdefault(name, Sheet(columns)).rows.append(
            (reader.line_num, row[:key_index] + row[key_index + 1:])
        )
    return sheets, errors
