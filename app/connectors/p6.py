from collections import Counter
from xml.etree.ElementTree import ParseError

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring

from app.connectors.mapping import report
from app.ingestion.validators import convert
from app.schemas import FieldError, api_error


def field(element, *names):
    for name in names:
        value = (element.findtext(name) or '').strip()
        if value:
            return value
    return None


def read_p6(contents):
    try:
        root = fromstring(contents, forbid_dtd=True)
    except (ParseError, DefusedXmlException, ValueError, LookupError):
        raise api_error(422, 'invalid_p6_xml', 'تعذّرت قراءة ملف Primavera P6 XML.') from None
    for element in root.iter():
        element.tag = element.tag.rsplit('}', 1)[-1]
    if root.tag != 'APIBusinessObjects' or not root.findall('Project'):
        raise api_error(422, 'invalid_p6_root', 'اختار ملف Primavera P6 XML فيه مشروع واحد على الأقل.')

    result = {'projects': [], 'wbs': [], 'activities': [], 'relationships': []}
    errors = []
    for tag, key in [('Project', 'projects'), ('WBS', 'wbs'), ('Activity', 'activities'), ('Relationship', 'relationships')]:
        elements = [(element, None) for element in root.findall(tag)]
        if tag != 'Project':
            elements += [(element, field(project, 'ObjectId')) for project in root.findall('Project')
                         for element in project.findall(tag)]
        for row, (element, owner) in enumerate(elements, 1):
            project_id = field(element, 'ProjectObjectId') or owner
            object_id = field(element, 'ObjectId')
            record = {'object_id': object_id, 'id': field(element, 'Id'), 'name': field(element, 'Name')}
            if tag == 'WBS':
                record.update(project_object_id=project_id, code=field(element, 'Code'),
                              parent_object_id=field(element, 'ParentObjectId'))
            if tag == 'Activity':
                record.update(project_object_id=project_id, wbs_object_id=field(element, 'WBSObjectId'),
                              start=field(element, 'PlannedStartDate', 'StartDate'),
                              finish=field(element, 'PlannedFinishDate', 'FinishDate'))
                for aliases, target, required in [
                    (('PlannedDuration', 'OriginalDuration', 'OriginalDurationHours'), 'duration_hours', True),
                    (('PlannedTotalCost', 'BudgetedTotalCost'), 'planned_cost', False),
                    (('ActualTotalCost',), 'actual_cost', False),
                ]:
                    record[target] = decimal_field(element, aliases, tag, row, errors, required=required)
            if tag == 'Relationship':
                record = {
                    'object_id': object_id, 'project_object_id': project_id,
                    'predecessor_object_id': field(element, 'PredecessorActivityObjectId'),
                    'successor_object_id': field(element, 'SuccessorActivityObjectId'),
                    'predecessor_project_object_id': field(element, 'PredecessorProjectObjectId'),
                    'successor_project_object_id': field(element, 'SuccessorProjectObjectId'),
                    'type': field(element, 'RelationshipType', 'Type'),
                    'lag_hours': decimal_field(element, ('Lag',), tag, row, errors),
                }
            result[key].append(record)

    errors.extend(check_references(result))
    counts = {key: len(rows) for key, rows in result.items()}
    return result, report(errors).model_copy(update={'summary': counts})


def decimal_field(element, names, sheet, row, errors, *, required=False):
    raw = field(element, *names)
    if raw is None and not required:
        return None
    try:
        value = convert(raw, 'float')
        if required and value < 0:
            raise ValueError('أدخل مدة صفر أو أكبر.')
        return value
    except ValueError as error:
        errors.append(FieldError(sheet=sheet, row=row, column=names[0], msg_ar=str(error)))
        return None


def check_references(data):
    errors = []

    def issue(sheet, row, column, message):
        errors.append(FieldError(sheet=sheet, row=row, column=column, msg_ar=message))

    for key in ('projects', 'wbs', 'activities'):
        if key != 'wbs' and not data[key]:
            issue(key, 0, '', 'الملف مفيهوش سجلات من النوع ده.')
        counts = Counter(record['object_id'] for record in data[key])
        for row, record in enumerate(data[key], 1):
            if not record['object_id'] or counts[record['object_id']] > 1:
                issue(key, row, 'ObjectId', 'المعرّف مطلوب ولازم يكون فريد.')
            if key != 'wbs' and not record['id']:
                issue(key, row, 'Id', 'كود السجل مطلوب.')

    projects = {item['object_id'] for item in data['projects']}
    wbs = {item['object_id']: item['project_object_id'] for item in data['wbs']}
    activities = {item['object_id']: item['project_object_id'] for item in data['activities']}
    for key in ('wbs', 'activities'):
        for row, record in enumerate(data[key], 1):
            owner = record['project_object_id']
            if owner not in projects:
                issue(key, row, 'ProjectObjectId', 'المشروع المرتبط مش موجود في الملف.')
            wbs_id = record.get('wbs_object_id') or record.get('parent_object_id')
            if wbs_id and wbs.get(wbs_id) != owner:
                issue(key, row, 'WBSObjectId', 'بند هيكل الأعمال مش موجود في نفس المشروع.')
    for row, relationship in enumerate(data['relationships'], 1):
        if not relationship['type']:
            issue('relationships', row, 'RelationshipType', 'نوع العلاقة مطلوب.')
        left = relationship['predecessor_object_id']
        right = relationship['successor_object_id']
        if left and left == right:
            issue('relationships', row, '', 'النشاط مينفعش يرتبط بنفسه.')
        for side in ('predecessor', 'successor'):
            activity_id = relationship[f'{side}_object_id']
            project_id = relationship[f'{side}_project_object_id']
            if not activity_id or (activity_id not in activities and (not project_id or project_id in projects)):
                issue('relationships', row, side, 'النشاط المرتبط مش موجود ومفيش مرجع لمشروع خارجي.')
            elif activity_id in activities and project_id and activities[activity_id] != project_id:
                issue('relationships', row, side, 'مرجع المشروع مختلف عن مشروع النشاط.')
    return errors
