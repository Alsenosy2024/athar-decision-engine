document.addEventListener('alpine:init', () => {
    Alpine.data('dashboardPage', () => workspacePage('dashboard', () => ({
        run: null, dataset: null,
    }), {
        get kpis() {
            const { baseline, optimized } = this.run?.result ?? {};
            const keys = this.template === 'contracting'
                ? ['total', 'late_weeks_total', 'peak_od', 'ld']
                : ['total_cost', 'late_orders', 'peak_od', 'peak_storage_pct'];
            const rows = metricRows(baseline?.kpis, optimized?.kpis, this.template);
            return keys.map(key => rows.find(row => row.key === key)).filter(Boolean);
        },
        get cashUnit() { return this.template === 'contracting' ? 'ألف ريال' : 'ريال'; },
        get outOfDate() {
            return this.run && this.run.dataset_id !== this.$store.app.company?.active_dataset_id;
        },
        async load(base, live) {
            const run = await latestRun(base);
            const dataset = run?.result.optimized?.plan ? await api('/datasets/' + run.dataset_id) : null;
            if (!live()) return;
            this.$store.app.lastRun = this.run = run;
            this.dataset = dataset?.payload;
            if (!dataset) return;
            this.$nextTick(() => {
                if (!live()) return;
                const { baseline, optimized } = this.run.result;
                if (this.template === 'contracting' && optimized.plan.activities.length) {
                    ganttChart(this.$refs.gantt, optimized.plan.activities, this.dataset.projects);
                }
                if (this.template === 'production' && optimized.plan.schedule.length) {
                    productionChart(this.$refs.plan, optimized.plan.schedule, optimized.kpis.storage_curve.length, this.dataset);
                }
                cashChart(this.$refs.cash, baseline.kpis.cash_curve, optimized.kpis.cash_curve, this.template, this.cashUnit);
            });
        },
    }));
});
