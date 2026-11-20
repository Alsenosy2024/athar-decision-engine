from copy import deepcopy
from decimal import Decimal

from app.llm.schema import ChatContext, validate_changes
from app.schemas import api_error
from app.templates_registry import load_templates


def format_num_ar(value, signed=False):
    return format(value, '+,' if signed else ',')


def scaled(value, percent, minimum=0):
    return max(minimum, round(Decimal(value) * (1 + Decimal(str(percent)) / 100)))


def invalid_change(message):
    return api_error(422, 'invalid_change', message)


def apply_changes(dataset, model, changes, *, template_id):
    """Apply changes in request order to independent copies of the base inputs."""
    validate_changes(ChatContext(template_id, dataset), changes)
    template = load_templates()[template_id]
    dataset, model = deepcopy(dataset), deepcopy(model)
    params, weights = model['parameters'], model['weights']
    horizon = params[template.horizon_key]
    credit_unit = 'ألف ريال' if template_id == 'contracting' else 'ريال'
    descriptions = []
    indexed = {
        table: {row['id']: row for row in dataset.get(table, [])}
        for table in ('resources', 'projects', 'orders')
    }
    for change in changes:
        match change.type:
            case 'credit_limit_pct' | 'credit_limit_abs':
                before = params['credit_limit']
                after = scaled(before, change.value) if change.type == 'credit_limit_pct' else change.value
                params['credit_limit'] = after
                description = f'تغيير الحد الائتماني من {before:,} إلى {after:,} {credit_unit}'
            case 'crew_unavailable':
                resource = indexed['resources'][change.resource]
                if not change.from_week <= change.to_week < horizon:
                    raise invalid_change('فترة غياب الفريق خارج أفق التخطيط أو ترتيب الأسابيع غير صالح.')
                overrides = params['capacity_override'].setdefault(change.resource, {})
                for week in range(change.from_week, change.to_week + 1):
                    before = overrides.get(str(week), resource['capacity'])
                    overrides[str(week)] = max(0, before - change.units)
                description = (f'خفض سعة {resource["name_ar"]} بمقدار {change.units:,} '
                               f'من الأسبوع {change.from_week} إلى {change.to_week}')
            case 'deadline_shift' | 'project_start_delay':
                project = indexed['projects'][change.project]
                deadline = change.type == 'deadline_shift'
                key = 'deadline_week' if deadline else 'nta_week'
                before = project[key]
                after = before + change.weeks
                if after < 0:
                    raise invalid_change('الأسبوع الناتج لتسليم المشروع لازم يكون غير سالب.')
                project[key] = after
                label = 'موعد تسليم' if deadline else 'أقرب موعد لبدء'
                description = f'تعديل {label} {project["name_ar"]} من الأسبوع {before} إلى {after}'
            case 'ld_change':
                project = indexed['projects'][change.project]
                project['ld_per_week'] = change.value
                description = f'ضبط غرامة {project["name_ar"]} على {change.value:,} ألف ريال لكل أسبوع'
            case 'payment_lag':
                contracting = template_id == 'contracting'
                key = 'payment_lag_weeks' if contracting else 'payment_lag_days'
                unit = 'أسبوع' if contracting else 'يوم'
                before = params[key]
                after = before + change.value if change.relative else change.value
                if not 0 <= after <= 10000:
                    raise invalid_change('مهلة التحصيل خارج النطاق المدعوم.')
                params[key] = after
                description = f'تغيير مهلة التحصيل من {before:,} إلى {after:,} {unit}'
            case 'margin_pct':
                params['margin'] = float(1 + Decimal(str(change.value)) / 100)
                description = f'ضبط هامش الإيراد على {change.value:g}%'
            case 'demand_pct':
                orders = [row for row in dataset['orders'] if change.sku in (None, row['sku_id'])]
                if not orders:
                    raise invalid_change('مفيش طلبات ينطبق عليها تغيير الطلب.')
                # Zero demand removes the order rather than introducing a zero-quantity shipment.
                for row in orders:
                    row['qty'] = scaled(row['qty'], change.value)
                dataset['orders'] = [row for row in dataset['orders'] if row['qty'] > 0]
                scope = f'للمنتج {change.sku}' if change.sku else 'لكل المنتجات'
                description = f'تعديل الطلب {scope} بنسبة {change.value:+g}%'
            case 'line_down':
                last = change.from_day + change.days - 1
                if last >= horizon:
                    raise invalid_change('أيام توقف الخط خارج أفق التخطيط.')
                days = params['line_down'].get(change.line, [])
                params['line_down'][change.line] = sorted(set(days) | set(range(change.from_day, last + 1)))
                description = f'إيقاف الخط {change.line} من اليوم {change.from_day} إلى {last}'
            case 'storage_pct':
                before = params['cold_capacity']
                after = scaled(before, change.value)
                params['cold_capacity'] = after
                description = f'تغيير سعة التخزين المبرد من {before:,} إلى {after:,}'
            case 'order_due_shift':
                order = indexed['orders'][change.order]
                before = order['due_day']
                after = before + change.days
                if not 0 <= after < horizon:
                    raise invalid_change('موعد تسليم الطلب خارج أفق التخطيط.')
                if not order['qty']:
                    raise invalid_change('الطلب اتحذف ضمن تغييرات السيناريو.')
                order['due_day'] = after
                description = f'تغيير موعد تسليم {change.order} من اليوم {before} إلى {after}'
            case 'changeover_cost_pct':
                for line in dataset['lines']:
                    line['changeover_cost'] = scaled(line['changeover_cost'], change.value)
                description = f'تعديل تكلفة تبديل المنتجات بنسبة {change.value:+g}%'
            case 'weight_change':
                spec = next(spec for spec in template.objective_weights if spec.id == change.name)
                before = weights[change.name]
                after = before * change.value if change.relative else change.value
                if not spec.min <= after <= spec.max:
                    raise invalid_change(f'وزن «{spec.name_ar}» خارج النطاق المسموح.')
                weights[change.name] = after
                description = f'تغيير وزن {spec.name_ar} من {before:g} إلى {after:g}'
        descriptions.append(description)
    return dataset, model, descriptions


def kpi_diff(base, scenario):
    diffs = {}
    for key, before in base.items():
        after = scenario.get(key)
        if type(before) in (int, float) and type(after) in (int, float):
            delta = round(after - before, 6)
            diffs[key] = {
                'base': before, 'scenario': after, 'delta': delta,
                'delta_pct': round(delta / before * 100, 2) if before else None,
            }
    return diffs
