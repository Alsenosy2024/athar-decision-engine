import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.ingestion.sample_templates import write_sample_templates

VENDOR_URLS = {
    'chart.umd.js': 'https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.js',
    'alpine.min.js': 'https://cdn.jsdelivr.net/npm/alpinejs@3.14.9/dist/cdn.min.js',
}


def main():
    static = ROOT / 'static'
    fonts = static / 'fonts'
    fonts.mkdir(parents=True, exist_ok=True)
    for family in ('Text', 'Display'):
        for weight in ('Bold', 'Black'):
            source = Path.home() / 'Library' / 'Fonts' / f'thmanyahserif{family.lower()}-{weight}.otf'
            target = fonts / f'ThmanyahSerif{family}-{weight}.otf'
            if not source.is_file():
                if target.is_file():
                    continue
                raise SystemExit(f'Required font missing: {source}')
            shutil.copyfile(source, fonts / f'ThmanyahSerif{family}-{weight}.otf')

    vendor = static / 'vendor'
    vendor.mkdir(parents=True, exist_ok=True)
    for name, url in VENDOR_URLS.items():
        destination = vendor / name
        if destination.exists() and destination.stat().st_size > 0:
            continue
        download = subprocess.run(['curl', '--fail', '--silent', '--show-error', '--location', url],
                                  check=True, capture_output=True)
        destination.write_bytes(download.stdout)

    write_sample_templates(static / 'templates')


if __name__ == '__main__':
    main()
