document.addEventListener('alpine:init', () => {
    let nextId = 0;
    const assumption = () => ({ id: ++nextId, node_id: '', operation: 'pct', value: '', uncertainty: '' });
    Alpine.data('impactPage', () => ({
        models: [], modelId: '', drivers: [], assumptions: [], result: null, version: null,
        selection: '', busy: '', error: '', scenarioName: '', saving: false, savedName: '',
        units: enterpriseUnits,
        number, signed,
        sourceLabel: value => ({actual: 'فعلي', estimated: 'مقدّر', missing: 'ناقص'}[value] || ''),
        get focused() { return this.result?.nodes.find(row => row.id === this.selection); },
        get incoming() { return this.result?.paths.filter(edge => edge.target === this.selection) || []; },
        get outgoing() { return this.result?.paths.filter(edge => edge.source === this.selection) || []; },
        get ranked() {
            return (this.result?.nodes || []).filter(row => row.affected).sort((a, b) =>
                Math.abs(b.delta_pct ?? 0) - Math.abs(a.delta_pct ?? 0)
                || Number(!!b.delta) - Number(!!a.delta));
        },
        get focusOptions() {
            const ranked = this.ranked;
            if (!ranked.length) return this.result?.nodes || [];
            return this.focused && !ranked.some(row => row.id === this.selection) ? [this.focused, ...ranked] : ranked;
        },
        find(id) { return this.result?.nodes.find(row => row.id === id); },
        tone(row) {
            if (row?.delta > 0) return 'impact-up';
            if (row?.delta < 0) return 'impact-down';
            return row?.affected ? 'impact-shift' : '';
        },
        delta(row) {
            if (!row?.delta) return row?.affected && row.delta !== null ? 'تغيّر التوقيت أو نطاق المحاكاة' : '—';
            return signed(row.delta) + ' ' + this.units[row.unit];
        },
        driver(id) { return this.drivers.find(item => item.id === id); },
        add() { this.assumptions.push(assumption()); },
        async load() {
            if (this.busy) return;
            this.busy = 'load';
            const chat = this.$store.app.enterpriseChatImpact;
            this.$store.app.enterpriseChatImpact = null;
            const preferred = this.$store.app.onboardingImpactModel;
            this.$store.app.onboardingImpactModel = null;
            try {
                this.models = await api('/enterprise/models');
                if (chat) {
                    this.modelId = chat.model_id ? String(chat.model_id) : '';
                } else if (preferred || this.$store.app.isOnboardingCompany) {
                    const companyModel = this.models.find(model => model.company_id === this.$store.app.companyId);
                    this.modelId = String(preferred || companyModel?.id || '');
                } else if (!this.models.some(model => String(model.id) === this.modelId)) {
                    this.modelId = this.models[0] ? String(this.models[0].id) : '';
                }
                await this.loadDrivers(chat?.version);
                if (chat) {
                    this.assumptions = chat.scenario.assumptions.map(row => ({ ...row, id: ++nextId }));
                    await this.showResult(chat.result);
                }
            } catch (error) { this.error = error.message; }
            finally { this.busy = ''; }
        },
        async loadDrivers(version) {
            this.result = null;
            this.drivers = [];
            this.error = '';
            this.scenarioName = this.savedName = '';
            this.saving = false;
            this.assumptions = [assumption()];
            const query = new URLSearchParams();
            if (this.modelId) query.set('model_id', this.modelId);
            if (version != null) query.set('version', version);
            const data = await api('/enterprise/whatif/drivers?' + query);
            this.drivers = data.drivers;
            this.version = data.version;
            this.assumptions[0].node_id = this.drivers.find(row => row.id === 'credit')?.id || this.drivers[0]?.id || '';
        },
        async switchModel() {
            if (this.busy) return;
            this.busy = 'load';
            try { await this.loadDrivers(); }
            catch (error) { this.error = error.message; }
            finally { this.busy = ''; }
        },
        scenarioPayload() {
            return {
                assumptions: this.assumptions.map(({ node_id, operation, value, uncertainty }) => ({
                    node_id, operation, value: Number(value), uncertainty: Number(uncertainty),
                })),
                model_id: this.modelId ? Number(this.modelId) : null, version: this.version,
            };
        },
        async saveScenario() {
            if (this.busy) return;
            const name = this.scenarioName.trim();
            this.savedName = '';
            this.error = '';
            if (!name) {
                this.error = 'اكتب اسم للسيناريو.';
                this.$refs.scenarioName.focus();
                return;
            }
            this.busy = 'save';
            try {
                const company = this.$store.app.companyId;
                const scope = !this.modelId && company ? '?company_id=' + company : '';
                const saved = await api('/enterprise/scenarios' + scope, post({ name, ...this.scenarioPayload() }));
                this.savedName = saved.name;
                this.saving = false;
                this.scenarioName = '';
            } catch (error) { this.error = error.message; }
            finally { this.busy = ''; }
        },
        async run() {
            if (this.busy) return;
            this.busy = 'run';
            this.error = '';
            this.result = null;
            try {
                await this.showResult(await api('/enterprise/whatif', post(this.scenarioPayload())));
            } catch (error) { this.error = error.message; }
            finally { this.busy = ''; }
        },
        async showResult(result) {
            this.result = result;
            this.selection = result.assumptions[0].node_id;
            await Alpine.nextTick();
            this.renderCharts();
        },
        renderCharts() {
            const legends = ['قبل الفرضية', 'بعد الفرضية'];
            const { pnl, cash } = this.result.charts;
            if (pnl.length) {
                const labels = {
                    'pnl.revenue': 'الإيراد', 'pnl.direct_cost': 'التكلفة المباشرة',
                    'pnl.expenses': 'مصاريف التشغيل', financing: 'تكلفة التمويل', 'pnl.result': 'النتيجة',
                };
                const peak = Math.max(...pnl.flatMap(row => [Math.abs(row.before), Math.abs(row.after)]));
                const scale = peak >= 1e6 ? 1e6 : peak >= 1e3 ? 1e3 : 1;
                const currency = { 1: 'ريال', 1000: 'ألف ريال', 1000000: 'مليون ريال' }[scale];
                drawChart(this.$refs.pnl, c => ({
                    type: 'bar',
                    data: {
                        labels: pnl.map(row => labels[row.id]),
                        datasets: [
                            { label: legends[0], data: pnl.map(row => row.before), backgroundColor: c.reference },
                            { label: legends[1], data: pnl.map(row => row.after), backgroundColor: c.main },
                        ],
                    },
                    options: {
                        indexAxis: 'y',
                        scales: {
                            x: { reverse: true, title: axisTitle(currency),
                                ticks: { maxTicksLimit: 5, callback: value => number(value / scale) } },
                            y: { position: 'right', grid: { display: false }, ticks: { autoSkip: false } },
                        },
                    },
                }));
            }
            if (cash?.period_ar) {
                drawChart(this.$refs.cash, c => ({
                    type: 'bar',
                    data: { labels: [cash.period_ar], datasets: [
                        { label: legends[0], data: cash.before, backgroundColor: c.reference },
                        { label: legends[1], data: cash.after, backgroundColor: c.main },
                    ] },
                    options: { scales: { y: { title: axisTitle('ريال') } } },
                }));
            } else if (cash) {
                const weekly = series => Array.from({ length: Math.ceil((series.length - 1) / 7) }, (_, index) =>
                    Math.min(...series.slice(1 + index * 7, 8 + index * 7)));
                drawChart(this.$refs.cash, c => ({
                    type: 'line',
                    data: {
                        labels: cash.dates.slice(1).filter((_, index) => index % 7 === 0).map((_, index) => number(index + 1)),
                        datasets: [
                            { label: legends[0], data: weekly(cash.before), borderColor: c.reference, borderDash: [6, 4], pointRadius: 0 },
                            { label: legends[1], data: weekly(cash.after), borderColor: c.main, pointRadius: 0 },
                        ],
                    },
                    options: {
                        interaction: { mode: 'index', intersect: false },
                        scales: { x: reverseTime('الأسبوع'), y: { title: axisTitle('ريال') } },
                        plugins: { tooltip: { callbacks: {
                            title: items => 'أسبوع ' + cash.dates[1 + items[0].dataIndex * 7],
                        } } },
                    },
                }));
            }
        },
        follow(id, button) {
            this.selection = id;
            if (button) Alpine.nextTick(() => this.$refs.focus.focus());
        },
    }));
});
