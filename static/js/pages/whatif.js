const entityTables = { project: 'projects', resource: 'resources', line: 'lines', order: 'orders', sku: 'skus' };
const entityLabels = { project: 'المشروع', resource: 'الفريق', line: 'خط الإنتاج', order: 'الطلب', sku: 'المنتج', name: 'الهدف' };
const qty = (key, label, value, min = 0, max = 1e12, step = 1) => ({ key, label, value, min, max, step });
const entity = (key, optional = false) => ({ key, label: entityLabels[key], entity: true, optional });
const pctField = value => qty('value', 'نسبة التغيير (%)', value, -100, 100000, 'any');
const shift = (key, label) => qty(key, label, 2, -10000, 10000);
const changeTypes = {
    credit_limit_pct: ['تغيير حد الائتمان بنسبة', pctField(-10)],
    credit_limit_abs: ['تعيين حد الائتمان', qty('value', 'الحد الجديد بالوحدة المالية للقطاع', 3500)],
    crew_unavailable: ['غياب فريق', entity('resource'), qty('from', 'من أسبوع', 5, 0, 10000),
        qty('to', 'لحد أسبوع (شامل)', 6, 0, 10000), qty('units', 'عدد الفرق الغائبة', 1, 1)],
    deadline_shift: ['تغيير موعد التسليم', entity('project'), shift('weeks', 'الإزاحة بالأسابيع')],
    ld_change: ['تعيين غرامة التأخير', entity('project'), qty('value', 'الغرامة لكل أسبوع (ألف ريال)', 30)],
    project_start_delay: ['تأجيل بدء مشروع', entity('project'), qty('weeks', 'عدد الأسابيع', 2, 1, 10000)],
    payment_lag: ['تغيير مهلة التحصيل', qty('value', 'المهلة (أسابيع للمقاولات، أيام للإنتاج)', 6, 0, 10000)],
    margin_pct: ['تعيين هامش المقاولات', qty('value', 'الهامش الجديد (%)', 10, 0, 10000, 'any')],
    demand_pct: ['تغيير الطلب', pctField(20), entity('sku', true)],
    line_down: ['توقف خط إنتاج', entity('line'), qty('from_day', 'من يوم', 3, 0, 10000),
        qty('days', 'مدة التوقف بالأيام', 2, 1, 10000)],
    storage_pct: ['تغيير سعة التخزين', pctField(-20)],
    order_due_shift: ['تغيير موعد الشحن', entity('order'), shift('days', 'الإزاحة بالأيام')],
    changeover_cost_pct: ['تغيير تكلفة تبديل المنتج', pctField(50)],
    weight_change: ['تعيين وزن هدف', entity('name'), qty('value', 'الوزن الجديد', 2, 0, 5, 'any')],
};

document.addEventListener('alpine:init', () => {
    Alpine.data('whatifPage', () => workspacePage('whatif', () => ({
        baseRun: null, definition: null, dataset: {}, changes: [], result: null, risk: null,
        resultBase: null,
    }), {
        changeTypes, searchStatusLabel,
        get pinnedDemand() { return this.template === 'production' && !!this.baseRun?.result?.decision_contract; },
        get ready() { return !!this.baseRun?.result.optimized?.plan; },
        get presets() {
            const bySector = this.template === 'contracting'
                ? [['crew_unavailable', 'فريق غير متاح']]
                : [['demand_pct', 'الطلب +20%'], ['line_down', 'خط متوقف يومين']];
            return [['credit_limit_pct', 'حد الائتمان −10%'], ...bySector];
        },
        get rows() { return metricRows(this.result?.base?.kpis, this.result?.scenario?.kpis, this.template); },
        fields(type) { return changeTypes[type].slice(1); },
        choices(field) {
            if (field === 'name') return (this.definition?.objective_weights || []).map(row => ({ id: row.id, name_ar: row.name_ar }));
            return this.dataset[entityTables[field]] || [];
        },
        change(type) {
            return { type, ...Object.fromEntries(this.fields(type).map(field => [
                field.key, field.entity ? (field.optional ? '' : this.choices(field.key)[0]?.id ?? '') : field.value,
            ])) };
        },
        async load(base, live) {
            const [run, scenario, risk, definition] = await Promise.all([
                latestRun(base), latestRun(base, 'whatif'), latestRun(base, 'montecarlo'),
                api('/templates/' + this.template),
            ]);
            const dataset = run && await api('/datasets/' + run.dataset_id);
            if (!live()) return;
            this.$store.app.lastRun = this.baseRun = run;
            this.definition = definition;
            this.dataset = dataset?.payload || {};
            const handoff = this.$store.app.chatScenario;
            this.changes = handoff?.changes?.map(item => ({ ...item, ...(item.sku === null ? { sku: '' } : {}) }))
                || [this.change('credit_limit_pct')];
            this.$store.app.chatScenario = null;
            if (scenario) {
                this.result = { run_id: scenario.id, ...scenario.result };
                this.resultBase = scenario.base_run_id;
            }
            if (risk && risk.base_run_id === run?.id) this.setRisk(risk.result, live);
        },
        simulate() {
            const changes = this.changes.map(change => Object.fromEntries(
                Object.entries(change).filter(([key, value]) => key !== 'sku' || value !== ''),
            ));
            this.perform('scenario', async (base, live) => {
                const id = this.baseRun.id;
                const result = await api(base + '/whatif', post({ base_run_id: id, changes }));
                if (live()) {
                    this.result = result;
                    this.resultBase = id;
                }
            });
        },
        monteCarlo() {
            this.perform('mc', async (base, live) => {
                const result = await api(base + '/montecarlo', post({ run_id: this.baseRun.id, n: 200, seed: 42 }));
                if (live()) this.setRisk(result, live);
            });
        },
        setRisk(result, live) {
            this.risk = result;
            this.$nextTick(() => {
                if (live() && !this.pinnedDemand) histogramChart(this.$refs.mc, result);
            });
        },
        askAboutRun(id) { this.$store.app.sectorChatRun = { id, company: this.$store.app.companyId }; },
        draft() {
            if (this.result?.approval_policy || !this.result?.solution_record?.optimized?.eligible) return;
            this.perform('draft', (base, live) => this.sendToReview(base, this.result.run_id, 'scenario', live));
        },
    }));
});
