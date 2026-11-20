"""Bounded local readers. Evidence addresses survive normalization and review."""
import csv
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from io import BytesIO, StringIO
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
from xml.etree.ElementTree import ParseError
from zipfile import ZipFile, BadZipFile

from defusedxml.ElementTree import fromstring
from defusedxml.common import DefusedXmlException
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.worksheet._reader import WorkSheetParser
from pypdf.errors import PyPdfError
from xlrd.biffh import XLRDError

from app.connectors.p6 import read_p6
from app.schemas import api_error

MAX_ROWS = 1200
MAX_COLS = 80
MAX_PAGES = 30
W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
IMAGES = {'.png', '.jpg', '.jpeg', '.tif', '.tiff', '.heic', '.webp', '.bmp'}


@dataclass(frozen=True)
class Evidence:
    source: str
    cells: tuple[str, ...]
    confidence: float = 1.0


def clean(value):
    return '' if value is None else str(value).strip()


def evidence(source, rows, *, confidence=1.0):
    for index, row in enumerate(rows, 1):
        if index > MAX_ROWS or len(row) > MAX_COLS:
            raise ValueError('الملف أكبر من حد المعاينة. قسّمه لأجزاء أصغر.')
        cells = tuple(clean(value) for value in row)
        if any(len(cell) > 6000 for cell in cells):
            raise ValueError('فيه خانة نص أطول من حد المعاينة.')
        if any(cells):
            yield Evidence(f'{source}:{index}', cells, confidence)


def text_rows(text):
    # Keep the original text for source-grounded selection; preserve table columns where possible.
    return ((re.split(r'\t+| {2,}', line.strip()) if line.strip() else ()) for line in text.splitlines())


def text_file(contents):
    for encoding in ('utf-8-sig', 'utf-16', 'cp1256'):
        try:
            return contents.decode(encoding)
        except UnicodeError:
            continue
    raise ValueError('ترميز الملف مش مدعوم.')


def check_zip(contents):
    with ZipFile(BytesIO(contents)) as archive:
        if (len(archive.infolist()) > 2000
                or sum(item.file_size for item in archive.infolist()) > 40 * 1024 * 1024):
            raise ValueError('حجم الملف بعد فك الضغط أكبر من حد المعاينة.')


def workbook(name, contents, suffix):
    if suffix == '.xls':
        import xlrd

        book = xlrd.open_workbook(file_contents=contents, on_demand=True)
        try:
            if book.nsheets > 30:
                raise ValueError('الحد الأقصى 30 ورقة.')
            for sheet in book.sheets():
                yield from evidence(f'{name}/{sheet.name}', (sheet.row_values(i) for i in range(sheet.nrows)))
        finally:
            book.release_resources()
        return
    check_zip(contents)
    book = load_workbook(BytesIO(contents), read_only=True, data_only=True, keep_links=False)
    try:
        if len(book.worksheets) > 30:
            raise ValueError('الحد الأقصى 30 ورقة.')
        for sheet in book.worksheets:
            # Iterate actual cells, including sparse sheets and stale dimensions.
            # The pinned openpyxl parser preserves cached values and typed dates.
            with sheet._get_source() as source:
                parser = WorkSheetParser(source, sheet._shared_strings, data_only=True,
                                         epoch=book.epoch, date_formats=book._date_formats,
                                         timedelta_formats=book._timedelta_formats)
                for row_no, sparse in parser.parse():
                    populated = {cell['column']: clean(cell['value']) for cell in sparse if cell['value'] is not None}
                    if not populated:
                        continue
                    if row_no > MAX_ROWS or max(populated) > MAX_COLS:
                        raise ValueError('الملف أكبر من حد المعاينة. قسّمه لأجزاء أصغر.')
                    cells = tuple(populated.get(column, '') for column in range(1, max(populated) + 1))
                    if any(len(cell) > 6000 for cell in cells):
                        raise ValueError('فيه خانة نص أطول من حد المعاينة.')
                    yield Evidence(f'{name}/{sheet.title}:{row_no}', cells)
    finally:
        book.close()


def word_rows(contents):
    check_zip(contents)
    with ZipFile(BytesIO(contents)) as archive:
        document = fromstring(archive.read('word/document.xml'), forbid_dtd=True)

    def get_text(element):
        return ' '.join(''.join(node.text or '' for node in p.iter(W + 't'))
                        for p in element.iter(W + 'p'))

    body = document.find(W + 'body')
    if body is None:
        raise ValueError('ملف Word مفيهوش محتوى قابل للقراءة.')
    for block in body:
        if block.tag == W + 'p':
            yield (get_text(block),)
        elif block.tag == W + 'tbl':
            for row in block.findall(W + 'tr'):
                yield [get_text(cell) for cell in row.findall(W + 'tc')]


def native_read(contents, suffix, *, pages=()):
    if suffix != '.doc':
        from app.intelligence.ocr import recognize

        return recognize(contents, suffix, pages)
    with TemporaryDirectory(prefix='athar-ai-word-') as temp:
        path = Path(temp, 'document.doc')
        path.write_bytes(contents)
        result = subprocess.run(['/usr/bin/textutil', '-convert', 'txt', '-stdout', str(path)],
                                capture_output=True, timeout=30, check=True)
        if len(result.stdout) > 2 * 1024 * 1024:
            raise ValueError('النص المستخرج أكبر من حد المعاينة.')
        return result.stdout.decode()


def pdf_rows(name, contents):
    from pypdf import PdfReader

    pdf = PdfReader(BytesIO(contents))
    if len(pdf.pages) > MAX_PAGES:
        raise ValueError('الحد الأقصى للـPDF هو 30 صفحة.')
    texts = [page.extract_text(extraction_mode='layout') or '' for page in pdf.pages]
    missing = [i for i, text in enumerate(texts) if not text.strip()]
    scans = native_read(contents, '.pdf', pages=missing) if missing else {}
    for index, text in enumerate(texts):
        label = f'{name}/p{index + 1}'
        if text.strip():
            yield from evidence(label, text_rows(text))
        elif scan := scans.get(str(index)):
            yield from evidence(label, text_rows(scan['text']), confidence=scan['confidence'])


def messages(contents, suffix):
    if suffix != '.mbox':
        return [contents]
    # Preserve Unix mbox envelope boundaries without opening a shared mailbox.
    return [part for part in re.split(rb'^From [^\n]*\r?\n', contents, flags=re.M) if part.strip()]


def source_rows(name, contents, schedules, depth=0):
    suffix = Path(name).suffix.lower()
    if suffix in {'.xlsx', '.xlsm', '.xls'}:
        yield from workbook(name, contents, suffix)
    elif suffix in {'.csv', '.tsv'}:
        text = text_file(contents)
        try:
            dialect = csv.Sniffer().sniff(text[:8192], delimiters=',;\t')
            reader = csv.reader(StringIO(text), dialect, strict=True)
        except csv.Error:
            reader = csv.reader(StringIO(text), delimiter='\t' if suffix == '.tsv' else ',', strict=True)
        yield from evidence(name, reader)
    elif suffix == '.docx':
        yield from evidence(name, word_rows(contents))
    elif suffix == '.doc':
        yield from evidence(name, text_rows(native_read(contents, suffix)))
    elif suffix == '.pdf':
        yield from pdf_rows(name, contents)
    elif suffix in IMAGES:
        for index, scan in native_read(contents, suffix).items():
            yield from evidence(f'{name}/p{int(index) + 1}', text_rows(scan['text']),
                                confidence=scan['confidence'])
    elif suffix == '.xml':
        data, validation = read_p6(contents)
        if not validation.ok:
            raise ValueError('ملف P6 فيه مراجع أو قيم تحتاج تصحيح.')
        if sum(map(len, data.values())) > MAX_ROWS:
            raise ValueError('ملف P6 أكبر من حد المعاينة.')
        schedules.append({'source': name, 'records': data, 'xml': contents})
        rows = []
        for group, records in data.items():
            for item in records:
                rows.append([group, *(f'{key}: {value}' for key, value in item.items() if value is not None)])
        yield from evidence(name, rows)
    elif suffix in {'.eml', '.mbox'} and depth < 2:
        parts = messages(contents, suffix)
        if len(parts) > 20:
            raise ValueError('الحد الأقصى 20 إيميل في المعاينة.')
        for index, raw in enumerate(parts, 1):
            message = BytesParser(policy=policy.default).parsebytes(raw)
            body = message.get_body(preferencelist=('plain', 'html'))
            if body:
                content = body.get_content()
                if body.get_content_subtype() == 'html':
                    content = re.sub(r'</(?:p|div|tr|li)>|<br\s*/?>', '\n', content, flags=re.I)
                    content = re.sub(r'<[^>]+>', '', content)
                    from html import unescape
                    content = unescape(content)
                yield from evidence(f'{name}/mail{index}', text_rows(content))
            for attachment in message.iter_attachments():
                filename = attachment.get_filename()
                payload = attachment.get_payload(decode=True)
                if filename and payload and supported(Path(filename).suffix.lower()):
                    yield from source_rows(f'{name}/mail{index}/{safe_name(filename)}', payload, schedules, depth + 1)
    elif suffix in {'.txt', '.md'}:
        yield from evidence(name, text_rows(text_file(contents)))
    else:
        raise ValueError('الصيغة مش مدعومة. اختار Excel، CSV، PDF، Word، صورة، P6 XML أو EML/MBOX.')


def supported(suffix):
    return suffix in IMAGES | {'.xlsx', '.xlsm', '.xls', '.csv', '.tsv', '.docx', '.doc',
                              '.pdf', '.xml', '.eml', '.mbox', '.txt', '.md'}


def safe_name(name):
    return Path(name.replace('\\', '/')).name[:140]


def read_documents(files, description):
    rows, schedules = [], []
    try:
        for filename, contents in files:
            start = len(rows)
            for row in source_rows(safe_name(filename), contents, schedules):
                rows.append(row)
                if len(rows) > MAX_ROWS:
                    raise ValueError('الملفات فيها صفوف أكتر من حد المعاينة.')
            if len(rows) == start:
                raise ValueError(f'مفيش بيانات مقروءة في {safe_name(filename)}.')
        if description:
            rows.extend(evidence('وصف الشركة', text_rows(description)))
        if not rows or len(rows) > MAX_ROWS:
            raise ValueError('راجع حجم الملفات أو اكتب وصف الشركة.')
    except (BadZipFile, InvalidFileException, ParseError, DefusedXmlException, PyPdfError, XLRDError,
            subprocess.SubprocessError, OSError, csv.Error, TypeError, KeyError, UnicodeError, LookupError):
        raise api_error(422, 'ai_unreadable_document', 'تعذّرت قراءة الملف. راجع الصيغة أو جودة المسح.') from None
    except ValueError as error:
        raise api_error(422, 'ai_invalid_document', str(error)) from None
    return rows, schedules
