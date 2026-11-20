const hybridCandidates = {
    central: 'الطلب المركزي', low_demand: 'طلب تقديري أقل', high_demand: 'طلب تقديري أعلى',
    service: 'أولوية الخدمة', liquidity: 'أولوية السيولة',
};
const candidateLabel = id => hybridCandidates[id] ?? id ?? '—';

const paretoDepartments = {
    operations: 'التشغيل', sales: 'المبيعات', finance: 'المالية', leadership: 'الإدارة العليا',
};
const paretoMetrics = {
    holding_and_setup_sar: 'التخزين وتبديل المنتج (ريال)',
    on_time_service_pp: 'التسليم في الموعد (نقطة مئوية)',
    peak_credit_sar: 'أقصى سحب (ريال)', financing_sar: 'التمويل (ريال)',
    cost_sar: 'التكلفة (ريال)',
};
const paretoPointLabel = point => point.candidate_id ? candidateLabel(point.candidate_id)
    : Object.entries(point.selection).map(([unit, plan]) => unit + ' · #' + plan.slice(5)).join(' / ');
const compactHybridNumber = new Intl.NumberFormat(locale, {notation: 'compact', maximumFractionDigits: 1});

function candidateCostChart(canvas, risk) {
    drawChart(canvas, ({reference, main}) => ({
        type: 'bar',
        data: {
            labels: risk.candidates.map(row => candidateLabel(row.id)),
            datasets: [
                {label: 'وسيط التكلفة (P50)', data: risk.candidates.map(row => row.p50_cost_sar),
                    backgroundColor: reference, borderRadius: 3, maxBarThickness: 16},
                {label: 'CVaR ' + percent(risk.alpha), data: risk.candidates.map(row => row.cvar_cost_sar),
                    backgroundColor: main, borderRadius: 3, maxBarThickness: 16},
            ],
        },
        options: {
            indexAxis: 'y',
            scales: {
                x: {beginAtZero: true, reverse: true, title: axisTitle('التكلفة (ريال)'),
                    ticks: {maxTicksLimit: 5, callback: value => compactHybridNumber.format(value)}},
                y: {position: 'right', grid: {display: false}, ticks: {autoSkip: false}},
            },
            plugins: {tooltip: {callbacks: {
                label: item => item.dataset.label + ' · ' + number(item.parsed.x) + ' ريال',
            }}},
        },
    }));
}

function paretoScatter(canvas, points) {
    const zeroFloor = key => points.every(point => point.objectives[key] === 0) ? {min: 0} : {};
    drawChart(canvas, ({main, ink}) => ({
        type: 'scatter',
        data: {datasets: [{
            label: 'خطط على جبهة المفاضلة',
            data: points.map(point => ({
                x: point.objectives.cost_sar, y: point.objectives.service_loss_pct, point,
            })),
            backgroundColor: main, borderColor: ink, borderWidth: 1, pointRadius: 6, pointHoverRadius: 9, clip: 8,
        }]},
        options: {
            scales: {
                x: {...zeroFloor('cost_sar'), title: axisTitle('التكلفة (ريال)'),
                    ticks: {maxTicksLimit: 5, callback: value => compactHybridNumber.format(value)}},
                y: {...zeroFloor('service_loss_pct'), title: axisTitle('فاقد الخدمة (%)'), ticks: {maxTicksLimit: 5}},
            },
            plugins: {legend: {display: false}, tooltip: {callbacks: {
                title: items => paretoPointLabel(items[0].raw.point),
                label: item => [
                    'التكلفة · ' + number(item.parsed.x) + ' ريال',
                    'فاقد الخدمة · ' + number(item.parsed.y) + '%',
                    'أقصى سحب · ' + number(item.raw.point.objectives.peak_credit_sar) + ' ريال',
                ],
            }}},
        },
    }));
}

document.addEventListener('alpine:init', () => {
    Alpine.directive('hybrid-chart', (canvas, {expression, value}, {effect, evaluateLater, cleanup}) => {
        const read = evaluateLater(expression);
        let frame;
        effect(() => read(data => {
            cancelAnimationFrame(frame);
            frame = requestAnimationFrame(() => {
                if (!canvas.isConnected || !data) return;
                if (value === 'cost') candidateCostChart(canvas, data);
                else paretoScatter(canvas, data);
            });
        }));
        cleanup(() => {
            cancelAnimationFrame(frame);
            Chart.getChart(canvas)?.destroy();
        });
    });

    const unit = () => ({id: '', current: '', plans: []});
    const limit = () => ({kind: 'credit', day: '', amount: ''});
    Alpine.data('hybridScenarios', () => workspacePage('scenarios', () => ({
        saved: null, benchmark: null,
        allocation: null, approvedPlans: [], units: [unit(), unit()], limits: [limit()],
    }), {
        candidateLabel, paretoPointLabel, paretoDepartments, paretoMetrics,
        reset() { this.$refs.allocationForm.open = false; },
        get regret() {
            return this.benchmark?.comparisons.find(row => row.reference === 'current' && row.challenger === 'scenario');
        },
        async load(base, live) {
            if (this.template !== 'production') return;
            const saved = await api('/ui' + base + '/hybrid-run');
            if (live()) this.saved = saved;
        },
        async runHybrid() {
            await this.perform('solve', async (base, live) => {
                const dataset_id = this.$store.app.company.active_dataset_id;
                const forecast = await api(base + '/forecast', post({dataset_id}));
                if (!live()) return;
                await api(base + '/optimize', post({dataset_id, forecast_id: forecast.forecast_id, risk_policy: {}}));
                await this.load(base, live);
            });
        },
        runBenchmark() {
            return this.perform('benchmark', async (base, live) => {
                const result = await api(base + '/ai/decision-benchmark', post());
                if (live()) this.benchmark = result;
            });
        },
        loadAllocation() {
            if (!this.$store.app.canPlan) return;
            return this.perform('plans', async (base, live) => {
                const plans = await api(base + '/plans');
                if (live()) this.approvedPlans = plans.filter(row => row.status === 'approved' && row.eligibility?.eligible);
            });
        },
        addUnit() { this.units.push(unit()); },
        addLimit() { this.limits.push(limit()); },
        async runAllocation() {
            this.allocation = null;
            await this.perform('allocation', async (base, live) => {
                const units = this.units.map(row => ({
                    id: row.id.trim(), current: Number(row.current),
                    plans: [...new Set([row.current, ...row.plans].map(Number))],
                }));
                const limits = Object.fromEntries(this.limits.map(row => [row.kind + '.' + Number(row.day), Number(row.amount)]));
                if (Object.keys(limits).length !== this.limits.length) {
                    throw new Error('سجّل كل حد مورد مرة واحدة لكل يوم.');
                }
                const result = await api(base + '/decision-loop/allocation', post({units, limits}));
                if (live()) this.allocation = result;
            });
        },
    }));
});
