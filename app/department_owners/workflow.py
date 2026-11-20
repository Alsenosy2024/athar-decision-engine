"""Department assignments, review queue and atomic onboarding revisions."""
import json
from datetime import UTC, datetime, timedelta
from secrets import token_urlsafe
from time import time

from app.db import audit, encode, get_conn, utc_now
from app.enterprise import store
from app.enterprise.schema import ModelInput
from app.onboarding import ingest, storage
from app.onboarding.presets import SECTIONS
from app.onboarding.projection import materialize, readiness, record_key
from app.onboarding.records import Record
from app.schemas import api_error

STALE_DAYS = 30

DDL = """
CREATE TABLE IF NOT EXISTS department_assignments (
    company_id INTEGER NOT NULL REFERENCES onboarding_companies(company_id),
    section TEXT NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id),
    PRIMARY KEY (company_id, section)
);
CREATE INDEX IF NOT EXISTS department_assignments_user ON department_assignments(user_id);
CREATE TABLE IF NOT EXISTS department_previews (
    token TEXT PRIMARY KEY,
    company_id INTEGER NOT NULL,
    section TEXT NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id),
    version INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    diff_json TEXT NOT NULL,
    FOREIGN KEY (company_id, section) REFERENCES department_assignments(company_id, section) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS department_submissions (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES onboarding_companies(company_id),
    section TEXT NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id),
    base_version INTEGER NOT NULL,
    diff_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'rejected', 'superseded')),
    submitted_at TEXT NOT NULL,
    reviewer_id INTEGER REFERENCES users(id),
    decided_at TEXT,
    comment TEXT,
    approved_version INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS department_one_pending ON department_submissions(company_id, section)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS department_submissions_company ON department_submissions(company_id, id DESC);
"""

ELIGIBLE = """u.role = 'planner' AND EXISTS (
    SELECT 1 FROM user_companies g WHERE g.company_id=a.company_id AND g.user_id=u.id
)"""


def initialize(path):
    with get_conn(path) as connection:
        connection.executescript(DDL)


def check_section(section):
    if section not in SECTIONS:
        raise api_error(404, 'department_not_found', 'القسم المطلوب مش موجود.')


def assignments(connection, company_id):
    return {
        row['section']: dict(row)
        for row in connection.execute(f"""
            SELECT a.section, a.user_id, u.username, ({ELIGIBLE}) AS eligible
            FROM department_assignments a JOIN users u ON u.id=a.user_id
            WHERE a.company_id=?
        """, (company_id,))
    }


def ensure_owner(connection, company_id, section, user_id):
    check_section(section)
    row = connection.execute(f"""
        SELECT 1 FROM department_assignments a JOIN users u ON u.id=a.user_id
        WHERE a.company_id=? AND a.section=? AND a.user_id=? AND {ELIGIBLE}
    """, (company_id, section, user_id)).fetchone()
    if row is None:
        raise api_error(404, 'department_not_found', 'القسم المطلوب مش متاح لحسابك.')


def assign(connection, company_id, section, user_id, admin):
    check_section(section)
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        storage.snapshot(connection, company_id)
        if user_id is not None and not connection.execute("""
            SELECT 1 FROM users u JOIN user_companies g ON g.user_id=u.id
            WHERE u.id=? AND g.company_id=? AND u.role='planner'
        """, (user_id, company_id)).fetchone():
            raise api_error(422, 'invalid_department_owner', 'اختار مخطّط عنده صلاحية على الشركة دي.')
        previous = assignments(connection, company_id).get(section)
        old_id = previous['user_id'] if previous else None
        if old_id == user_id:
            return previous
        connection.execute('DELETE FROM department_assignments WHERE company_id=? AND section=?',
                           (company_id, section))
        expired = connection.execute("""
            UPDATE department_submissions SET status='superseded', reviewer_id=?, decided_at=?
            WHERE company_id=? AND section=? AND status='pending'
        """, (admin.id, utc_now(), company_id, section)).rowcount
        if user_id is not None:
            connection.execute('INSERT INTO department_assignments VALUES (?, ?, ?)', (company_id, section, user_id))
            connection.execute('UPDATE users SET department_scoped=1 WHERE id=?', (user_id,))
        audit(connection, company_id, 'department.assign', 'department', None,
              {'section': section, 'old_user_id': old_id, 'user_id': user_id, 'superseded': expired})
        return assignments(connection, company_id).get(section)


def workspace(connection, principal):
    if principal.role == 'admin':
        companies = connection.execute("""
            SELECT c.id, c.name FROM onboarding_companies o JOIN companies c ON c.id=o.company_id
            ORDER BY c.id
        """)
        return {'role': principal.role, 'companies': [dict(row) for row in companies], 'sections': []}
    rows = connection.execute(f"""
        SELECT a.company_id, c.name AS company, a.section FROM department_assignments a
        JOIN companies c ON c.id=a.company_id JOIN users u ON u.id=a.user_id
        WHERE a.user_id=? AND {ELIGIBLE} ORDER BY a.company_id, a.section
    """, (principal.id,))
    return {'role': principal.role, 'companies': [],
            'sections': [dict(row) | {'name': SECTIONS[row['section']]} for row in rows]}


def records_for(graph, section):
    return {record_key(row): row for row in graph.onboarding.records if ingest.section_for(row.key) == section}


def last_updates(connection, model, sections):
    """Find the latest version that changed each section, including legacy commits."""
    pending = set(sections)
    updates = dict.fromkeys(sections)
    previous = {section: {} for section in sections}
    for record in model['graph'].onboarding.records:
        section = ingest.section_for(record.key)
        if section in pending:
            row = record.model_dump(mode='json')
            previous[section][(row['key'], row['date'])] = row
    pending = {section for section in pending if previous[section]}
    if not pending:
        return updates
    timestamp = model['created_at']
    versions = connection.execute("""
        SELECT version, created_at, json_extract(graph_json, '$.onboarding.records') AS records_json
        FROM enterprise_model_versions WHERE model_id=? AND version < ?
        ORDER BY version DESC
    """, (model['id'], model['version']))
    for version in versions:
        older = {section: {} for section in pending}
        for row in json.loads(version['records_json'] or '[]'):
            section = ingest.section_for(row['key'])
            if section in pending:
                day = row.get('date')
                older[section][(row['key'], day)] = row
        for section in tuple(pending):
            if previous[section] != older[section]:
                updates[section] = timestamp
                pending.remove(section)
            previous[section] = older[section]
        if not pending:
            return updates
        timestamp = version['created_at']
    for section in pending:
        if previous[section]:
            updates[section] = timestamp
    return updates


def section_status(connection, model, section=None, *, now=None):
    readiness_info = readiness(model['graph'])
    updates = last_updates(connection, model, (section,) if section else SECTIONS)
    cutoff = (now or datetime.now(UTC)) - timedelta(days=STALE_DAYS)
    items = []
    for item in readiness_info['sections']:
        key = item['id']
        if key not in updates:
            continue
        timestamp = updates[key]
        reminder = ('never_updated' if timestamp is None and item['total'] else
                    'stale' if timestamp and datetime.fromisoformat(timestamp) <= cutoff else None)
        # Cross-department feature dependencies belong on the company onboarding screen.
        items.append({k: item[k] for k in ('id', 'name', 'score', 'done', 'total', 'missing')} |
                     {'last_updated': timestamp, 'reminder': reminder})
    return items


def email_reminder_hook(company_id, section, user_id, reason):
    """Return a reminder event for a future transport. The application has no sender."""
    return {'company_id': company_id, 'section': section, 'user_id': user_id, 'reason': reason}


def make_preview(connection, model, section, user_id, records, errors, *, evidence=None, issues=()):
    result = {'version': model['version'], 'token': None, 'diff': [],
              'validation': {'ok': not errors, 'errors': errors}, 'issues': issues}
    if errors:
        return result
    old = records_for(model['graph'], section)
    diff = []
    for record in records:
        before = old.get(record_key(record))
        if record != before:
            diff.append({
                'before': before.model_dump(mode='json') if before else None,
                'after': record.model_dump(mode='json'),
                'evidence': (evidence or {}).get(record_key(record)),
            })
    if not diff:
        return result
    token = token_urlsafe(32)
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        ensure_owner(connection, model['company_id'], section, user_id)
        storage.check_version(storage.snapshot(connection, model['company_id']), model['version'])
        connection.execute('DELETE FROM department_previews WHERE expires_at<=?', (int(time()),))
        connection.execute('INSERT INTO department_previews VALUES (?, ?, ?, ?, ?, ?, ?)', (
            token, model['company_id'], section, user_id, model['version'],
            int(time()) + storage.PREVIEW_SECONDS, encode(diff),
        ))
    return result | {'token': token, 'diff': diff}


def submissions(connection, company_id, section=None, user_id=None):
    query = """
        SELECT s.*, u.username, r.username AS reviewer FROM department_submissions s
        JOIN users u ON u.id=s.user_id LEFT JOIN users r ON r.id=s.reviewer_id
        WHERE s.company_id=?
    """
    params = [company_id]
    if section is not None:
        query += ' AND s.section=? AND s.user_id=?'
        params.extend((section, user_id))
    return [submission(row) for row in connection.execute(
        query + " ORDER BY (s.status='pending') DESC, s.id DESC LIMIT 50", params,
    )]


def submission(row):
    item = dict(row)
    item['diff'] = json.loads(item.pop('diff_json'))
    return item


def submit(connection, company_id, section, user_id, token):
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        ensure_owner(connection, company_id, section, user_id)
        row = connection.execute("""
            SELECT version, diff_json FROM department_previews
            WHERE token=? AND company_id=? AND section=? AND user_id=? AND expires_at>?
        """, (token, company_id, section, user_id, int(time()))).fetchone()
        if row is None:
            raise api_error(404, 'department_preview_not_found', 'المعاينة مش موجودة أو انتهت. راجع الفرق من جديد.')
        storage.check_version(storage.snapshot(connection, company_id), row['version'])
        if connection.execute("""
            SELECT 1 FROM department_submissions WHERE company_id=? AND section=? AND status='pending'
        """, (company_id, section)).fetchone():
            raise api_error(409, 'department_pending', 'فيه تحديث للقسم مستني اعتماد. استنى قرار الأدمن.')
        sid = connection.execute("""
            INSERT INTO department_submissions
                (company_id, section, user_id, base_version, diff_json, status, submitted_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?)
        """, (company_id, section, user_id, row['version'], row['diff_json'], utc_now())).lastrowid
        connection.execute('DELETE FROM department_previews WHERE token=?', (token,))
        audit(connection, company_id, 'department.submit', 'department_submission', sid,
              {'section': section, 'base_version': row['version']})
        return {'id': sid, 'status': 'pending'}


def merge_submission(connection, company_id, proposal):
    section = proposal['section']
    ensure_owner(connection, company_id, section, proposal['user_id'])
    model = storage.snapshot(connection, company_id)
    graph = model['graph']
    if model['version'] != proposal['base_version']:
        base = store.read(connection, model['id'], proposal['base_version'])
        if records_for(base['graph'], section) != records_for(graph, section):
            raise api_error(409, 'department_version_conflict',
                            'بيانات القسم اتغيّرت بعد المعاينة. ارفض الطلب واطلب معاينة جديدة.')
    records = {record_key(record): record for record in graph.onboarding.records}
    for item in json.loads(proposal['diff_json']):
        record = Record.model_validate(item['after'])
        records[record_key(record)] = record
    if len(records) > 20000:
        raise api_error(422, 'too_many_onboarding_rows', 'الحد الأقصى 20,000 سجل محفوظ للشركة.')
    onboarding = graph.onboarding.model_copy(update={'records': sorted(
        records.values(), key=lambda row: (row.key, row.date is not None, row.date),
    )})
    version = model['version'] + 1
    store.write_version(connection, model['id'], version, ModelInput(
        name=model['name'], graph=materialize(graph.model_copy(update={'onboarding': onboarding})),
    ))
    return version


def decide(connection, company_id, submission_id, decision, reviewer_id, comment):
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        row = connection.execute('SELECT * FROM department_submissions WHERE id=? AND company_id=?',
                                 (submission_id, company_id)).fetchone()
        if row is None:
            raise api_error(404, 'department_submission_not_found', 'طلب التحديث مش موجود.')
        if row['status'] != 'pending':
            raise api_error(409, 'department_already_decided', 'الطلب ده اتراجع أو اتلغى بالفعل.')
        version = merge_submission(connection, company_id, row) if decision == 'approved' else None
        connection.execute("""
            UPDATE department_submissions SET status=?, reviewer_id=?, decided_at=?, comment=?, approved_version=?
            WHERE id=?
        """, (decision, reviewer_id, utc_now(), comment, version, submission_id))
        audit(connection, company_id, f'department.{decision}', 'department_submission', submission_id,
              {'section': row['section'], 'base_version': row['base_version'], 'version': version})
        return {'id': submission_id, 'status': decision, 'approved_version': version}
