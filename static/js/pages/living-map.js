const mapUnits = { SAR: 'ريال', 'SAR/unit': 'ريال/وحدة', units: 'وحدة', storage_units: 'وحدة تخزين',
    teams: 'فريق', days: 'يوم', weeks: 'أسبوع', '%': '%' };
const mapValue = (value, unit) => value == null ? '—' : number(value) + (unit ? ' ' + (mapUnits[unit] || unit) : '');
const mapPair = (source, target) => source + ':' + target;
const higherIsAdverse = row => (row.id.startsWith('risk.') && row.id !== 'risk.fill') ||
    row.type === 'supplier' || row.type === 'payables' ||
    ['pnl.expenses', 'pnl.direct_cost', 'financing', 'financing.draw'].includes(row.id) ||
    row.id.startsWith('downtime.');
const mapAdverse = row => row?.delta != null && (higherIsAdverse(row) ? row.delta > 0 : row.delta < 0);
const mapMagnitude = row => Math.abs(row.delta_pct ?? (row.delta ? 100 : 0));

// Relax the carried paths by week, then by hop. The hop breaks zero-lag
// ties, and cycles such as financing feedback cannot keep delaying a wave.
function mapTimeline(result, assumptions) {
    const times = new Map(assumptions.map(row => [row.node_id, { week: 0, depth: 0 }]));
    const paths = [...new Map(result.paths.map(row => [mapPair(row.source, row.target), row])).values()];
    for (let i = 0; i < result.nodes.length; i++) {
        let changed = false;
        for (const edge of paths) {
            const source = times.get(edge.source);
            if (!source) continue;
            const next = { week: source.week + (edge.rule?.lag_weeks || 0), depth: source.depth + 1 };
            const current = times.get(edge.target);
            if (!current || next.week < current.week || (next.week === current.week && next.depth < current.depth)) {
                times.set(edge.target, next);
                changed = true;
            }
        }
        if (!changed) break;
    }
    const waves = paths.flatMap(edge => {
        const source = times.get(edge.source);
        if (!source) return [];
        const lag = edge.rule?.lag_weeks || 0;
        const arrival = source.week + lag;
        return [{ ...edge, lag, arrival, start: source.week + source.depth * .22,
            end: arrival + (source.depth + 1) * .22 }];
    }).sort((a, b) => a.end - b.end || a.start - b.start);
    return { times, waves, lastWeek: Math.max(0, ...waves.map(edge => edge.arrival)) + 1,
        duration: Math.max(.45, ...waves.map(edge => edge.end + .34)) };
}

document.addEventListener('alpine:init', () => {
    Alpine.data('livingMapPage', () => {
        let renderer, animation = 0, timeline;
        let visit = 0, active = '';
        let nodeById = new Map(), resultById = new Map(), departmentById = new Map(), edgeByPair = new Map();
        const reduced = matchMedia('(prefers-reduced-motion: reduce)');

        const page = {
            processMarkup: '',
            markup: '', map: null, company: null, causalRoads: [], observedRoads: [], focus: '', selectedModel: '',
            busy: '', error: '', feedback: '', question: '', provider: '', edits: [], driverId: '',
            weeks: 52, baselineKpis: null, result: null, submitted: [], week: 0, clock: null, playing: false,
            saved: [], scenarioName: '', compareIds: [], comparison: null,
            goalDriver: '', goalLow: '', goalHigh: '', goalProfit: '', goalCash: '', recommendations: null,
            number, mapValue,
            unitLabel(unit) { return mapUnits[unit] || unit; },
            get editable() { return ['admin', 'planner'].includes(this.$store.app.user?.role); },
            get selection() {
                return this.map?.model?.id == null ? {} : {
                    model_id: this.map.model.id, version: this.map.model.version,
                };
            },
            get models() { return this.map?.models || []; },
            get availableDrivers() {
                return (this.map?.drivers || []).filter(row => !this.edits.some(edit => edit.node_id === row.id));
            },
            get focusedDepartment() { return departmentById.get(this.focus); },
            get focusedNodes() { return this.focusedDepartment?.node_ids.map(id => this.nodeAtWeek(id)) || []; },
            get lastWeek() { return this.isSimulating ? this.simulation.weeks.length : this.result ? timeline.lastWeek : 1; },
            get hasPlayback() { return this.isSimulating || (this.flowMode === 'impact' && !!this.result); },
            get waves() { return this.result ? timeline.waves : []; },
            get visibleWaves() { return this.flowMode === 'impact' ? this.waves.filter(edge => edge.arrival < this.week) : this.waves; },
            get affected() {
                if (!this.result) return [];
                return this.map.departments.flatMap(dept => {
                    const rows = dept.node_ids.map(id => resultById.get(id)).filter(row => row?.affected);
                    if (!rows.length) return [];
                    rows.sort((a, b) => mapMagnitude(b) - mapMagnitude(a));
                    return [{ ...dept, count: rows.length, lead: rows[0] }];
                }).sort((a, b) => mapMagnitude(b.lead) - mapMagnitude(a.lead) || b.count - a.count);
            },
            get metrics() {
                return (this.map?.kpis || []).map(kpi => {
                    const row = this.result && resultById.get(kpi.node_id);
                    const fill = kpi.id === 'fill_rate';
                    const after = fill ? this.result?.fill_rate ?? row?.after : row?.after;
                    const baseline = fill && this.result?.fill_rate_baseline != null
                        ? this.result.fill_rate_baseline : row ? row.before : (this.baselineKpis?.[kpi.id] ?? this.briefing?.kpis[kpi.id] ?? kpi.baseline);
                    const bands = fill ? this.result?.fill_rate_quantiles ?? row?.quantiles : row?.quantiles;
                    // The breach KPI is the fraction of draws across the interval, while its
                    // percentiles describe the distribution of individual draws.
                    const headline = kpi.id !== 'credit_breach' && bands ? bands.p50 : after;
                    return { ...kpi, history: this.briefing?.history[kpi.id] ?? [], baseline, after, headline: this.result ? headline : baseline,
                        bands, source: after != null
                            ? row?.scenario_source || 'estimated'
                            : this.result ? 'missing' : (this.baselineKpis?.[kpi.id] ?? this.briefing?.kpis[kpi.id]) != null && (this.map.has_engines || kpi.baseline == null)
                                ? 'estimated' : kpi.source };
                });
            },
            get canSeek() {
                const profit = this.map?.kpis.find(row => row.id === 'profit');
                return this.editable && !this.map?.analysis_error && this.map?.goal_drivers.length &&
                    (profit?.baseline != null || (!!this.map?.has_engines && nodeById.has('pnl.result')));
            },
            get canRun() {
                return this.editable && !this.busy && this.edits.length && !this.map?.analysis_error &&
                    this.edits.every(row => this.numeric(row.value) && this.numeric(row.uncertainty)
                        && Number(row.uncertainty) >= 0);
            },
            numeric(value) { return String(value ?? '').trim() !== '' && Number.isFinite(Number(value)); },
            label(id) { return nodeById.get(id)?.name_ar || id; },
            driverLabel(id) { return this.map?.drivers.find(row => row.id === id)?.name_ar || this.label(id); },
            unit(id) { return nodeById.get(id)?.unit || ''; },
            deptLabel(id) { return departmentById.get(id)?.name_ar || id; },
            source(value) { return { actual: 'فعلي', estimated: 'مقدّر', missing: 'غير متاح' }[value]; },
            operationUnit(row) { return row.operation === 'pct' ? '%' : this.unit(row.node_id); },
            deltaText(row) {
                return row?.delta == null ? '—' : signed(row.delta) + ' ' + (mapUnits[row.unit] || row.unit);
            },
            tone(row) {
                if (!row?.affected) return 'idle';
                if (!row.delta) return 'signal';
                if (mapAdverse(row)) return mapMagnitude(row) >= 15 ? 'critical' : 'warning';
                return mapMagnitude(row) >= 15 ? 'positive' : 'signal';
            },
            nodeAtWeek(id) {
                const node = nodeById.get(id);
                if (!node) return null;
                const row = this.result && this.flowMode === 'impact' && resultById.get(id);
                if (!row) return { ...node, value: node.baseline, affected: false };
                const when = timeline?.times.get(id)?.week ?? 0;
                const reached = this.week > when;
                const value = reached ? row.after : row.before;
                const source = reached && row.affected ? row.scenario_source || 'estimated' : node.source;
                return { ...node, value, source: value == null ? 'missing' : source,
                    delta: row.delta, delta_pct: row.delta_pct, affected: reached && row.affected };
            },
            deptTone(dept) {
                if (this.processFrame) {
                    const row = this.processFrame.departments.find(item => item.id === dept.id);
                    return row?.waiting ? 'critical' : row?.in_service ? 'signal' : 'idle';
                }
                const priority = ['critical', 'warning', 'positive', 'signal', 'idle'];
                const tones = dept.node_ids.map(id => this.tone(this.nodeAtWeek(id)));
                return priority.find(tone => tones.includes(tone));
            },
            spark(history) {
                const low = Math.min(...history), span = Math.max(...history) - low || 1;
                return history.map((value, i) => (i * 80 / Math.max(history.length - 1, 1)) + ',' +
                    (22 - (value - low) / span * 20)).join(' ');
            },

            activate(page, companyId, loading) {
                const key = page === 'map' && !loading ? String(companyId ?? 'demo') : '';
                if (key === active) return;
                active = key;
                visit++;
                this.resetBriefing();
                this.stop();
                renderer?.destroy();
                renderer = null;
                if (!key) return;
                this.map = null;
                this.busy = this.error = this.feedback = '';
                this.weeks = 52;
                const current = visit;
                queueMicrotask(() => { if (current === visit) this.load(companyId); });
            },
            destroy() {
                visit++;
                this.resetBriefing();
                this.stop();
                renderer?.destroy();
            },
            async work(action, fn) {
                if (this.busy || !active) return;
                const current = visit;
                const live = () => current === visit && this.$store.app.page === 'map';
                this.busy = action;
                this.error = this.feedback = '';
                try { await fn(live); }
                catch (error) { if (live()) this.error = error.message; }
                finally { if (live()) this.busy = ''; }
            },
            load(companyId, modelId = '', { processImport = null, refresh = false } = {}) {
                return this.work('load', async live => {
                    const query = new URLSearchParams();
                    if (companyId != null) query.set('company_id', companyId);
                    if (modelId) query.set('model_id', modelId);
                    if (processImport) {
                        await api('/companies/' + companyId + '/process-mining/' + processImport.endpoint,
                            processImport.options);
                        if (!live()) return;
                    }
                    const [original, markup, scenarios, processMarkup] = await Promise.all([
                        api('/enterprise/map?' + query),
                        this.markup || fetch('/static/partials/living-map.html').then(response => {
                            if (!response.ok) throw new Error('تعذّر تحميل واجهة خريطة الشركة.');
                            return response.text();
                        }),
                        api('/enterprise/scenarios'),
                        this.processMarkup || fetch('/static/partials/process-flow.html').then(response => {
                            if (!response.ok) throw new Error('تعذّر تحميل واجهة سجل الأحداث.');
                            return response.text();
                        }),
                    ]);
                    const map = original.model ? original : original.process_mining
                        ? this.processOnlyMap(original) : await api('/enterprise/map');
                    if (!live()) return;
                    if (refresh && !map.process_only && !this.map?.process_only &&
                        map.model.id === this.map?.model.id && map.model.version === this.map?.model.version) {
                        this.refreshBriefing();
                        return;
                    }
                    this.resetBriefing();
                    this.stop();
                    timeline = null;
                    resultById = new Map();
                    nodeById = new Map(map.nodes.map(row => [row.id, row]));
                    departmentById = new Map(map.departments.map(row => [row.id, row]));
                    edgeByPair = new Map(map.edges.map(edge => [mapPair(edge.source, edge.target), edge]));
                    const grouped = new Map();
                    for (const edge of map.edges) {
                        if (edge.from_department === edge.to_department) continue;
                        const key = mapPair(edge.from_department, edge.to_department);
                        const old = grouped.get(key);
                        grouped.set(key, { key, from: edge.from_department, to: edge.to_department,
                            weight: Math.max(old?.weight || 0, edge.weight) });
                    }
                    const scoped = scenarios.filter(row => row.model_id === map.model.id &&
                        row.version === map.model.version && row.company_id === (companyId ?? null));
                    this.resetProcessFlow(map);
                    Object.assign(this, { map, markup, processMarkup, company: original.company, causalRoads: [...grouped.values()],
                        selectedModel: map.model.id == null ? '' : String(map.model.id),
                        focus: '', edits: [], question: '', provider: '', driverId: '', result: null, submitted: [], baselineKpis: null,
                        week: 0, saved: scoped, scenarioName: '', comparison: null, compareIds: [],
                        goalDriver: '', goalLow: '', goalHigh: '', goalProfit: '', goalCash: '', recommendations: null });
                    if (!map.process_only) this.refreshBriefing();
                    this.$nextTick(() => {
                        if (live()) this.paint(this.$store.app.theme);
                    });
                });
            },
            chooseModel(value) { this.load(this.$store.app.companyId, value); },
            loadBaseline() {
                return this.work('baseline', async live => {
                    const values = await api('/enterprise/map/baseline', post({ ...this.selection, weeks: Number(this.weeks) }));
                    if (live()) this.baselineKpis = values;
                });
            },
            makeEdit(row) {
                return { node_id: row.node_id, operation: row.operation || 'set',
                    value: row.value, uncertainty: row.uncertainty || 0 };
            },
            addDriver() {
                const driver = this.availableDrivers.find(row => row.id === this.driverId);
                if (!driver || this.edits.length >= 50) return;
                this.edits.push(this.makeEdit({ node_id: driver.id, value: driver.value }));
                this.driverId = '';
            },
            slider(row) {
                const base = row.operation === 'set' ? nodeById.get(row.node_id).baseline : 0;
                const extent = row.operation === 'pct' ? 50 : Math.max(Math.abs(base || 0) * .4, 10);
                const value = this.numeric(row.value) ? Number(row.value) : base;
                const min = Math.min(base - extent, value), max = Math.max(base + extent, value);
                return { min, max, step: (max - min) / 200 };
            },
            setOperation(row, operation) {
                row.operation = operation;
                row.value = operation === 'set' ? nodeById.get(row.node_id).baseline : 0;
                row.uncertainty = 0;
            },
            ask() {
                const message = this.question.trim();
                if (!message) return;
                return this.work('ask', async live => {
                    const response = await api('/enterprise/chat', post({ ...this.selection, message, parse_only: true }));
                    if (!live()) return;
                    this.provider = response.provider;
                    this.feedback = response.reply_ar;
                    if (response.scenario.intent === 'what_if') {
                        this.edits = response.scenario.assumptions.map(row => this.makeEdit(row));
                        this.driverId = '';
                    }
                });
            },
            run() {
                if (!this.canRun) return;
                const assumptions = this.edits.map(row => ({ ...row, value: Number(row.value),
                    uncertainty: Number(row.uncertainty) }));
                return this.work('run', async live => {
                    const result = await api('/enterprise/whatif', post({
                        ...this.selection, assumptions, weeks: Number(this.weeks), n: 200, seed: 42,
                    }));
                    if (!live()) return;
                    resultById = new Map(result.nodes.map(row => [row.id, row]));
                    timeline = mapTimeline(result, assumptions);
                    this.result = result;
                    this.submitted = assumptions;
                    this.showImpact();
                });
            },
            highlight() {
                if (this.processFrame) return this.flowEffects();
                if (this.flowMode !== 'impact') return new Map();
                const effects = new Map();
                for (const edge of this.waves) {
                    const mapped = edgeByPair.get(mapPair(edge.source, edge.target));
                    if (!mapped || (this.clock == null ? this.week <= edge.arrival : this.clock < edge.start)) continue;
                    const key = mapPair(mapped.from_department, mapped.to_department);
                    const progress = this.clock == null ? 1 :
                        Math.max(0, Math.min(1, (this.clock - edge.start) / (edge.end - edge.start)));
                    const trail = this.clock == null ? 0 : (this.clock - edge.end) / .34;
                    const pulse = trail > 0 && trail < 1 ? trail : 0;
                    const adverse = mapAdverse(resultById.get(edge.target));
                    const prior = effects.get(key);
                    if (!prior || prior.progress <= progress || progress < 1) effects.set(key, { progress, pulse, adverse });
                }
                return effects;
            },
            paint(theme) {
                const stage = this.$refs.livingStage;
                if (!stage || !this.map || !active) return;
                renderer ??= createLivingMapCanvas(stage);
                renderer.update({ edges: this.roads, layout: this.selectedModel, theme,
                    focus: this.focus, effects: this.traceEffects() ?? this.highlight() });
            },
            stop() {
                cancelAnimationFrame(animation);
                animation = 0;
                this.playing = false;
                this.clock = null;
            },
            scrub(value) {
                this.selectAlert('');
                this.stop();
                this.week = Number(value);
            },
            replay() {
                this.selectAlert('');
                if (this.isSimulating) {
                    this.stop();
                    this.week = 1;
                    if (reduced.matches) return;
                    this.clock = 0;
                    this.playing = true;
                    const start = performance.now();
                    const interval = Math.min(850, 10000 / this.lastWeek);
                    const tick = now => {
                        const elapsed = (now - start) / interval;
                        this.week = Math.min(this.lastWeek, Math.floor(elapsed) + 1);
                        if (elapsed >= this.lastWeek) {
                            this.stop();
                            return;
                        }
                        this.clock = elapsed % 1;
                        animation = requestAnimationFrame(tick);
                    };
                    animation = requestAnimationFrame(tick);
                    return;
                }
                if (!timeline) return;
                this.stop();
                if (reduced.matches) {
                    this.week = timeline.lastWeek;
                    return;
                }
                this.week = 0;
                this.clock = 0;
                this.playing = true;
                const start = performance.now();
                const duration = Math.min(6500, Math.max(1450, timeline.duration * 700));
                const tick = now => {
                    const progress = Math.min(1, (now - start) / duration);
                    if (progress >= 1) {
                        this.stop();
                        this.week = timeline.lastWeek;
                        return;
                    }
                    this.clock = timeline.duration * progress;
                    this.week = Math.min(timeline.lastWeek, Math.floor(this.clock) + 1);
                    animation = requestAnimationFrame(tick);
                };
                animation = requestAnimationFrame(tick);
            },
            save() {
                const name = this.scenarioName.trim();
                if (!name || !this.submitted.length) return;
                return this.work('save', async live => {
                    const query = this.$store.app.companyId == null ? '' : '?company_id=' + this.$store.app.companyId;
                    const row = await api('/enterprise/scenarios' + query, post({
                        ...this.selection, name, assumptions: this.submitted,
                    }));
                    if (!live()) return;
                    this.saved.unshift(row);
                    this.scenarioName = '';
                    this.feedback = 'اتحفظ السيناريو.';
                });
            },
            compare() {
                if (this.compareIds.length < 2 || this.compareIds.length > 4) return;
                return this.work('compare', async live => {
                    const result = await api('/enterprise/scenarios/compare', post({
                        scenario_ids: this.compareIds.map(Number), weeks: Number(this.weeks), n: 200, seed: 42,
                    }));
                    if (live()) this.comparison = result;
                });
            },
            chooseGoalDriver(id) {
                const value = nodeById.get(id)?.baseline;
                this.goalDriver = id;
                this.goalLow = value ?? '';
                this.goalHigh = '';
            },
            seek() {
                if (!this.goalDriver || ![this.goalLow, this.goalHigh].every(this.numeric)) return;
                const goal = { weeks: Number(this.weeks), trials: 4, controls: [{
                    node_id: this.goalDriver, low: Number(this.goalLow), high: Number(this.goalHigh),
                }] };
                if (this.numeric(this.goalProfit)) goal.target_pct = Number(this.goalProfit);
                if (this.numeric(this.goalCash)) goal.cash_floor = Number(this.goalCash);
                return this.work('goal', async live => {
                    const result = await api('/enterprise/map/goal-seek', post({ ...this.selection, goal }));
                    if (live()) this.recommendations = result;
                });
            },
            useRecommendation(item) {
                this.edits = item.assumptions.map(row => this.makeEdit(row));
                this.feedback = 'اتجهّزت فرضيات التوصية. راجعها وأكّد حساب الأثر.';
                this.$refs.inputPanel.scrollIntoView({ block: 'nearest' });
            },
        };
        Object.defineProperties(page, Object.getOwnPropertyDescriptors(processFlowControls()));
        return mapBriefing(page);
    });
});
