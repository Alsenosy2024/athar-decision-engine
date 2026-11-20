document.addEventListener('alpine:init', () => {
    Alpine.data('optimizePage', () => workspacePage('optimize', () => ({ run: null, runId: null, datasetId: null }), {
        get rows() { return metricRows(this.run?.baseline?.kpis, this.run?.optimized?.kpis, this.template); },
        async load(base, live) {
            const saved = await latestRun(base);
            if (live() && saved) this.showRun(saved.id, saved.result, saved.dataset_id);
        },
        optimize() {
            this.perform('solve', async (base, live) => {
                const datasetId = this.$store.app.company.active_dataset_id;
                const result = await api(base + '/optimize', post({ dataset_id: datasetId }));
                if (live()) this.showRun(result.run_id, result, datasetId);
            });
        },
        showRun(id, run, datasetId) {
            this.run = run;
            this.runId = id;
            this.datasetId = datasetId;
            this.$store.app.lastRun = { id, result: run, dataset_id: datasetId };
            const company = this.$store.app.companyId;
            this.$nextTick(() => {
                if (this.$store.app.companyId !== company || this.$store.app.page !== 'optimize') return;
                const rows = this.rows.filter(row => row.unit && row.unit !== '%' && ['total', 'total_cost', 'peak_od', 'interest', 'ld', 'lateness_cost', 'changeover_cost', 'holding_cost'].includes(row.key));
                if (rows.length) comparisonChart(this.$refs.compare, rows.map(row => row.label),
                    rows.map(row => row.before), rows.map(row => row.after), ['الخطة المرجعية', 'الخطة المحسّنة'], rows[0].unit);
            });
        },
        draft() {
            this.perform('draft', (base, live) => this.sendToReview(base, this.runId, 'optimized', live));
        },
    }));
});
