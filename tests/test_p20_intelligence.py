from datetime import date, timedelta
from email.message import EmailMessage
from io import BytesIO
import json
from zipfile import ZipFile

import numpy as np
from openpyxl import Workbook
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
import pytest

from app.config import Settings
from app.db import get_conn
from app.enterprise.schema import ModelInput, Node
from app.intelligence import analysis, calibration, documents, extraction, forecasting
from app.intelligence.grounding import answer
from app.llm import codex_cli

SCOPE = '/api/companies/1/ai'
VALUES = {'cash': 120000, 'pnl.result': 55000, 'pnl.revenue': 200000, 'inventory': 850, 'credit': 75000}
LINES = ['Cash 120,000 SAR', 'Net profit 55,000 SAR', 'Revenue 200,000 SAR',
         'Inventory 850 units', 'Credit limit 75,000 SAR']
CSV = ('Metric,Value,Unit\nCash,"120,000",SAR\nNet profit,"55,000",SAR\n'
       'Revenue,"200,000",SAR\nInventory,850,units\nCredit limit,"75,000",SAR\n').encode()


def as_pdf(lines):
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({
        NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
        NameObject('/BaseFont'): NameObject('/Helvetica'),
    })
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({
        NameObject('/F1'): font,
    })})
    stream = DecodedStreamObject()
    body = '\n'.join(f'1 0 0 1 54 {690 - i * 28} Tm ({line}) Tj' for i, line in enumerate(lines))
    stream.set_data(('BT /F1 18 Tf\n' + body + '\nET').encode())
    page[NameObject('/Contents')] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def as_docx(lines):
    output = BytesIO()
    paragraphs = ''.join(f'<w:p><w:r><w:t>{line}</w:t></w:r></w:p>' for line in lines)
    with ZipFile(output, 'w') as archive:
        archive.writestr('word/document.xml', f'<w:document xmlns:w="{documents.W[1:-1]}">'
                         f'<w:body>{paragraphs}</w:body></w:document>')
    return output.getvalue()


def as_excel():
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.append(['November summary'])
    worksheet.append([])
    worksheet.append(['Cash', 'Net profit', 'Revenue', 'Inventory', 'Credit limit'])
    worksheet.append(list(VALUES.values()))
    extra = workbook.create_sheet('Footer')
    extra.append(['Report reviewed'])
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def as_email():
    message = EmailMessage()
    message['Subject'] = 'Monthly data'
    message.set_content('See the attached table.')
    message.add_attachment(CSV, maintype='text', subtype='csv', filename='balances.csv')
    return message.as_bytes()


def as_image():
    from PIL import Image, ImageDraw, ImageFont
    from pathlib import Path

    photo = Image.new('RGB', (1550, 640), 'white')
    font = Path('/System/Library/Fonts/Supplemental/Arial.ttf')
    draw = ImageDraw.Draw(photo)
    draw.multiline_text((80, 50), '\n'.join(LINES), font=ImageFont.truetype(str(font), 56) if font.is_file()
                        else ImageFont.load_default(size=56), fill='black', spacing=25)
    buffer = BytesIO()
    photo.save(buffer, format='PNG')
    return buffer.getvalue()


@pytest.mark.parametrize(('filename', 'content'), [
    ('export.csv', lambda: CSV), ('irregular.xlsx', as_excel),
    ('statement.pdf', lambda: as_pdf(LINES)), ('invoice.docx', lambda: as_docx(LINES)),
    ('export.eml', as_email), ('scan.png', as_image), ('notes.txt', lambda: '\n'.join(LINES).encode()),
])
def test_source_backed_fixture_accuracy(filename, content, monkeypatch):
    if filename.endswith('.png'):
        def recognize_scan(payload, suffix, **kwargs):
            assert suffix == '.png' and payload.startswith(b'\x89PNG')
            return {'0': {'text': '\n'.join(LINES), 'confidence': 0.82}}
        monkeypatch.setattr(documents, 'native_read', recognize_scan)
    rows, _ = documents.read_documents([(filename, content())], '')
    result = extraction.preview(rows, None, 'شركة تجريبية', Settings(llm_provider='rules'), {})
    actual = {item['node_id']: item['value'] for item in result['diff']}
    assert len(actual) == len(VALUES)
    assert sum(actual.get(key) == value for key, value in VALUES.items()) / len(VALUES) >= 0.90
    assert all(item['literal'] in item['evidence'] and item['source'].startswith(filename)
               for item in result['diff'])
    if filename.endswith('.png'):
        assert all(item['confidence'] == 0.82 for item in result['diff'])


def post_entry(client, description=None, model_id=None, files=None):
    form = {}
    if description:
        form['description'] = description
    if model_id:
        form['model_id'] = model_id
    response = client.post(SCOPE + '/entry', data=form, files=files)
    assert response.status_code == 201, response.text
    return response.json()


def approve(client, review, **body):
    return client.post(f"{SCOPE}/reviews/{review['id']}/decide", json={'status': 'accepted', **body})


def test_entry_diff_and_stale_decision_are_atomic(client, db_path):
    draft = post_entry(client, 'السيولة ١٢٠ ألف ريال، حد الائتمان ٨٠٠٠٠ ريال')
    assert len(draft['data']['diff']) == 2
    assert client.get('/api/enterprise/models').json() == []
    accepted = approve(client, draft).json()
    model_id = accepted['outcome']['model_id']
    assert accepted['created_by'] == accepted['decided_by'] == 'qa-admin'
    next_review = post_entry(client, 'السيولة 150000 ريال', model_id)
    stale = post_entry(client, 'السيولة 140000 ريال', model_id)
    assert next_review['data']['diff'][0]['before'] == 120000
    assert approve(client, next_review).json()['outcome']['version'] == 2
    assert approve(client, stale).status_code == 409
    assert approve(client, next_review).status_code == 409
    rejected = client.post(f"{SCOPE}/reviews/{stale['id']}/decide", json={'status': 'rejected'})
    assert rejected.json()['status'] == 'rejected'
    with get_conn(db_path) as connection:
        records = connection.execute("SELECT action, actor FROM audit_log WHERE action LIKE 'ai.%'").fetchall()
    assert {row['action'] for row in records} == {'ai.propose.entry', 'ai.accepted', 'ai.rejected'}
    assert {row['actor'] for row in records} == {'qa-admin'}


def test_company_scope_roles_and_csrf(client):
    from conftest import sign_in

    review = post_entry(client, 'السيولة 1000 ريال')
    path = f"{SCOPE}/reviews/{review['id']}"
    csrf = client.headers['X-CSRF-Token']
    client.headers.pop('X-CSRF-Token')
    assert approve(client, review).status_code == 403
    client.headers['X-CSRF-Token'] = csrf
    assert client.get(path.replace('/1/', '/2/')).status_code == 404
    assert client.post('/api/users', json={'username': 'ai-viewer', 'password': 'ai-viewer-password',
                                          'role': 'viewer', 'company_ids': [1]}).status_code == 201
    sign_in(client, 'ai-viewer', 'ai-viewer-password')
    assert client.get(path).status_code == 200
    assert approve(client, review).status_code == 403
    assert client.get('/api/companies/2/ai/reviews').status_code == 404


def test_currency_conflict_and_dedupe():
    raw = ('Cash 10 thousand USD\nCash 10 thousand USD\nNet profit 50000 SAR\n'
           'Net profit 70000 SAR\nInventory 850 units\nSupplier lead time 2 weeks')
    rows, _ = documents.read_documents([], raw)
    result = extraction.preview(rows, None, 'شركة', Settings(llm_provider='rules'), {'USD': 3.75})
    assert result['duplicates'] == 1
    assert {row['node_id']: row['value'] for row in result['diff']} == {
        'cash': 37500, 'inventory': 850, 'supplier.lead_time': 14,
    }
    assert 'متعارضة' in result['issues'][0]['message_ar']
    blocked = extraction.preview(rows, None, 'شركة', Settings(llm_provider='rules'), {})
    assert any('سعر تحويل' in issue['message_ar'] for issue in blocked['issues'])
    assert extraction.normalized_value('100', 'QAR', 'SAR', {'QAR': 1.03}) == 103
    assert extraction.normalized_value('100', 'ريال قطري', 'SAR', {'QAR': 1.03}) == 103


def test_onboarding_explicit_edges_and_unmeasured_node():
    rows, _ = documents.read_documents([], 'الطلب 100 وحدة\nالإيرادات 2500 ريال\nالمخزون\nالطلب -> الإيرادات = 25')
    data = extraction.preview(rows, None, 'شركة', Settings(llm_provider='rules'), {})
    model = ModelInput.model_validate(data['model'])
    assert {node.id: node.baseline.value for node in model.graph.nodes} == {
        'demand': 100, 'pnl.revenue': 2500, 'inventory': None,
    }
    assert model.graph.edges[0].rule.multiplier == 25
    assert data['relations'][0]['evidence'] == 'الطلب -> الإيرادات = 25'


def test_codex_onboarding_selects_only_quoted_company_evidence(monkeypatch):
    rows, _ = documents.read_documents([], 'بنبيع الأجهزة بالأجل وعندنا مخزن')

    def complete(*_):
        return json.dumps({'entities': [
            {'row': 0, 'node_id': 'ar', 'quote': 'بنبيع الأجهزة بالأجل', 'confidence': 0.88},
            {'row': 0, 'node_id': 'cash', 'quote': 'رصيد كاش من عندي', 'confidence': 0.99},
        ]})

    monkeypatch.setattr(codex_cli, 'complete', complete)
    draft = extraction.preview(rows, None, 'الشركة', Settings(llm_provider='codex'), {})
    assert {(row['node_id'], row['value'], row['provider']) for row in draft['diff']} == {
        ('storage', None, 'rules'), ('ar', None, 'codex'),
    }


def test_sparse_xlsx_never_silently_drops_source_cells():
    from fastapi import HTTPException

    workbook = Workbook()
    workbook.active['A1'] = 'Cash 100'
    workbook.active['AZ10000'] = 'Profit 400'
    buffer = BytesIO()
    workbook.save(buffer)
    with pytest.raises(HTTPException) as error:
        documents.read_documents([('far.xlsx', buffer.getvalue())], '')
    assert error.value.status_code == 422


def test_scanned_pdf_renders_for_ocr():
    from PIL import Image

    from app.intelligence.ocr import pdf_images

    output = BytesIO()
    with Image.open(BytesIO(as_image())) as image:
        image.save(output, 'PDF')
    [(index, rendered)] = list(pdf_images(output.getvalue(), [0]))
    assert index == 0 and 1000 < rendered.width <= 2600


def test_gap_estimate_remains_optional_and_persisted(client):
    contents = ('Date,Cash\n2026-01-01,100\n2026-01-08,110\n2026-01-15,120\n'
                '2026-01-22,\n2026-01-29,130\n').encode()
    draft = post_entry(client, files=[('files', ('weekly.csv', contents, 'text/csv'))])
    assert [item['value'] for item in draft['data']['estimates']] == [110]
    assert 110 not in [item['value'] for item in draft['data']['diff'] if item['date'] == '2026-01-22']
    accepted = approve(client, draft, estimates=[0]).json()
    assert accepted['outcome']['accepted_estimates'][0]['estimated'] is True
    current = client.get('/api/enterprise/models/' + str(accepted['outcome']['model_id'])).json()
    assert [item['date'] for item in current['graph']['nodes'][0]['baseline']['points']][3] == '2026-01-22'
    forecast_request = {'model_id': current['id'], 'node_id': 'cash'}
    forecast = client.post(SCOPE + '/forecast', json=forecast_request).json()['data']
    assert forecast['estimated_excluded'] == 1 and forecast['history_count'] == 1
    actual = post_entry(client, model_id=current['id'], files=[
        ('files', ('actual.csv', b'Date,Cash\n2026-01-22,109\n', 'text/csv')),
    ])
    assert approve(client, actual).status_code == 200
    forecast = client.post(SCOPE + '/forecast', json=forecast_request).json()['data']
    assert forecast['estimated_excluded'] == 0 and forecast['history_count'] == 5


def test_provider_uses_only_grounded_number_and_identifier(monkeypatch):
    raw = 'Funds immediately on hand 120000 SAR\nOther liquid assets 40000 SAR'
    rows, _ = documents.read_documents([], raw)

    def completion(_home, prompt, _schema):
        assert 'untrusted' in prompt
        return json.dumps({'mappings': [
            {'row': 0, 'node_id': 'cash', 'literal': '120000', 'confidence': 0.87},
            {'row': 1, 'node_id': 'cash', 'literal': '999999', 'confidence': 0.9},
        ]})
    monkeypatch.setattr(codex_cli, 'complete', completion)
    data = extraction.preview(rows, None, 'شركة', Settings(llm_provider='codex'), {})
    assert [(item['value'], item['confidence']) for item in data['diff']] == [(120000, 0.87)]
    assert data['provider'] == 'codex' and len(data['unmapped']) == 1


def test_grounded_answer_rejects_unproven_fact_ids(monkeypatch):
    def bad_completion(*args):
        return '{"ids":[0, 999]}'
    monkeypatch.setattr(codex_cli, 'complete', bad_completion)
    facts = [analysis.fact('cash', 'السيولة 120 ريال', {'value': 120})]
    result = answer(Settings(llm_provider='codex'), 'الكاش كام؟', facts, [0])
    assert result['provider'] == 'rules'
    assert result['reply_ar'] == 'السيولة 120 ريال'


def test_p6_plan_records_are_reviewable(client):
    xml = b'''<APIBusinessObjects><Project><ObjectId>100</ObjectId><Id>PR-01</Id>
        <Name>Office</Name><Activity><ObjectId>101</ObjectId><Id>A01</Id><Name>Steel</Name>
        <PlannedDuration>48</PlannedDuration><PlannedTotalCost>5000</PlannedTotalCost>
        </Activity></Project></APIBusinessObjects>'''
    draft = post_entry(client, files=[('files', ('p6.xml', xml, 'text/xml'))])
    [schedule] = draft['data']['schedules']
    [activity] = schedule['records']['activities']
    assert activity['id'] == 'A01' and activity['duration_hours'] == 48 and activity['planned_cost'] == 5000
    outcome = approve(client, draft).json()['outcome']
    assert outcome['schedules_reviewed'] == 1
    [import_id] = outcome['p6_import_ids']
    assert client.get(f'/api/companies/1/p6-imports/{import_id}/export.p6.xml').content == xml


def series_node(key, values, node_type='demand', unit='units'):
    start = date(2024, 1, 1)
    return {
        'id': key, 'name_ar': {'cash': 'السيولة', 'pnl.result': 'الربح', 'demand': 'الطلب'}.get(key, key),
        'type': node_type, 'unit': unit,
        'baseline': {'kind': 'series', 'frequency': 'weekly',
                     'points': [{'date': (start + timedelta(weeks=index)).isoformat(), 'value': float(value)}
                                for index, value in enumerate(values)]},
        'definition': {'kind': 'formula', 'expression': 'historical baseline'},
    }


def save_model(client, nodes, edges=()):
    response = client.post('/api/enterprise/models?company_id=1',
                           json={'name': 'شركة اختبار', 'graph': {'nodes': nodes, 'edges': list(edges)}})
    assert response.status_code == 201, response.text
    return response.json()['id']


class MockFoundation:
    def __init__(self, bias):
        self.bias = bias
        self.calls = []

    def forecast(self, inputs, horizon):
        self.calls.extend(inputs)
        point = np.asarray([[serie[-1] + index + 1 + self.bias for index in range(horizon)] for serie in inputs])
        return np.stack((point - 1, point, point + 1), axis=1).tolist()


def test_forecast_chooses_ensemble_and_conformalizes_held_out():
    model = Node.model_validate(series_node('demand', range(1, 70)))
    timesfm, chronos = MockFoundation(3), MockFoundation(-3)
    result = forecasting.ensemble_forecast(model, 4, timesfm, chronos)
    assert set(result['backtests']) == {'stat', 'timesfm', 'chronos_bolt', 'ensemble'}
    assert result['engine'] == 'ensemble'
    assert result['calibration_n'] == 14 and result['confidence'] == 0.8
    assert result['evaluation_coverage'] == [1.0] * 4
    assert result['points'][0]['p50'] == 70
    assert len(timesfm.calls[-1]) == 69
    assert all(p['p10'] <= p['p50'] <= p['p90'] for p in result['points'])


def test_forecast_short_history_has_no_calibrated_claim(client):
    model_id = save_model(client, [series_node('demand', [12, 13, 14])])
    response = client.post(SCOPE + '/forecast', json={'model_id': model_id, 'node_id': 'demand'})
    assert response.status_code == 201, response.text
    data = response.json()['data']
    assert data['engine'] == 'stat' and data['interval_method'] == 'uncalibrated'
    assert data['confidence'] is None


def test_active_production_year_can_calibrate_four_periods(client):
    scope = SCOPE.replace('/1/', '/2/')
    result = client.post(scope + '/forecast', json={'node_id': 'demand.S1', 'horizon': 4})
    assert result.status_code == 201, result.text
    forecast = result.json()['data']
    assert forecast['history_count'] == 52
    assert forecast['confidence'] == 0.8 and forecast['calibration_n'] == 14
    assert 'week' in forecast['points'][0] and 'date' not in forecast['points'][0]


def test_calibration_fits_lag_then_is_reviewed(client):
    rng = np.random.default_rng(42)
    increments = rng.normal(0, 3, 90)
    upstream = 100 + np.cumsum(increments)
    output = 300 + np.cumsum(np.r_[0, 2.5 * increments[:-1]] + rng.normal(0, 0.05, 90))
    edge = {'source': 'demand', 'target': 'pnl.result', 'name_ar': 'أثر الطلب على الربح',
            'rule': {'multiplier': 1}}
    model_id = save_model(client, [
        series_node('demand', upstream), series_node('pnl.result', output, 'profit_loss', 'SAR'),
    ], [edge])
    response = client.post(SCOPE + '/calibrate', json={'model_id': model_id})
    assert response.status_code == 201, response.text
    draft = response.json()
    [link] = draft['data']['edges']
    assert link['lag_weeks'] == 1 and link['multiplier'] == pytest.approx(2.5, abs=0.03)
    assert link['holdout_skill'] > 0.9
    assert approve(client, draft).json()['outcome']['version'] == 2


def test_causal_discovery_orients_collider():
    rng = np.random.default_rng(42)
    days = [date(2024, 1, 1) + timedelta(weeks=i) for i in range(500)]
    x, y, noise = rng.normal(size=(3, len(days)))
    discovered = calibration.pc_links({key: dict(zip(days, values)) for key, values in
                                       [('x', x), ('y', y), ('z', 2*x + 2*y + noise * 0.15)]})
    assert discovered == {('x', 'z'), ('y', 'z')}


def test_refresh_uses_past_only_and_question_traces_history(client):
    model_id = save_model(client, [
        series_node('demand', [100] * 26 + [50]), series_node('cash', [500] * 26 + [200], 'cash', 'SAR'),
    ], [{'source': 'demand', 'target': 'cash', 'name_ar': 'أثر الطلب على السيولة'}])
    refresh = client.post(SCOPE + '/refresh', json={'model_id': model_id}).json()
    assert {item['node_id'] for item in refresh['data']['anomalies']} == {'cash', 'demand'}
    assert refresh['data']['anomalies'][0]['reference'] == 500
    reply = client.post(SCOPE + '/question', json={'model_id': model_id, 'question': 'ليه الكاش نزل؟'}).json()['data']
    [evidence] = [item['evidence'] for item in reply['facts']]
    assert evidence['kind'] == 'history' and evidence['path'][0]['source'] == 'demand'
    assert 'السيولة' in reply['reply_ar'] and '200' in reply['reply_ar']


def test_goal_uses_propagation_and_saves_only_feasible_review(client):
    model_id = save_model(client, [
        series_node('demand', [100]), series_node('cash', [500], 'cash', 'SAR'),
        series_node('pnl.result', [1000], 'profit_loss', 'SAR'),
    ], [{'source': 'demand', 'target': 'pnl.result', 'name_ar': 'الطلب يرفع الربح',
         'rule': {'multiplier': 5}},
        {'source': 'demand', 'target': 'cash', 'name_ar': 'الطلب يحتاج سيولة',
         'rule': {'multiplier': -2}}])
    goal_text = client.post(SCOPE + '/goal/interpret', json={
        'model_id': model_id, 'text': 'عايز أرفع الربح 10% والسيولة ما تنزلش عن 450 ريال',
    }).json()
    assert goal_text == {'objective': 'pnl.result', 'direction': 'maximize', 'target_pct': 10, 'cash_floor': 450}
    catalog = client.get(SCOPE + '/catalog', params={'model_id': model_id}).json()
    assert {driver['id'] for driver in catalog['drivers']} == {'demand'}
    blocked = client.post(SCOPE + '/recommend', json={'model_id': model_id, 'goal': {
        'controls': [{'node_id': 'pnl.result', 'low': 990, 'high': 1100}],
    }})
    assert blocked.status_code == 422
    response = client.post(SCOPE + '/recommend', json={'model_id': model_id, 'goal': {
        'target_pct': 10, 'cash_floor': 450, 'trials': 8,
        'controls': [{'node_id': 'demand', 'low': 80, 'high': 125}],
    }})
    assert response.status_code == 201, response.text
    draft = response.json()
    best = draft['data']['recommendations'][0]
    assert best['objective'] == 1125 and best['cash'] == 450 and best['target_met']
    assert best['critique']['weakest']['node_id'] == 'demand'
    assert best['critique']['weakest']['objective_loss'] == 225
    drill = client.post(SCOPE + '/question', json={
        'model_id': model_id, 'proposal_id': draft['id'], 'question': 'ليه الربح زاد؟'
    }).json()['data']
    assert drill['facts'][0]['evidence']['path'][0]['name_ar'] == 'الطلب يرفع الربح'
    outcome = approve(client, draft).json()['outcome']
    scenarios = client.get('/api/enterprise/scenarios').json()
    assert outcome['scenario_id'] == scenarios[0]['id']
    assert scenarios[0]['assumptions'][0]['value'] == 125
