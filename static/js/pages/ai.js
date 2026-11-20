document.addEventListener('alpine:init', () => Alpine.data('aiWorkspace', () => ({
    sections: [
        ['entry', 'إدخال البيانات'], ['analysis', 'تحليل ومعايرة'], ['forecast', 'التوقعات'],
        ['decision', 'اختبار قرار'], ['question', 'اسأل أثر'],
    ],
    kinds: {
        entry: 'فرق البيانات', refresh: 'إشارات البيانات', forecast: 'توقع', calibration: 'معايرة العلاقات',
        decision: 'توصيات القرار', answer: 'رد أثر',
    },
    statuses: { pending: 'بانتظار المراجعة', accepted: 'مقبول', rejected: 'مرفوض' },
    engines: {
        stat: 'إحصائي', timesfm: 'TimesFM', chronos_bolt: 'Chronos-Bolt', ensemble: 'TimesFM + Chronos-Bolt',
        codex: 'Codex', rules: 'قواعد محلية', median: 'وسيط سابق',
    },
    units: { SAR: 'ريال', 'SAR/unit': 'ريال للوحدة', units: 'وحدة', storage_units: 'وحدة تخزين',
        teams: 'فريق', days: 'يوم', weeks: 'أسبوع', '%': '%' },
    number, percent, timestamp,
    companyId: '', modelId: '', data: null, records: [], moreRecords: false, review: null, section: 'entry',
    files: [], description: '', modelName: '', remote: true, fxCurrency: 'USD', fxRate: '',
    estimates: [], scenarioRank: 0, topic: '', focusNode: '', drill: null,
    forecastNode: '', forecastSource: 'model', horizon: '4',
    objective: '', direction: 'maximize', targetPct: '', cashFloor: '', riskCeiling: '', controls: [],
    goalText: '', busy: false, error: '', message: '',
    get allowed() { return ['admin', 'planner'].includes(this.$store.app.user?.role); },
    get payload() { return this.review?.data || {}; },
    get modelNodes() { return this.data?.nodes || []; },
    get goalDrivers() { return (this.data?.drivers || []).filter(item => item.id !== this.objective); },
    get forecastOptions() {
        if (this.forecastSource === 'operations') return this.data?.operations || [];
        return this.modelNodes.filter(node => this.data?.series.includes(node.id));
    },
    get questionFacts() { return this.payload.facts || this.payload.insight?.facts || []; },
    get reviewTitle() { return this.kinds[this.review?.kind] || ''; },
    get scope() { return '/companies/' + this.companyId + '/ai'; },
    get canAccept() {
        if (this.review.kind === 'entry') {
            return this.payload.diff.length || this.payload.relations.length ||
                this.payload.schedules.length || this.estimates.length;
        }
        if (this.review.kind === 'calibration') return this.payload.edges.length;
        if (this.review.kind === 'decision') return this.payload.recommendations.some(item => !item.violations.length);
        return true;
    },
    boot(companies) {
        if (this.companyId || !companies.length) return;
        const initial = Number(new URLSearchParams(location.search).get('company_id'));
        const company = companies.find(item => item.id === initial) || companies[0];
        this.changeCompany(company.id);
    },
    async work(callback) {
        this.busy = true;
        this.error = this.message = '';
        try {
            await callback();
        } catch (error) {
            this.error = error.message;
        } finally {
            this.busy = false;
        }
    },
    changeCompany(id) {
        this.companyId = String(id);
        this.modelId = '';
        this.review = this.data = null;
        this.records = [];
        this.files = [];
        this.description = this.modelName = this.fxRate = this.topic = this.goalText = '';
        this.drill = null;
        this.controls = [];
        return this.work(async () => {
            await this.loadCatalog();
            await this.loadReviews();
        });
    },
    changeModel(id) {
        this.modelId = String(id);
        this.drill = null;
        this.controls = [];
        return this.work(() => this.loadCatalog());
    },
    async loadCatalog() {
        this.data = await api(this.scope + '/catalog' + (this.modelId ? '?model_id=' + this.modelId : ''));
        this.forecastSource = this.modelId && this.data.series.length ? 'model' : this.data.operations.length ? 'operations' : 'model';
        this.forecastNode = this.forecastOptions[0]?.id || '';
        if (this.modelId && !this.modelNodes.some(node => node.id === this.objective)) {
            this.objective = this.modelNodes.find(node => node.id === 'pnl.result')?.id || this.modelNodes[0]?.id || '';
        }
        if (!this.modelNodes.some(node => node.id === this.focusNode)) this.focusNode = '';
    },
    async loadReviews(more = false) {
        const before = more ? '?before=' + this.records.at(-1).id : '';
        const rows = await api(this.scope + '/reviews' + before);
        this.records = more ? [...this.records, ...rows] : rows;
        this.moreRecords = rows.length === 50;
    },
    changeSection(section) {
        this.section = section;
        if (section === 'decision' && !this.controls.length && this.goalDrivers.length) this.addControl();
    },
    addControl() {
        const driver = this.goalDrivers.find(item => !this.controls.some(control => control.node_id === item.id));
        if (!driver) return;
        const span = Math.max(Math.abs(driver.value) * 0.15, 1);
        this.controls.push({ node_id: driver.id, low: driver.value - span, high: driver.value + span, uncertainty: 0 });
    },
    replaceControl(control, nodeId) {
        const driver = this.goalDrivers.find(item => item.id === nodeId);
        const span = Math.max(Math.abs(driver.value) * 0.15, 1);
        Object.assign(control, { node_id: nodeId, low: driver.value - span, high: driver.value + span, uncertainty: 0 });
    },
    setFiles(event) {
        this.files = Array.from(event.dataTransfer?.files || event.target.files);
    },
    showReview(review) {
        this.review = review;
        this.estimates = [];
        this.scenarioRank = 0;
    },
    scrollReview() { this.$nextTick(() => this.$refs.review?.scrollIntoView({ block: 'start' })); },
    openReview(id) {
        return this.work(async () => {
            this.showReview(await api(this.scope + '/reviews/' + id));
            this.scrollReview();
        });
    },
    async create(path, options) {
        await this.work(async () => {
            this.showReview(await api(this.scope + path, options));
            await this.loadReviews();
            this.scrollReview();
        });
    },
    submitEntry() {
        const body = new FormData();
        for (const file of this.files) body.append('files', file, file.name);
        if (this.modelId) body.set('model_id', this.modelId);
        body.set('description', this.description);
        body.set('name', this.modelName);
        body.set('remote', this.remote);
        if (this.fxRate !== '') body.set('fx_rates', JSON.stringify({ [this.fxCurrency]: Number(this.fxRate) }));
        return this.create('/entry', { method: 'POST', body });
    },
    runAnalysis(path) {
        const body = { model_id: Number(this.modelId) };
        if (path === '/refresh') body.remote = this.remote;
        return this.create(path, post(body));
    },
    runForecast() {
        return this.create('/forecast', post({
            model_id: this.forecastSource === 'model' ? Number(this.modelId) : null,
            node_id: this.forecastNode, horizon: Number(this.horizon),
        }));
    },
    readGoal() {
        return this.work(async () => {
            const result = await api(this.scope + '/goal/interpret', post({ model_id: Number(this.modelId), text: this.goalText }));
            if (result.objective) this.setObjective(result.objective);
            if (result.direction) this.direction = result.direction;
            if (result.target_pct !== null) this.targetPct = result.target_pct;
            if (result.cash_floor !== null) this.cashFloor = result.cash_floor;
            this.message = 'راجع القيم اللي اتعبّت وحدّد حدود المدخلات قبل التشغيل.';
        });
    },
    setObjective(id) {
        this.objective = id;
        this.controls = this.controls.filter(item => item.node_id !== id);
    },
    runDecision() {
        const optional = value => value === '' ? null : Number(value);
        return this.create('/recommend', post({
            model_id: Number(this.modelId),
            goal: {
                objective: this.objective, direction: this.direction, target_pct: optional(this.targetPct),
                cash_floor: optional(this.cashFloor), risk_ceiling: optional(this.riskCeiling),
                controls: this.controls.map(control => ({
                    node_id: control.node_id, low: Number(control.low), high: Number(control.high),
                    uncertainty: Number(control.uncertainty),
                })),
            },
        }));
    },
    async drillInto(rank) {
        const review = this.review;
        const objective = review.data.objective;
        if (Number(this.modelId) !== review.model_id) {
            await this.changeModel(review.model_id);
            if (this.error) return;
        }
        this.drill = { id: review.id, rank };
        this.focusNode = objective;
        this.topic = 'إيه مسار الأثر في التوصية؟';
        this.changeSection('question');
    },
    ask() {
        return this.create('/question', post({
            model_id: Number(this.modelId), question: this.topic, target_node: this.focusNode || null,
            proposal_id: this.drill?.id || null, rank: this.drill?.rank || 0, remote: this.remote,
        }));
    },
    decide(status, rank = 0) {
        return this.work(async () => {
            const review = await api(this.scope + '/reviews/' + this.review.id + '/decide',
                post({ status, rank, estimates: this.estimates.map(Number) }));
            this.showReview(review);
            if (review.outcome?.model_id) {
                this.modelId = String(review.outcome.model_id);
                await this.loadCatalog();
            }
            await this.loadReviews();
            this.message = status === 'accepted' ? 'اتسجل قبول المراجعة.' : 'اتسجل رفض المراجعة.';
        });
    },
    nameOf(id) { return this.modelNodes.find(node => node.id === id)?.name_ar || id; },
    labelOf(engine) { return this.engines[engine] || engine; },
    unitOf(unit) { return this.units[unit] || unit; },
})));
