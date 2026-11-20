const executiveRisk = metric => ['credit_breach', 'delays'].includes(metric.id);

function mapExecutiveControls() {
    let baselineRequest = 0;
    let baselineTimer, baselineController;

    return {
        baselineLoading: false, baselineError: '', baselineLedger: null, baselineWeeks: null,
        animateValue, percentileLabel, executiveRisk,
        resetExecutiveBaseline() {
            baselineRequest++;
            clearTimeout(baselineTimer);
            baselineController?.abort();
            this.baselineLoading = false;
            this.baselineError = '';
            this.baselineKpis = this.baselineLedger = this.baselineWeeks = null;
        },
        refreshExecutiveBaseline({ defer = false } = {}) {
            this.resetExecutiveBaseline();
            if (!(this.map?.has_engines || this.onboardingProjection) || this.map.analysis_error || this.map.process_only) return;
            const requestId = baselineRequest;
            const query = new URLSearchParams({ ...this.selection, weeks: this.weeks, include_ledger: 'true' });
            this.baselineLoading = true;
            const calculate = async () => {
                baselineController = new AbortController();
                try {
                    const values = await api('/enterprise/map/baseline?' + query, {
                        cache: 'no-store', signal: baselineController.signal,
                    });
                    if (requestId === baselineRequest) {
                        this.baselineKpis = values.kpis;
                        this.baselineLedger = values.cash_ledger?.rows ?? null;
                        this.baselineWeeks = values.weeks;
                    }
                } catch (error) {
                    if (requestId === baselineRequest) this.baselineError = error.message;
                } finally {
                    if (requestId === baselineRequest) this.baselineLoading = false;
                }
            };
            if (defer) baselineTimer = setTimeout(calculate, 900);
            else calculate();
        },
        get executiveMetrics() {
            const metrics = this.metrics;
            const operations = metrics.slice(2);
            const reserves = ['pnl.revenue', 'demand'].flatMap(id => {
                const row = this.map?.nodes.find(node => node.id === id);
                if (row?.baseline == null) return [];
                const impact = this.result?.nodes.find(node => node.id === id);
                const headline = impact ? impact.quantiles?.p50 ?? impact.after : row.baseline;
                return [{ ...row, label: row.name_ar, headline, baseline: impact?.before ?? row.baseline, history: [],
                    bands: impact?.quantiles }];
            });
            return [...metrics.slice(0, 2),
                ...operations.filter(row => row.headline != null), ...reserves,
                ...operations.filter(row => row.headline == null)].slice(0, 4);
        },
        get mainAlert() { return this.briefing?.alerts[0]; },
        get signalText() {
            if (this.mainAlert) return this.mainAlert.text;
            if (this.map?.analysis_error) return this.map.analysis_error;
            if (this.baselineError) return this.baselineError;
            if (this.baselineLoading) return 'بنحسب الربح والسيولة من النموذج.';
            if (this.map?.readiness != null && this.map.readiness < 100) {
                return 'تجهيز بيانات الشركة لسه جاري (' + percent(this.map.readiness / 100) + ').';
            }
            if (this.briefing?.warning) return this.briefing.warning;
            if (this.briefingError) return this.briefingError;
            if (!this.briefing) return 'بنراجع مؤشرات المتابعة.';
            if (!Object.values(this.briefing.history).some(values => values.length > 8)) {
                return 'السجل المتاح قصير على فحص الشذوذ.';
            }
            return 'فحص آخر القراءات ما رصدش تنبيه.';
        },
        get executiveDecision() {
            const recommendation = this.recommendations?.recommendations[0];
            if (recommendation && !recommendation.violations.length && recommendation.target_met !== false) {
                const action = recommendation.assumptions.map(row =>
                    this.label(row.node_id) + ': ' + mapValue(row.value, this.unit(row.node_id))).join('، ');
                return { text: 'راجع البديل الأعلى ترتيبًا · ' + action, next: 'راجع الفرضيات', run: () => this.useRecommendation(recommendation) };
            }
            if (this.mainAlert) return { text: this.mainAlert.action, next: 'تتبّع التنبيه', run: () => {
                this.selectAlert(this.mainAlert.id);
                this.$nextTick(() => {
                    const trace = this.$refs.executiveTrace;
                    trace?.focus();
                });
            } };
            if (this.map?.readiness != null && this.map.readiness < 100) {
                return { text: 'كمّل المدخلات الناقصة لحساب الربح والسيولة.', next: 'ابدأ بتجهيز البيانات', href: '#/onboarding' };
            }
            if (this.baselineError) return { text: 'جرّب حساب المؤشرات مرة تانية.', next: 'أعد الحساب', run: () => this.refreshExecutiveBaseline() };
            if (this.recommendations) {
                return { text: 'راجع نطاق التغيير وقيود الهدف.', next: 'افتح التوصيات', run: () => {
                    this.$refs.executiveGoal.open = true;
                    this.$refs.executiveGoal.querySelector('summary').focus();
                } };
            }
            if (this.result) return { text: 'راجع فرق المؤشرات وسلسلة الأثر قبل اعتماد الفرضية.', next: 'راجع النتائج', run: () => this.$refs.executiveResults.focus() };
            if (this.editable && !this.map?.analysis_error && this.map?.drivers.length) {
                return { text: 'اختبر فرضية على الربح والسيولة.', next: 'اسأل أثر', run: () => this.$refs.inputPanel.querySelector('textarea')?.focus() };
            }
            return { text: 'راجع القيم المتاحة ومصادرها في أقسام الشركة.', next: 'افتح شبكة الأقسام', run: () => this.$refs.executiveNetwork.focus() };
        },
        executiveChange(metric) {
            if (this.result) return executiveChange(metric.baseline, metric.headline, executiveRisk(metric));
            return executiveChange(...(metric.history.length > 1 ? metric.history.slice(-2) : [null, null]), executiveRisk(metric));
        },
        executiveCaption(metric) {
            if (metric.headline == null) return this.baselineLoading ? 'جارٍ الحساب' : 'مفيش قيمة متاحة';
            return this.result ? this.fixedScenario ? 'سيناريو محدد'
                : metric.bands && metric.id !== 'credit_breach' ? 'متوقع (P50)' : 'نتيجة الفرضية'
                : (metric.history.length > 1 ? 'مقارنة آخر قراءتين' : 'خط الأساس') + ' · ' + this.source(metric.source);
        },
        recommendationBasis(item) {
            const elasticity = item.impact.paths.find(edge => edge.rule?.mode === 'elasticity'
                && item.assumptions.some(row => row.node_id === edge.source));
            if (elasticity) {
                return elasticity.name_ar + ' · أثر ' + this.label(elasticity.source) + ' على '
                    + this.label(elasticity.target) + ' بمعامل ' + number(elasticity.rule.multiplier) + '.';
            }
            return item.model_basis === 'cp_sat_and_propagation' ? 'الحساب قائم على خطة التشغيل وعلاقات النموذج الحالي.'
                : 'الحساب قائم على معاملات وعلاقات إصدار النموذج الحالي.';
        },
        get fixedScenario() {
            return !!this.result && !this.submitted.some(row => row.uncertainty > 0);
        },
    };
}
