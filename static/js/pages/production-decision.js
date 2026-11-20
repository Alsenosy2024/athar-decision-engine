document.addEventListener('alpine:init', () => {
    const labels = {
        central: 'الطلب المركزي', low_demand: 'طلب تقديري أقل', high_demand: 'طلب تقديري أعلى',
        service: 'أولوية الخدمة', liquidity: 'أولوية السيولة',
    };
    const rules = { physical: 'قيد تشغيل', credit: 'حد الائتمان', service: 'حد الخدمة' };
    const statuses = {
        OPTIMAL: 'الأمثل', FEASIBLE: 'حل ممكن', UNKNOWN: 'المهلة انتهت', INFEASIBLE: 'مفيش حل ممكن',
    };
    const stageStatuses = {
        complete: 'التجربة اكتملت', awaiting_observations: 'مستني الطلب الفعلي',
        budget_exhausted: 'مهلة إعادة التخطيط انتهت', no_verified_recourse: 'مفيش خطة بديلة عدّت التحقق',
    };
    Alpine.data('productionDecision', () => workspacePage('optimize', () => ({
        result: null, activeCandidate: null, rolling: true,
    }), {
        load() {},
        label: id => labels[id] ?? id ?? '—',
        rule: id => rules[id],
        status: id => statuses[id] ?? id,
        stageStatus: id => stageStatuses[id] ?? id,
        get selected() { return this.result?.candidates.find(row => row.id === this.result.selected_id); },
        runExperiment() {
            this.perform('experiment', async (base, live) => {
                const result = await api(base + '/decision-loop/production', post({
                    dataset_id: this.$store.app.company.active_dataset_id,
                    rolling: this.rolling,
                }));
                if (live()) {
                    this.result = result;
                    this.activeCandidate = result.selected_id ?? result.candidates[0]?.id;
                }
            });
        },
    }));
});
