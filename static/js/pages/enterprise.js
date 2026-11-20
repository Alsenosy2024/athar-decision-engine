const enterpriseTypes = {
    sales: 'المبيعات', demand: 'الطلب', pricing: 'التسعير', production_line: 'خطوط الإنتاج',
    inventory: 'المخزون', storage: 'التخزين', procurement: 'المشتريات', supplier: 'الموردون',
    workforce: 'فرق العمل', project: 'المشروعات', cash: 'السيولة', receivables: 'مستحقات العملاء',
    payables: 'مستحقات الموردين', financing: 'التمويل', credit: 'الائتمان',
    profit_loss: 'الأرباح والخسائر', risk_kpi: 'المخاطر',
};
const enterpriseUnits = {
    SAR: 'ر.س', 'SAR/unit': 'ر.س/وحدة', units: 'وحدة', storage_units: 'وحدة تخزين',
    teams: 'فرق', days: 'يوم', weeks: 'أسبوع', months: 'شهر', '%': '%',
};
const enterpriseEngines = {
    forecasting: 'محرك التوقعات',
    contracting_optimizer: 'محرك تحسين المقاولات',
    production_optimizer: 'محرك تحسين الإنتاج',
};

document.addEventListener('alpine:init', () => {
    Alpine.data('enterprisePage', () => ({
        models: [],
        model: null,
        selected: '',
        nodeNames: {},
        busy: false,
        error: '',
        message: '',
        types: enterpriseTypes,
        units: enterpriseUnits,
        engines: enterpriseEngines,
        number, timestamp,
        get graph() { return this.model?.graph; },
        get node() { return this.graph?.nodes.find(node => node.id === this.selected); },
        get groups() {
            return Object.entries(this.types).map(([id, title]) => ({
                id, title, nodes: this.graph?.nodes.filter(node => node.type === id) || [],
            })).filter(group => group.nodes.length);
        },
        get incoming() { return this.graph?.edges.filter(edge => edge.target === this.selected) || []; },
        get outgoing() { return this.graph?.edges.filter(edge => edge.source === this.selected) || []; },
        name(id) { return this.nodeNames[id] || id; },
        setModel(model) {
            this.nodeNames = Object.fromEntries(model.graph.nodes.map(node => [node.id, node.name_ar]));
            this.model = model;
            this.selected = model.graph.nodes.find(node => node.type === 'cash')?.id ?? model.graph.nodes[0].id;
        },
        async perform(work) {
            if (this.busy) return;
            this.busy = true;
            this.error = this.message = '';
            try {
                await work.call(this);
            } catch (error) {
                this.error = error.message;
            } finally {
                this.busy = false;
            }
        },
        load() {
            return this.perform(async function () {
                this.models = await api('/enterprise/models');
                const app = this.$store.app;
                const companyModel = app.isOnboardingCompany && this.models.find(model => model.company_id === app.companyId);
                const id = companyModel?.id ?? this.models.find(model => model.id === this.model?.id)?.id ?? this.models[0]?.id;
                const url = id ? '/enterprise/models/' + id : '/enterprise/templates/enterprise';
                this.setModel(await api(url));
            });
        },
        choose(id) {
            return this.perform(async function () {
                this.setModel(await api('/enterprise/models/' + id));
            });
        },
        saveDemo() {
            return this.perform(async function () {
                const company = this.$store.app.companyId;
                const model = await api('/enterprise/models/demo' + (company ? '?company_id=' + company : ''), post());
                this.models.push(model);
                this.setModel(model);
                this.message = 'اتحفظ الديمو كإصدار أول.';
            });
        },
        follow(id, button) {
            this.selected = id;
            const focus = button.closest('.enterprise-graph').querySelector('.enterprise-focus');
            Alpine.nextTick(() => focus.focus());
        },
    }));
});
