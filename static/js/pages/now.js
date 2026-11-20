const uiMapName = value => String(value || '').normalize('NFKC').toLowerCase()
    .replace(/[\u064b-\u065f\u0670\u0640]/g, '').replace(/[أإآٱ]/g, 'ا').trim();

function mapEntity(map, value) {
    const key = uiMapName(value);
    if (!key) return null;
    const entities = [...(map?.departments || []), ...(map?.nodes || [])];
    return entities.find(row => uiMapName(row.id) === key) ||
        entities.find(row => uiMapName(row.name_ar) === key) || null;
}

function askEntityCandidates(question, reply) {
    return {
        keys: [...(reply.scenario?.assumptions || []).map(item => item.node_id),
            ...(reply.follow_up_metric ? [reply.follow_up_metric] : [])],
        question, answer: reply.reply_ar, modelId: reply.model_id, version: reply.version,
    };
}

const mapTopicAliases = {
    procurement: ['المورد', 'الموردين', 'المشتريات', 'التوريد', 'مورد'],
    operations: ['خط الانتاج', 'خطوط الانتاج', 'الانتاج', 'التشغيل'],
    workforce: ['المورد البشري', 'فرق العمل', 'الفريق', 'العمالة'],
    inventory: ['المخزون', 'التخزين', 'المخزن'],
    projects: ['المشروع', 'المشروعات', 'المشاريع'],
    sales: ['الطلبات', 'المنتج', 'المنتجات', 'المبيعات', 'الطلب'],
    finance: ['السيولة', 'الكاش', 'النقدية', 'الائتمان'],
    results: ['الربح', 'الارباح', 'الخسائر'],
};

function entityFromCandidates(map, {keys = [], question, answer}) {
    const rows = [...(map?.nodes || []), ...(map?.departments || [])];
    const words = value => uiMapName(value).replace(/[^\p{L}\p{N}]+/gu, ' ').trim();
    const mentions = (haystack, term) => term.length >= 3 && (' ' + haystack + ' ').includes(' ' + term + ' ');
    const match = value => rows.filter(row => mentions(words(value), words(row.name_ar)))
        .sort((a, b) => b.name_ar.length - a.name_ar.length)[0];
    const query = words(question);
    const aliases = Object.entries(mapTopicAliases).flatMap(([id, names]) =>
        names.filter(name => mentions(query, name)).map(name => ({id, name})))
        .sort((a, b) => b.name.length - a.name.length);
    return match(question) || keys.map(key => mapEntity(map, key)).find(Boolean) ||
        aliases.map(({id}) => mapEntity(map, id)).find(Boolean) || match(answer);
}

let mapFocusSequence = 0;
function sendMapFocus(store, candidates) {
    store.mapDemo = Boolean(candidates.demo);
    store.entityFocus = { ...candidates, companyId: store.companyId, sequence: ++mapFocusSequence };
    if (store.page !== 'map') location.hash = '#/map';
}

const nowChartColor = name => getComputedStyle(document.documentElement).getPropertyValue('--now-' + name).trim();
const compactNumber = new Intl.NumberFormat(locale, { notation: 'compact', maximumFractionDigits: 1 });
const nowAxis = { ticks: { callback: value => compactNumber.format(value), maxTicksLimit: 5 } };

function mountNowChart(canvas, makeSpec) {
    // The partial is injected and x-if inserts its canvases before layout.
    requestAnimationFrame(() => {
        if (canvas.isConnected && canvas.offsetWidth) drawChart(canvas, makeSpec, false);
    });
}

function verticalMarker(chart, axis, value, color, dash = []) {
    const {ctx, chartArea: area, scales} = chart;
    const x = scales[axis].getPixelForValue(value);
    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.setLineDash(dash);
    ctx.beginPath();
    ctx.moveTo(x, area.top);
    ctx.lineTo(x, area.bottom);
    ctx.stroke();
    ctx.restore();
}

const nowPartials = new Map();
function nowPartial(name) {
    if (!nowPartials.has(name)) {
        nowPartials.set(name, fetch('/static/partials/' + name + '.html').then(response => {
            if (!response.ok) throw new Error('تعذّر تحميل واجهة المؤشرات.');
            return response.text();
        }).catch(error => { nowPartials.delete(name); throw error; }));
    }
    return nowPartials.get(name);
}

document.addEventListener('alpine:init', () => {
    Alpine.data('nowPage', () => {
        let visit = 0, scope = '', mapRenderer, mapStage, opener;
        return {
            snapshot: null, preview: {departments: []}, mapReady: false, busy: false, error: '',
            forecastKey: '', markup: '', decisionMarkup: '', selected: null,
            number, signed, percent, animateValue, eligibilityLabel, eligibilityText,
            unit(value) { return mapUnits[value] || value; },
            statusLabel(status) {
                return {approved: 'معتمد', pending: 'مستني', rejected: 'مرفوض', infeasible: 'غير قابل للتنفيذ'}[status] || status;
            },
            sourceText(data, period = data?.period) {
                return [this.$store.app.company?.name, data?.source, period || 'الفترة غير متاحة'].filter(Boolean).join(' · ');
            },
            pointPeriod(kpi) {
                const first = kpi.points[0]?.[0], last = kpi.points.at(-1)?.[0];
                const keepDate = value => value?.replace(/\d{4}-\d{2}-\d{2}/g, date => '\u2066' + date.replaceAll('-', '‑') + '\u2069');
                return first && first !== last && first !== 'مرجعي' ? keepDate(first) + ' إلى ' + keepDate(last) : keepDate(kpi.period);
            },
            get forecastPoint() {
                return this.snapshot?.forecast?.series.find(row => row.key === this.forecastKey);
            },
            get previewCaption() {
                return this.sourceText({source: this.preview.model?.name || 'سجل العمليات',
                    period: this.preview.model?.version ? 'إصدار ' + this.preview.model.version : this.preview.process_mining?.updated_at?.slice(0, 10)});
            },
            change(kpi) { return executiveChange(kpi.before, kpi.value, kpi.lower_better); },
            refreshDecisions() {
                const token = ++visit;
                this.load(this.$store.app.companyId, token, 'prescriptive');
            },
            bandLabel(key) { return percentileLabel(key, this.snapshot.forecast.lower_better); },
            canFocus(key) { return !!mapEntity(this.preview, key); },
            canFocusDecision(item) {
                return Boolean(item.model_id && item.focus.length) || item.focus.some(key => this.canFocus(key));
            },
            openEntity(keys, model = {}) { sendMapFocus(this.$store.app, { keys, ...model }); },
            explain(row, button) {
                opener = button;
                this.selected = row;
                this.$nextTick(() => { if (!this.$refs.explanation.open) this.$refs.explanation.showModal(); });
            },
            returnFocus() { opener?.focus(); },
            activate(page, companyId, route = 'dashboard') {
                const key = page === route && companyId ? String(companyId) : '';
                if (key === scope) return;
                scope = key;
                const token = ++visit;
                this.resetMap();
                this.$root.querySelectorAll('.now-chart canvas, .now-spark canvas').forEach(canvas => Chart.getChart(canvas)?.destroy());
                if (!key) return;
                queueMicrotask(() => { if (token === visit) this.load(companyId, token, route); });
            },
            async load(companyId, token, route) {
                Object.assign(this, {snapshot: null, forecastKey: '', preview: {departments: []}, mapReady: false, error: '', busy: true});
                const lookup = this.$store.app.assistantMapModel;
                const mapURL = '/enterprise/map?company_id=' + companyId +
                    (lookup ? '&model_id=' + lookup.id + '&version=' + lookup.version : '');
                api(mapURL).then(map => {
                    if (token !== visit) return;
                    this.preview = {...map, departments: map.departments || []};
                }).catch(() => {}).finally(() => { if (token === visit) this.mapReady = true; });
                try {
                    const [snapshot, decisions, main] = await Promise.all([
                        api('/ui/companies/' + companyId + '/now'), nowPartial('now-decisions'),
                        route === 'dashboard' ? nowPartial('now') : '',
                    ]);
                    if (token !== visit) return;
                    Object.assign(this, { snapshot, decisionMarkup: decisions, markup: main,
                        forecastKey: snapshot.forecast?.series[0]?.key || '' });
                } catch (error) {
                    if (token === visit) this.error = error.message;
                } finally {
                    if (token === visit) this.busy = false;
                }
            },
            resetMap() {
                mapRenderer?.destroy();
                mapRenderer = mapStage = null;
            },
            destroy() { visit++; this.resetMap(); },
            previewValue(dept) {
                const node = this.preview.nodes?.find(row => row.id === dept.headline_id);
                return node?.baseline == null ? '' : mapValue(node.baseline, node.unit);
            },
            paintMap(element, map, theme) {
                requestAnimationFrame(() => {
                    if (!scope || !element.isConnected || !element.offsetWidth) return;
                    if (mapStage !== element) {
                        this.resetMap();
                        mapStage = element;
                        mapRenderer = createLivingMapCanvas(element);
                    }
                    const edges = new Map();
                    for (const road of map.flow_roads || []) {
                        const key = mapPair(road.from_department, road.to_department);
                        edges.set(key, {key, from: road.from_department, to: road.to_department, weight: road.weight});
                    }
                    for (const edge of map.flow_roads ? [] : map.edges || []) {
                        if (edge.status === 'draft' || edge.from_department === edge.to_department) continue;
                        const key = mapPair(edge.from_department, edge.to_department);
                        edges.set(key, {key, from: edge.from_department, to: edge.to_department,
                            weight: Math.max(edges.get(key)?.weight ?? 0, edge.weight)});
                    }
                    mapRenderer.update({edges: [...edges.values()], layout: map.model?.id, theme, effects: new Map()});
                });
            },
            paintSpark(canvas, kpi, index) {
                mountNowChart(canvas, tone => ({
                    type: 'line',
                    data: { labels: kpi.points.map(([date]) => date),
                        datasets: [{data: kpi.points.map(([, value]) => value),
                            borderColor: tone.palette[index], borderWidth: 2, tension: .34, pointRadius: 0}] },
                    options: { layout: {padding: 4}, events: [], scales: {x: {display: false}, y: {display: false}},
                        plugins: {legend: {display: false}} },
                }));
            },
            paintForecast(canvas, point) {
                const forecast = this.snapshot.forecast;
                const marker = {id: 'singleForecastRange', afterDatasetsDraw(chart) {
                    if (point.labels.length === 1) {
                        const {ctx, scales} = chart;
                        const x = scales.x.getPixelForValue(0);
                        ctx.save();
                        ctx.lineWidth = 12;
                        ctx.strokeStyle = nowChartColor('fan-fill');
                        ctx.beginPath();
                        ctx.moveTo(x, scales.y.getPixelForValue(point.p10[0]));
                        ctx.lineTo(x, scales.y.getPixelForValue(point.p90[0]));
                        ctx.stroke();
                        ctx.restore();
                    }
                }};
                mountNowChart(canvas, () => {
                    const edge = {borderColor: nowChartColor('fan'), borderWidth: 1.25, borderDash: [4, 4], pointRadius: 0};
                    return { type: 'line', ...(point.labels.length === 1 ? {plugins: [marker]} : {}),
                        data: { labels: point.labels, datasets: [
                            { ...edge, label: percentileLabel('p10', forecast.lower_better), data: point.p10, order: 2 },
                            { ...edge, label: percentileLabel('p90', forecast.lower_better), data: point.p90, order: 2,
                                backgroundColor: nowChartColor('fan-fill'), fill: '-1' },
                            { label: 'P50 · متوقع', data: point.p50, order: 1,
                                borderColor: nowChartColor('fan'), backgroundColor: nowChartColor('fan'),
                                pointRadius: 3, pointHoverRadius: 6, borderWidth: 2.5 },
                        ] },
                        options: {interaction: {mode: 'index', intersect: false},
                            scales: {x: {offset: true, grid: {display: false}}, y: {...nowAxis, title: {display: true, text: this.unit(forecast.unit)}}},
                            plugins: {legend: {display: false}} },
                    };
                });
            },
            paintSimulation(canvas, result) {
                const counts = new Map(result.bins.map(bin => [bin.bin, bin.count]));
                const low = result.bins[0].bin, high = result.bins.at(-1).bin;
                // Late orders/weeks are integer outcomes. Keep gaps on a numeric
                // axis and aggregate only for a very wide support.
                const width = Math.max(1, Math.ceil((high - low + 1) / 48));
                const bins = [];
                for (let start = low; start <= high; start += width) bins.push({x: start + (width - 1) / 2, y: 0});
                for (const [outcome, count] of counts) bins[Math.floor((outcome - low) / width)].y += count;
                const axisLow = low - .5, axisHigh = bins.at(-1).x + width / 2;
                mountNowChart(canvas, tone => {
                    const risk = nowChartColor('tail');
                    return {
                        type: 'bar', data: { datasets: [{ label: 'عدد التجارب', data: bins,
                            backgroundColor: bins.map(bin => bin.x + width / 2 - .5 >= result.cutoff ? risk : nowChartColor('hist')),
                            borderRadius: 3, categoryPercentage: 1, barPercentage: .88 }] },
                        plugins: [{ id: 'riskTail',
                            beforeDatasetsDraw(chart) {
                                const { ctx, chartArea: area, scales } = chart;
                                const x = scales.x.getPixelForValue(result.cutoff - .5);
                                ctx.save(); ctx.fillStyle = nowChartColor('tail-fill');
                                ctx.fillRect(x, area.top, area.right - x, area.height); ctx.restore();
                            },
                            afterDatasetsDraw(chart) {
                                verticalMarker(chart, 'x', result.p50, tone.ink);
                                verticalMarker(chart, 'x', result.cvar, risk, [5, 4]);
                            },
                        }],
                        options: { scales: {
                            x: { type: 'linear', offset: false, min: axisLow, max: axisHigh, grid: {display: false},
                                ticks: {...nowAxis.ticks, precision: 0, includeBounds: false},
                                title: { display: true, text: result.unit } },
                            y: { ...nowAxis, beginAtZero: true, ticks: {...nowAxis.ticks, precision: 0},
                                title: { display: true, text: 'عدد التجارب' } },
                        }, plugins: { legend: {display: false}, tooltip: {callbacks: {
                            title: ([item]) => width === 1 ? number(item.parsed.x) :
                                number(item.parsed.x - (width - 1) / 2) + '–' + number(item.parsed.x + (width - 1) / 2),
                        }} } },
                    };
                });
            },
            paintActual(canvas, result) {
                mountNowChart(canvas, tone => ({
                    type: 'bar', data: {labels: ['الفعلي'], datasets: [{data: [result.actual], label: 'الفعلي',
                        backgroundColor: nowChartColor('fan'), borderRadius: 7, barThickness: 14}]},
                    plugins: [{id: 'planTarget', afterDatasetsDraw: chart => verticalMarker(chart, 'x', result.plan, tone.ink)}],
                    options: {indexAxis: 'y', scales: {y: {display: false}, x: {...nowAxis, min: Math.min(result.actual, result.plan, 0) * 1.14,
                        max: Math.max(result.actual, result.plan, 1) * 1.14, grid: {display: false}}},
                        plugins: {legend: {display: false}} },
                }));
            },
        };
    });
});
