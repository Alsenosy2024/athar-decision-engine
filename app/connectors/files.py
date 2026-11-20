import csv
from io import BytesIO, StringIO
from pathlib import Path
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile, ZipFile

from defusedxml.common import DefusedXmlException
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from app.connectors.mapping import MAX_ROWS
from app.ingestion.loader import Sheet
from app.schemas import api_error

MAX_COLUMNS = 120
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def upload_bytes(file):
    contents = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(contents) > MAX_UPLOAD_BYTES:
        raise api_error(413, 'import_file_too_large', 'الحد الأقصى لحجم الملف 10 ميجابايت.')
    return contents


def tabular_sheet(rows):
    rows = iter(rows)
    headers = [str(value).strip() if value is not None else '' for value in next(rows, [])]
    while headers and not headers[-1]:
        headers.pop()
    if not headers or len(headers) > MAX_COLUMNS:
        raise ValueError
    sheet = Sheet(headers)
    for row_no, row in enumerate(rows, 2):
        if row_no > MAX_ROWS + 1:
            raise api_error(422, 'too_many_rows', f'الحد الأقصى {MAX_ROWS} صف لكل ورقة.')
        if not any(cell is not None and str(cell).strip() for cell in row):
            continue
        if any(cell is not None and str(cell).strip() for cell in row[len(headers):]):
            raise api_error(422, 'unmapped_row_width', f'الصف {row_no} فيه خانات من غير عناوين أعمدة.')
        sheet.rows.append((row_no, list(row[:len(headers)]) + [None] * max(0, len(headers) - len(row))))
    return sheet


def read_tables(filename, contents, selected=None, *, load_rows=False):
    try:
        suffix = Path(filename or '').suffix.lower()
        if suffix == '.csv':
            reader = csv.reader(StringIO(contents.decode('utf-8-sig'), newline=''), strict=True)
            return {'CSV': tabular_sheet(reader)}
        if suffix == '.xlsx':
            with ZipFile(BytesIO(contents)) as archive:
                if sum(item.file_size for item in archive.infolist()) > 40 * 1024 * 1024:
                    raise api_error(413, 'import_file_too_large', 'حجم ملف Excel بعد فك الضغط كبير.')
            book = load_workbook(BytesIO(contents), read_only=True, data_only=True, keep_links=False)
            try:
                if selected is not None and selected not in book.sheetnames:
                    raise api_error(422, 'unknown_sheet', 'الورقة المختارة مش موجودة في الملف.')
                names = [selected] if selected is not None else book.sheetnames
                if len(names) > 30:
                    raise api_error(422, 'too_many_sheets', 'الحد الأقصى 30 ورقة Excel.')
                tables = {}
                for name in names:
                    sheet = book[name]
                    # Ignore a stale worksheet dimension; stop at the first out-of-bounds row.
                    sheet.reset_dimensions()
                    rows = sheet.iter_rows(values_only=True)
                    if selected is not None or load_rows:
                        tables[name] = tabular_sheet(rows)
                    else:
                        # A title-only or blank sheet can coexist with the selected data sheet.
                        header = [str(value).strip() if value is not None else '' for value in next(rows, ())]
                        tables[name] = Sheet(header[:MAX_COLUMNS])
                return tables
            finally:
                book.close()
    except (BadZipFile, InvalidFileException, DefusedXmlException, ParseError, ValueError, KeyError, OSError, csv.Error):
        raise api_error(422, 'invalid_tabular_file', 'تعذّرت قراءة الملف. راجع الصيغة والعناوين والترميز.') from None
    raise api_error(422, 'invalid_tabular_format', 'اختار ملف Excel بصيغة XLSX أو ملف CSV بترميز UTF-8.')
