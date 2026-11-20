import csv
from copy import deepcopy

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.ingestion.loader import load_demo
from app.templates_registry import load_templates

ACTUALS = {
    'contracting': (('activity_id', 'actual_start_week', 'actual_finish_week', 'actual_cost'), (
        ('A0', 0, 4, 600), ('A1', 4, 12, 2080), ('A2', 12, 18, 840),
    )),
    'production': (('order_id', 'actual_ship_day', 'actual_qty'), (
        ('O1', 3, 7000), ('O2', 4, 6500), ('O3', 5, 5000),
    )),
}


def write_sample_templates(directory):
    directory.mkdir(parents=True, exist_ok=True)
    for template_id, template in load_templates().items():
        sample = load_demo(template_id)
        if template_id == 'contracting':
            sample['activities'] = [sample['activities'][index] for index in (0, 4, 8)]
            sample['history'] = [sample['history'][index] for index in (0, 20, 40)]
        else:
            third_line = deepcopy(sample['lines'][0])
            third_line.update(id='L3', name_ar='خط التعبئة')
            sample['lines'].append(third_line)
            sample['rates'] = [
                sample['rates'][0],
                sample['rates'][8],
                {'line_id': 'L3', 'sku_id': 'S2', 'units_per_day': 3600},
            ]
        book = Workbook()
        book.remove(book.active)
        for name, columns in template.upload_sheets.items():
            sheet = book.create_sheet(name)
            sheet.sheet_view.rightToLeft = True
            sheet.freeze_panes = 'A2'
            sheet.append(columns)
            for record in sample[name][:3]:
                sheet.append([record[column] for column in columns])
            for cell in sheet[1]:
                cell.font = Font(bold=True, color='FFFFFF')
                cell.fill = PatternFill('solid', fgColor='17382F')
                cell.alignment = Alignment(horizontal='center')
                sheet.column_dimensions[get_column_letter(cell.column)].width = max(18, len(cell.value) + 3)
        book.save(directory / f'{template_id}_template.xlsx')
        book.close()

        columns, rows = ACTUALS[template_id]
        with (directory / f'actuals_{template_id}_template.csv').open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(columns)
            writer.writerows(rows)
