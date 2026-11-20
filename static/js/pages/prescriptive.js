const phase18Labels = {
    profit: 'الربح بعد التمويل', cash_low: 'أقل رصيد سيولة', credit_breach: 'احتمال تجاوز الائتمان',
    delays: 'إجمالي تأخير المشروعات', fill_rate: 'تلبية الطلبات',
    duration_weeks: 'مدة الأنشطة (أسبوع)', cost: 'التكلفة (ألف ريال)',
    qty: 'الكمية المشحونة (وحدة)', late_days: 'التأخير (يوم)',
    cash_minimum: 'الحد الأدنى للسيولة (ريال)', credit_maximum: 'الحد الأقصى لاحتمال التجاوز (%)',
    delays_maximum: 'الحد الأقصى للتأخير (أسبوع)',
};

const phase18Reasons = {
    engine: 'العلاقة محسوبة بمحرك النموذج',
    missing_history: 'مفيش تاريخ للطرفين',
    mixed_frequency: 'تواتر السلاسل مختلف',
    zero_reference: 'خط أساس صفري للمرونة',
    multiple_inputs: 'العلاقة ليها مدخلات متزامنة',
    insufficient_history: 'البيانات المتطابقة قليلة',
    insufficient_variation: 'التغير في التاريخ غير كافٍ',
    weak_holdout: 'دقة الاختبار أقل من الحد المطلوب',
};

const phase18Tabs = [
    ['goal', 'البحث عن هدف'], ['calibration', 'المعايرة'],
    ['monitor', 'متابعة المؤشرات'], ['log', 'سجل القرارات'],
];
const phase18Put = body => ({ ...post(body), method: 'PUT' });
const phase18Value = value => value === '' || value == null ? null : Number(value);

document.addEventListener('alpine:init', () => {
    const lastTab = new Map();
    Alpine.data('prescriptivePage', () => workspacePage('prescriptive', () => ({
        models: [], modelId: '', setup: null, activeTab: 'goal', currentVersion: null,
        goalText: '', goalForm: { profit_pct: 10, cash_minimum: 0, controls: [] },
        recommendation: null, draftName: '', calibration: [], monitoring: null, thresholdForm: {},
        uncertainty: [], log: null, sectorLog: [], reviewed: {}, actualForms: {},
    }), {
        markup: '', tabs: phase18Tabs, labels: phase18Labels, reasons: phase18Reasons,
        thresholdFields: ['cash_minimum', 'credit_maximum', 'delays_maximum'],
        get editAllowed() { return ['admin', 'planner'].includes(this.$store.app.user?.role); },
        get endpoint() { return '/enterprise/models/' + this.modelId + '/prescriptive'; },
        async load(base, live) {
            const [markup, models] = await Promise.all([
                this.markup || fetch('/static/partials/prescriptive.html').then(response => {
                    if (!response.ok) throw new Error('تعذّر تحميل صفحة القرارات.');
                    return response.text();
                }),
                api('/enterprise/models'),
            ]);
            if (!live()) return;
            this.markup = markup;
            this.activeTab = lastTab.get(this.$store.app.companyId) || 'goal';
            this.models = models.filter(model => model.company_id === this.$store.app.companyId);
            this.modelId = String(this.models[0]?.id ?? '');
            await this.selectModel(base, live);
        },
        async selectModel(base, live) {
            this.setup = this.recommendation = this.monitoring = this.log = null;
            this.calibration = [];
            this.reviewed = this.actualForms = {};
            if (this.modelId) {
                const setup = await api(this.endpoint + '/setup');
                if (!live()) return;
                this.setup = setup;
                this.currentVersion = setup.version;
                this.calibration = setup.calibration || [];
                this.thresholdForm = { ...setup.thresholds };
                this.uncertainty = Object.entries(setup.thresholds.driver_uncertainty)
                    .map(([node_id, spread]) => ({ node_id, spread }));
                this.goalForm.controls = [];
                this.addControl();
            }
            await this.loadTab(base, live);
        },
        async loadTab(base, live) {
            if (this.activeTab === 'monitor' && this.modelId) {
                const result = await api(this.endpoint + '/monitor');
                if (live()) this.monitoring = result;
            }
            if (this.activeTab === 'log') {
                const [log, sectorLog] = await Promise.all([
                    this.modelId ? api(this.endpoint + '/decisions') : null,
                    api(base + '/decision-log'),
                ]);
                if (!live()) return;
                this.log = log;
                this.sectorLog = sectorLog;
            }
        },
        switchTab(tab) {
            if (this.busy) return;
            this.activeTab = tab;
            lastTab.set(this.$store.app.companyId, tab);
            this.perform('tab', this.loadTab);
        },
        async refresh(base, live) {
            if (this.activeTab === 'monitor' && this.modelId) {
                const setup = await api(this.endpoint + '/setup');
                if (!live()) return;
                if (setup.version !== this.currentVersion) {
                    await this.selectModel(base, live);
                    return;
                }
            }
            await this.loadTab(base, live);
        },
        addControl() {
            const chosen = this.goalForm.controls.map(item => item.node_id);
            const driver = this.setup?.controls.find(item => !chosen.includes(item.id));
            if (!driver || chosen.length >= 3) return;
            const control = { node_id: driver.id, minimum: '', maximum: '' };
            this.goalForm.controls.push(control);
        },
        referenceFor(id) { return this.setup?.controls.find(item => item.id === id)?.value; },
        async parseGoal(_, live) {
            const target = await api(this.endpoint + '/goal-input', post({ message: this.goalText }));
            if (live()) {
                Object.assign(this.goalForm, target);
                this.recommendation = null;
            }
        },
        async search(_, live) {
            this.recommendation = null;
            const goal = {
                version: this.currentVersion,
                profit_pct: phase18Value(this.goalForm.profit_pct), cash_minimum: phase18Value(this.goalForm.cash_minimum),
                controls: this.goalForm.controls.map(control => ({
                    node_id: control.node_id, minimum: phase18Value(control.minimum), maximum: phase18Value(control.maximum),
                })),
            };
            const result = await api(this.endpoint + '/goal-seek', post(goal));
            if (live()) this.recommendation = { ...result, goal };
        },
        async draft(row, live) {
            const result = await api(this.endpoint + '/decisions', post({
                goal: this.recommendation.goal, name: this.draftName, actions: row.actions,
            }));
            if (live()) {
                this.message = 'اتحفظت مسودة القرار رقم ' + number(result.id) + '.';
                this.draftName = '';
            }
        },
        async preview(_, live) {
            const result = await api(this.endpoint + '/calibration');
            if (live()) this.calibration = result.edges;
        },
        async apply(base, live) {
            const result = await api(this.endpoint + '/calibration', post({ version: this.currentVersion }));
            if (!live()) return;
            await this.selectModel(base, live);
            if (live()) this.message = 'اتحفظت المعايرة على الإصدار ' + number(result.version) + '.';
        },
        async saveThresholds(_, live) {
            const body = Object.fromEntries(this.thresholdFields.map(key => [key, phase18Value(this.thresholdForm[key])]));
            const entries = this.uncertainty.map(row => [row.node_id, phase18Value(row.spread)]);
            if (new Set(entries.map(([key]) => key)).size !== entries.length) {
                throw new Error('اختار مدخل مختلف لكل قيمة عدم يقين.');
            }
            body.driver_uncertainty = Object.fromEntries(entries);
            const result = await api(this.endpoint + '/thresholds', phase18Put(body));
            if (live()) {
                this.monitoring = result;
                this.message = 'اتحفظت الحدود واتحدّثت التنبيهات.';
            }
        },
        async decide(id, decision, base, live) {
            await api(this.endpoint + '/decisions/' + id + '/decide',
                post({ decision, comment: this.reviewed[id] || '' }));
            if (!live()) return;
            await this.loadTab(base, live);
            if (live()) this.message = 'اتسجلت المراجعة.';
        },
        startActuals(item) {
            this.actualForms[item.id] = { observed_on: item.actuals?.observed_on || '', metrics: { ...item.actuals?.metrics } };
        },
        async saveActuals(id, base, live) {
            const form = this.actualForms[id];
            const metrics = Object.fromEntries(Object.entries(form.metrics).filter(([, value]) => phase18Value(value) !== null)
                .map(([key, value]) => [key, Number(value)]));
            await api(this.endpoint + '/decisions/' + id + '/actuals', post({ observed_on: form.observed_on, metrics }));
            if (!live()) return;
            delete this.actualForms[id];
            await this.loadTab(base, live);
            if (live()) this.message = 'اتسجلت القيم الفعلية واتحدّث تقرير الدقة.';
        },
        decisionLabel(status) { return { draft: 'مسودة', approved: 'معتمد', rejected: 'مرفوض' }[status]; },
    }));
});
