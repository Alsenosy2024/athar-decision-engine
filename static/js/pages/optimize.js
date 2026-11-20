document.addEventListener('alpine:init', () => {
    Alpine.data('optimizePage', () => workspacePage('optimize', () => ({ run: null, runId: null, datasetId: null }), {
        get rows() { return metricRows(this.run?.baseline?.kpis, this.run?.optimized?.kpis, this.template); },
        async load(base, live) {
            const saved = await latestRun(base);
            if (live() && saved) this.showRun(saved.id, saved.result, saved.dataset_id);
        },
        optimize({ forecast = false, replan = false, replay = false } = {}) {
            this.perform('solve', async (base, live) => {
                const datasetId = replay ? this.datasetId : this.$store.app.company.active_dataset_id;
                const previousRunId = this.runId;
                const options = replay ? { replay_run_id: previousRunId } : { dataset_id: datasetId };
                if (forecast) {
                    const result = await api(base + '/forecast', post({ dataset_id: datasetId }));
                    if (!live()) return;
                    options.forecast_id = result.forecast_id;
                }
                if (replan || (forecast && previousRunId)) options.previous_run_id = previousRunId;
                const result = await api(base + '/optimize', post(options));
                if (live()) this.showRun(result.run_id, result, datasetId);
            });
        },
        get contract() { return this.run?.decision_contract; },
        get resultLabel() {
            const status = this.run?.solver?.status;
            if (this.contract && status === 'FEASIBLE') return 'حل تجريبي ممكن';
            if (this.contract && status === 'OPTIMAL') return 'حل تجريبي أمثل';
            return solverLabel(status);
        },
        get demandTargets() { return this.run?.optimized?.kpis?.forecast_inventory_targets || []; },
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
        askAboutRun(id) { this.$store.app.sectorChatRun = { id, company: this.$store.app.companyId }; },
        draft() {
            if (this.contract || !this.run?.solution_record?.optimized?.eligible) return;
            this.perform('draft', (base, live) => this.sendToReview(base, this.runId, 'optimized', live));
        },
    }));
});
