"""Offline Arabic/English OCR with bounded PDF and image rasterization."""
from contextlib import closing
from io import BytesIO
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from threading import Lock

import pypdfium2 as pdfium
from PIL import Image, ImageOps, ImageSequence
from tesserocr import OEM, PSM, PyTessBaseAPI

from app.config import ROOT

MAX_PIXELS = 24_000_000
MAX_SIDE = 2600
PDFIUM_LOCK = Lock()


def pdf_images(contents, pages):
    # PDFium serializes calls across unrelated documents as well as rendering.
    for index in pages:
        with PDFIUM_LOCK, pdfium.PdfDocument(contents) as pdf, closing(pdf[index]) as page:
            scale = min(3.0, MAX_SIDE / max(page.get_size()))
            with closing(page.render(scale=scale)) as bitmap:
                image = bitmap.to_pil().copy()
        yield index, image


def image_frames(contents):
    with Image.open(BytesIO(contents)) as source:
        if getattr(source, 'n_frames', 1) > 30:
            raise ValueError('الحد الأقصى 30 صورة في الملف.')
        for index, frame in enumerate(ImageSequence.Iterator(source)):
            if frame.width * frame.height > MAX_PIXELS:
                raise ValueError('الصورة أكبر من حد المعاينة. صغّرها وحاول تاني.')
            rendered = ImageOps.exif_transpose(frame).convert('RGB')
            rendered.thumbnail((MAX_SIDE, MAX_SIDE))
            yield index, rendered


def heic_as_png(contents):
    with TemporaryDirectory(prefix='athar-heic-') as temp:
        source = Path(temp, 'image.heic')
        target = Path(temp, 'image.png')
        source.write_bytes(contents)
        subprocess.run(['/usr/bin/sips', '-s', 'format', 'png', '-Z', str(MAX_SIDE), str(source),
                        '--out', str(target)], capture_output=True, timeout=25, check=True)
        return target.read_bytes()


def recognize(contents, suffix, pages):
    tessdata = Path(os.environ.get('ATHAR_OCR_DATA', ROOT / '.ocr-data'))
    if not all((tessdata / f'{language}.traineddata').is_file() for language in ('ara', 'eng')):
        raise ValueError('ملفات OCR محتاجة تجهيز. شغّل scripts/setup_ocr.sh مرة واحدة.')
    if suffix == '.heic':
        contents = heic_as_png(contents)
    frames = pdf_images(contents, pages) if suffix == '.pdf' else image_frames(contents)
    found = {}
    try:
        with PyTessBaseAPI(path=str(tessdata), lang='ara+eng', oem=OEM.LSTM_ONLY, psm=PSM.AUTO) as engine:
            for index, image in frames:
                engine.SetImage(image)
                text = (engine.GetUTF8Text() or '').strip()
                found[str(index)] = {'text': text, 'confidence': engine.MeanTextConf() / 100 if text else 0}
    except (RuntimeError, pdfium.PdfiumError) as error:
        raise ValueError('الـOCR مقدرش يقرا الصفحة أو الصورة.') from error
    return found
