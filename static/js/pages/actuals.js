const actualMetricLabels = {
    duration_weeks: 'المدة (أسبوع)', cost: 'التكلفة (ألف ريال)', qty: 'الكمية (وحدة)',
    late_days: 'التأخير (يوم)', start_week: 'أسبوع البدء', finish_week: 'أسبوع الانتهاء',
    ship_day: 'يوم الشحن', observed_rows: 'السجلات الفعلية',
};

document.addEventListener('alpine:init', () => {
    Alpine.data('actualsPage', () => workspacePage('actuals', () => ({
        plans: [], planId: '', fileName: '', actuals: null, calibrations: [], detailMetric: '',
    }), {
        variantLabel,
        get comparison() { return this.actuals?.comparison; },
        get metrics() { return Object.keys(this.comparison?.rows[0]?.plan || {}); },
        metricLabel(key) { return actualMetricLabels[key] || key; },
        calibrationLabel(key) {
            const [kind, entity] = key.split('.');
            const phase = { F: 'الأساسات', S: 'الهيكل', X: 'التشطيبات' }[entity] ?? entity;
            return (kind === 'dur_ratio' ? 'معامل مدة ' : 'معامل طلب ') + phase;
        },
        calibrationValue(value) {
            return typeof value === 'number' ? number(value)
                : 'متوسط ' + number(value?.mu) + ' · انحراف ' + number(value?.sigma) + ' · عينة ' + number(value?.n);
        },
        reset() { this.$refs.actualFile.value = ''; },
        async load(base, live) {
            const [plans, actuals, calibrations] = await Promise.all([
                api(base + '/plans'), api(base + '/actuals/latest?allow_missing=true'), api(base + '/calibrations'),
            ]);
            if (!live()) return;
            this.plans = plans.filter(plan => plan.status === 'approved');
            this.calibrations = calibrations;
            if (actuals) this.showActuals(actuals, live);
        },
        showActuals(result, live) {
            this.actuals = result;
            const chart = result.comparison.chart;
            this.detailMetric = chart.metric;
            this.$nextTick(() => {
                if (live() && chart.labels.length) comparisonChart(this.$refs.pva, chart.labels,
                    chart.plan, chart.actual, ['المخطط', 'الفعلي'], this.metricLabel(chart.metric));
            });
        },
        upload() {
            const body = new FormData(this.$refs.actualForm);
            this.perform('upload', async (base, live) => {
                const result = await api(base + '/actuals/upload', { method: 'POST', body });
                if (!live()) return;
                this.showActuals(result, live);
                this.fileName = '';
                this.reset();
                this.message = 'تم حفظ المقارنة بالبيانات الفعلية.';
            });
        },
        recalibrate() {
            this.perform('calibrate', async (base, live) => {
                const result = await api(base + '/recalibrate', post());
                if (!live()) return;
                this.message = result.msg_ar;
                const calibrations = await api(base + '/calibrations');
                if (live()) this.calibrations = calibrations;
            });
        },
    }));
});
