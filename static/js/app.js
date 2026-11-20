const pages = [
    ['map', 'خريطة الشركة', 'overview'],
    ['dashboard', 'لوحة التحكم', 'overview'],
    ['data', 'البيانات', 'inputs'],
    ['model', 'نموذج الشركة', 'inputs'],
    ['onboarding', 'تجهيز الشركة', 'inputs'],
    ['enterprise', 'النموذج المؤسسي', 'inputs'],
    ['connectors', 'ربط البيانات', 'inputs'],
    ['forecast', 'التوقعات', 'analysis'],
    ['optimize', 'التحسين', 'analysis'],
    ['whatif', 'ماذا لو', 'analysis'],
    ['impact', 'أثر الفرضية', 'analysis'],
    ['scenarios', 'مقارنة السيناريوهات', 'analysis'],
    ['prescriptive', 'القرارات والمتابعة', 'analysis'],
    ['chat', 'المساعد الذكي', 'analysis'],
    ['approvals', 'الاعتمادات والتصدير', 'delivery'],
    ['actuals', 'الفعلي مقابل المخطط', 'delivery'],
];

const onboardingSectors = new Set(['trading', 'retail', 'services', 'custom']);
const onboardingPages = new Set(['map', 'onboarding', 'enterprise', 'connectors', 'impact', 'scenarios', 'prescriptive']);

const locale = 'ar-SA-u-ca-gregory-nu-latn';
const formatter = new Intl.NumberFormat(locale, { maximumFractionDigits: 2 });
const percentFormatter = new Intl.NumberFormat(locale, { style: 'percent', maximumFractionDigits: 1 });
const dateFormatter = new Intl.DateTimeFormat(locale, { dateStyle: 'medium', timeStyle: 'short' });
const number = value => typeof value === 'number' ? formatter.format(value) : value ?? '—';
const signed = value => typeof value === 'number' && value > 0 ? '+' + number(value) : number(value);
const percent = value => value == null ? '—' : percentFormatter.format(value);
const timestamp = value => value ? dateFormatter.format(new Date(value)) : '—';
const post = body => ({
    method: 'POST',
    ...(body === undefined ? {} : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
});

let csrfToken;

async function request(path, options = {}) {
    try {
        const headers = new Headers(options.headers);
        if (csrfToken && ((options.method && options.method !== 'GET') || path.includes('/export.'))) {
            headers.set('X-CSRF-Token', csrfToken);
        }
        const response = await fetch('/api' + path, { ...options, headers });
        if (response.status === 401) location.replace('/login' + location.hash);
        return response;
    } catch {
        throw new Error('تعذّر الاتصال بالخادم. حاول مرة تانية.');
    }
}

async function api(path, options) {
    const response = await request(path, options);
    const body = await response.json().catch(() => null);
    if (!response.ok && !body?.validation) {
        throw new Error(body?.detail?.msg_ar || 'تعذّر تنفيذ الطلب. حاول مرة تانية.');
    }
    return body;
}

const latestRun = (base, kind = 'optimize') => api(base + '/runs/latest?allow_missing=true&kind=' + kind);
const solverLabel = status => ({
    OPTIMAL: 'حل أمثل', FEASIBLE: 'حل قابل للتنفيذ', INFEASIBLE: 'مفيش حل ممكن',
    MODEL_INVALID: 'النموذج غير صالح', UNKNOWN: 'مفيش حل في الوقت المتاح',
}[status] ?? 'حالة غير معروفة');

const metricDefinitions = {
    total: 'إجمالي التكلفة', total_cost: 'إجمالي التكلفة', ld: 'غرامات التأخير',
    interest: 'تكلفة التمويل', peak_od: 'ذروة العجز النقدي', breach: 'تجاوز حد الائتمان',
    late_weeks_total: 'أسابيع التأخير', makespan: 'مدة الخطة بالأسابيع',
    lateness_cost: 'تكلفة التأخير', late_orders: 'الطلبات المتأخرة', late_days_total: 'أيام التأخير',
    unshipped: 'الطلبات غير المشحونة', unshipped_cost: 'تكلفة عدم الشحن', holding_cost: 'تكلفة التخزين',
    changeovers: 'مرات تبديل المنتج', changeover_cost: 'تكلفة التبديل', peak_storage_pct: 'ذروة إشغال التخزين',
    revenue: 'الإيرادات', margin: 'الهامش',
};

function metricRows(left, right, template) {
    const cashKeys = new Set(['total', 'total_cost', 'ld', 'interest', 'peak_od', 'breach', 'lateness_cost',
        'unshipped_cost', 'holding_cost', 'changeover_cost', 'revenue', 'margin']);
    const unit = template === 'contracting' ? 'ألف ريال' : 'ريال';
    const row = (key, label, unit, before, after) => ({
        key, label, unit, before, after,
        delta: after == null || before == null ? null : after - before,
    });
    const rows = Object.entries(metricDefinitions).filter(([key]) => key in (left || {}) || key in (right || {}))
        .map(([key, label]) => row(key, label, cashKeys.has(key) ? unit : key === 'peak_storage_pct' ? '%' : '',
            left?.[key], right?.[key]));
    const utilKey = template === 'contracting' ? 'resource_util' : 'line_util';
    const utilLabel = template === 'contracting' ? 'استغلال الفريق' : 'استغلال الخط';
    const utilizationIds = new Set([...Object.keys(left?.[utilKey] || {}), ...Object.keys(right?.[utilKey] || {})]);
    for (const id of utilizationIds) {
        rows.push(row(utilKey + '.' + id, utilLabel + ' ' + id, '%', left?.[utilKey]?.[id], right?.[utilKey]?.[id]));
    }
    return rows;
}

// Reload a page on entry. A response from a previous company or visit cannot update the current page.
function workspacePage(name, state, methods) {
    let visit = 0;
    let selection = '';
    return Object.defineProperties({
        ...state(),
        busy: '',
        message: '',
        error: '',
        number, signed, percent, timestamp, solverLabel,
        get template() { return this.$store.app.template; },
        activate(page, companyId) {
            const key = page === name && companyId ? String(companyId) : '';
            if (key === selection) return;
            selection = key;
            const current = ++visit;
            if (!key) return;
            queueMicrotask(() => {
                if (current !== visit) return;
                Object.assign(this, state(), { busy: '', message: '', error: '' });
                this.reset?.();
                this.perform('load', this.load);
            });
        },
        async perform(action, work) {
            if (this.busy || !selection) return;
            const current = visit;
            const id = this.$store.app.companyId;
            const live = () => visit === current && this.$store.app.companyId === id && this.$store.app.page === name;
            this.busy = action;
            this.error = this.message = '';
            try {
                await work.call(this, '/companies/' + id, live);
            } catch (error) {
                if (live()) {
                    this.error = error.message;
                    this.$store.app.notify(error.message);
                }
            } finally {
                if (live()) this.busy = '';
            }
        },
        sendToReview(base, runId, variant, live) {
            return api(base + '/plans', post({ run_id: runId, variant })).then(plan => {
                if (live()) this.message = 'تم حفظ مسودة الخطة رقم ' + number(plan.id) + '.';
            });
        },
    }, Object.getOwnPropertyDescriptors(methods));
}

document.addEventListener('alpine:init', () => {
    let toastTimer;
    Alpine.store('app', {
        pages, number,
        page: 'map',
        user: null,
        companies: [],
        templates: {},
        companyId: null,
        loading: true,
        theme: localStorage.getItem('decision-theme') === 'dark' ? 'dark' : 'light',
        lastRun: null,
        forecast: null,
        plans: [],
        chatScenario: null,
        enterpriseChatImpact: null,
        onboardingImpactModel: null,
        error: '',
        toast: '',
        get canApprove() { return ['admin', 'approver'].includes(this.user?.role); },
        get company() { return this.companies.find(company => company.id === this.companyId); },
        get template() { return this.company?.template; },
        get isOnboardingCompany() { return onboardingSectors.has(this.template); },
        get visiblePages() {
            return this.isOnboardingCompany ? this.pages.filter(([id]) => onboardingPages.has(id)) : this.pages;
        },
        async init() {
            this.setTheme(this.theme);
            this.readHash();
            window.addEventListener('hashchange', () => this.readHash());
            try {
                this.user = await api('/auth/me');
                csrfToken = this.user.csrf_token;
                await this.loadCompanies();
            } catch (error) {
                this.error = error.message;
                this.loading = false;
            }
        },
        async logout() {
            try {
                const response = await request('/auth/logout', post());
                if (!response.ok) throw new Error((await response.json()).detail.msg_ar);
                location.replace('/login');
            } catch (error) {
                this.notify(error.message);
            }
        },
        async loadCompanies(preferredId = null) {
            this.loading = true;
            this.error = '';
            try {
                const [companies, templates] = await Promise.all([api('/companies'), api('/templates')]);
                this.templates = Object.fromEntries(templates.map(item => [item.template_id, item]));
                this.companies = companies;
                const selected = companies.find(company => company.id === preferredId);
                this.switchCompany(selected?.id ?? companies[0]?.id ?? null);
            } catch (error) {
                this.error = error.message;
                this.notify(error.message);
            } finally {
                this.loading = false;
            }
        },
        switchCompany(id) {
            this.lastRun = this.forecast = this.chatScenario = null;
            this.enterpriseChatImpact = this.onboardingImpactModel = null;
            this.plans = [];
            this.toast = '';
            const changed = this.companyId !== id;
            this.companyId = id;
            if (changed && this.isOnboardingCompany && this.page !== 'map') {
                this.page = 'onboarding';
                location.hash = '#/onboarding';
            }
        },
        notify(message) {
            this.toast = message;
            clearTimeout(toastTimer);
            toastTimer = setTimeout(() => { this.toast = ''; }, 6000);
        },
        readHash() {
            const target = location.hash.slice(2);
            const page = pages.some(([id]) => id === target) ? target : 'map';
            this.page = this.isOnboardingCompany && !onboardingPages.has(page) ? 'onboarding' : page;
        },
        setTheme(theme) {
            this.theme = theme;
            document.documentElement.dataset.theme = theme;
            localStorage.setItem('decision-theme', theme);
            recolorCharts();
        },
        toggleTheme() { this.setTheme(this.theme === 'light' ? 'dark' : 'light'); },
    });
});
