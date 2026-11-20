document.addEventListener('alpine:init', () => {
    const base = '/enterprise/scenarios';
    const scopeKey = ({ model_id, version, company_id }) => model_id == null
        ? (company_id == null ? 'demo' : 'demo:' + company_id) : model_id + ':' + version;
    const kpis = {
        profit: 'النتيجة بعد التمويل', cash_low: 'أقل رصيد سيولة', credit_breach: 'احتمال تجاوز الائتمان',
        delays: 'إجمالي تأخير المشروعات', fill_rate: 'نسبة تلبية طلبات الإنتاج',
    };
    const chartNumber = new Intl.NumberFormat(locale, { notation: 'compact', maximumFractionDigits: 2 });
    Alpine.data('scenariosPage', () => ({
        models: [], saved: [], drivers: [], scope: '', scenarioIds: [], driverIds: [],
        variation: 10, kpi: 'profit', comparison: null, tornado: null,
        busy: '', error: '',
        kpis, number, signed, enterpriseResultContext,
        get scopes() {
            const models = new Map(this.models.map(row => [row.id, row.name]));
            const options = new Map([['demo', 'ديمو شركة روافد']]);
            for (const row of [...this.models.map(model => ({ ...model, model_id: model.id })), ...this.saved]) {
                if (row.model_id == null) {
                    const company = this.$store.app.companies.find(item => item.id === row.company_id);
                    if (company) options.set(scopeKey(row), 'ديمو شركة روافد · ' + company.name);
                    continue;
                }
                const name = models.get(row.model_id) || 'نموذج رقم ' + row.model_id;
                options.set(scopeKey(row), name + ' · إصدار ' + row.version);
            }
            return Object.fromEntries(options);
        },
        get available() { return this.saved.filter(row => scopeKey(row) === this.scope); },
        get chartRows() { return (this.tornado?.rows ?? []).filter(row => row.low.delta != null || row.high.delta != null); },
        get sensitivityTitle() {
            return 'فرق ' + this.tornado?.metric.label + ' عن خط الأساس (' + this.tornado?.metric.unit + ')';
        },
        get snapshot() {
            if (this.scope === 'demo' || this.scope.startsWith('demo:')) return {};
            const [model_id, version] = this.scope.split(':').map(Number);
            return { model_id, version };
        },
        async perform(action, work) {
            if (this.busy) return;
            this.busy = action;
            this.error = '';
            try { await work(); }
            catch (error) { this.error = error.message; }
            finally {
                this.busy = '';
                if (action === 'tornado' && this.$store.app.page === 'scenarios' && this.tornado) {
                    await Alpine.nextTick();
                    this.renderChart();
                }
            }
        },
        load() {
            return this.perform('load', async () => {
                [this.models, this.saved] = await Promise.all([api('/enterprise/models'), api(base)]);
                const app = this.$store.app;
                const model = app.isOnboardingCompany && this.models.find(item => item.company_id === app.companyId);
                if (model) this.scope = scopeKey({ ...model, model_id: model.id });
                else if (!(this.scope in this.scopes)) this.scope = this.saved.length ? scopeKey(this.saved[0]) : 'demo';
                await this.loadDrivers();
            });
        },
        async loadDrivers() {
            this.scenarioIds = [];
            this.driverIds = [];
            this.drivers = [];
            this.comparison = this.tornado = null;
            const query = new URLSearchParams(this.snapshot).toString();
            this.drivers = (await api('/enterprise/whatif/drivers' + (query ? '?' + query : ''))).drivers;
        },
        compare() {
            return this.perform('compare', async () => {
                this.comparison = null;
                const inputs = { scenario_ids: this.scenarioIds.map(Number) };
                this.comparison = await api(base + '/compare?include_excel=true', post(inputs));
            });
        },
        analyze() {
            return this.perform('tornado', async () => {
                this.tornado = null;
                const inputs = { ...this.snapshot, kpi: this.kpi, percent: this.variation, driver_ids: [...this.driverIds] };
                this.tornado = await api(base + '/tornado?include_excel=true', post(inputs));
            });
        },
        download(kind) {
            const data = kind === 'compare' ? this.comparison : this.tornado;
            const bytes = Uint8Array.from(atob(data.xlsx_base64), char => char.charCodeAt(0));
            const url = URL.createObjectURL(new Blob([bytes], {
                type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            }));
            const link = document.createElement('a');
            link.href = url;
            link.download = kind === 'compare' ? 'enterprise-scenarios.xlsx' : 'enterprise-tornado.xlsx';
            link.click();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
        },
        renderChart() {
            const rows = this.chartRows;
            if (!rows.length) return;
            const title = 'الفرق عن خط الأساس (' + this.tornado.metric.unit + ')';
            const variation = number(this.tornado.percent);
            drawChart(this.$refs.tornadoChart, c => ({
                type: 'bar',
                data: {
                    labels: rows.map(({ driver }) => driver.id),
                    datasets: [
                        { label: 'خفض المدخل ' + variation + '%', data: rows.map(row => row.low.delta),
                            backgroundColor: c.reference, maxBarThickness: 16 },
                        { label: 'زيادة المدخل ' + variation + '%', data: rows.map(row => row.high.delta),
                            backgroundColor: c.main, maxBarThickness: 16 },
                    ],
                },
                options: {
                    indexAxis: 'y',
                    scales: {
                        x: { reverse: true, title: axisTitle(title), ticks: {
                            maxTicksLimit: 5, callback: value => chartNumber.format(value),
                        }},
                        y: { position: 'right', grid: { display: false }, ticks: {
                            autoSkip: false, callback: (_, index) => rows[index].driver.name_ar,
                            font: { size: 11 },
                        }},
                    },
                    plugins: { tooltip: { callbacks: {
                        title: items => rows[items[0].dataIndex].driver.name_ar,
                        label: context => context.dataset.label + ' · ' + signed(context.raw),
                    }}},
                },
            }));
        },
    }));
});
