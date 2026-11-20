"""Explicit assignment of install-wide ERP configs left by older databases."""
import csv
from datetime import datetime
from io import StringIO

from pydantic import TypeAdapter, ValidationError

from app.connectors import erp
from app.connectors.files import MAX_UPLOAD_BYTES
from app.db import audit
from app.schemas import api_error

config_adapter = TypeAdapter(erp.ConnectorConfig)
OWNERSHIP_TRIGGERS = ("""
CREATE TRIGGER IF NOT EXISTS erp_owner_required BEFORE INSERT ON erp_connectors
WHEN NEW.company_id IS NULL
BEGIN SELECT RAISE(ABORT, 'ERP connector owner required'); END;
""", """
CREATE TRIGGER IF NOT EXISTS erp_owner_immutable BEFORE UPDATE OF company_id ON erp_connectors
WHEN OLD.company_id IS NOT NULL AND NEW.company_id IS NOT OLD.company_id
BEGIN SELECT RAISE(ABORT, 'ERP connector owner cannot change'); END;
""")


def legacy_configs(connection):
    result = []
    for row in connection.execute('SELECT id, config_json, created_at FROM erp_connectors '
                                  'WHERE company_id IS NULL ORDER BY id'):
        try:
            config = config_adapter.validate_json(row['config_json'])
        except ValidationError:
            result.append({'id': row['id'], 'created_at': row['created_at'], 'claimable': False})
        else:
            result.append({'id': row['id'], 'created_at': row['created_at'], 'claimable': True,
                           'name': config.name, 'kind': config.kind})
    return result


def claim(connection, company_id, connector_id):
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        row = connection.execute('SELECT config_json FROM erp_connectors WHERE company_id IS NULL AND id=?',
                                 (connector_id,)).fetchone()
        if row is None:
            raise api_error(404, 'erp_connector_not_found', 'الموصل مش موجود ضمن الإعدادات القديمة غير المسندة.')
        try:
            config = config_adapter.validate_json(row['config_json'])
        except ValidationError:
            raise api_error(422, 'erp_invalid_legacy_config', 'الإعداد القديم غير صالح. اعمل إعداد جديد للشركة.') from None
        connection.execute('UPDATE erp_connectors SET company_id=? WHERE id=?', (company_id, connector_id))
        audit(connection, company_id, 'erp.claim', 'erp_connector', connector_id)
    return {'id': connector_id, **config.model_dump()}


def entry_file(connector_id, config):
    table = erp.pull(config)
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(table.headers)
    for _, row in table.rows:
        writer.writerow([value.date() if isinstance(value, datetime) else value for value in row])
    raw = output.getvalue().encode('utf-8')
    if len(raw) > MAX_UPLOAD_BYTES:
        raise api_error(413, 'import_file_too_large', 'حجم بيانات الموصل أكبر من حد المعاينة.')
    return f'erp-{connector_id}.csv', raw
