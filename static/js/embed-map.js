(() => {
    const byId = id => document.getElementById(id);
    const dataElement = byId('embed-data');
    const map = JSON.parse(dataElement.textContent);
    dataElement.remove();
    if (!map) return;

    function element(tag, value, className) {
        const node = document.createElement(tag);
        node.textContent = value;
        if (className) node.className = className;
        return node;
    }

    const format = new Intl.NumberFormat('ar-u-nu-latn', { maximumFractionDigits: 2 });
    const units = { SAR: 'ريال', 'SAR/unit': 'ريال/وحدة', units: 'وحدة', storage_units: 'وحدة تخزين',
        teams: 'فريق', days: 'يوم', weeks: 'أسبوع' };
    const value = (number, unit) => number === null ? 'غير متاح'
        : format.format(number) + ' ' + (units[unit] || unit);
    const themeButton = byId('embed-theme');
    const names = map.company.name === map.model.name ? map.company.name
        : map.company.name + ' · ' + map.model.name;
    byId('embed-company').textContent = names + ' · الإصدار ' + format.format(map.model.version);
    const kpis = byId('embed-kpis');
    for (const kpi of map.kpis) {
        const metric = document.createElement('div');
        const display = element('dd', value(kpi.baseline, kpi.unit));
        if (kpi.source === 'estimated') display.append(element('small', ' · مقدّر'));
        metric.append(element('dt', kpi.label), display);
        kpis.append(metric);
    }
    const nodes = new Map(map.nodes.map(node => [node.id, node]));
    const departments = byId('embed-departments');
    for (const department of map.departments) {
        const node = nodes.get(department.headline_id);
        const card = document.createElement('article');
        card.className = 'lm-department';
        card.dataset.department = department.id;
        card.append(element('h2', department.name_ar, 'lm-node-label'),
            element('strong', value(node.baseline, node.unit), 'lm-node-value'),
            element('small', node.name_ar + (node.source === 'estimated' ? ' · مقدّر' : ''), 'lm-node-measure'));
        departments.append(card);
    }

    const roads = new Map();
    for (const edge of map.edges) {
        const from = edge.from_department, to = edge.to_department;
        if (from === to) continue;
        const key = from + ':' + to;
        const weight = Math.max(roads.get(key)?.weight ?? 0, edge.weight);
        roads.set(key, { from, to, key, weight });
    }
    byId('embed-content').hidden = false;
    themeButton.hidden = false;
    let renderer;
    function paint() {
        renderer ??= createLivingMapCanvas(byId('embed-stage'));
        renderer.update({ edges, theme: document.documentElement.dataset.theme || 'light', effects: new Map() });
    }
    const edges = [...roads.values()];
    themeButton.addEventListener('click', () => {
        const dark = document.documentElement.dataset.theme !== 'dark';
        document.documentElement.dataset.theme = dark ? 'dark' : 'light';
        themeButton.textContent = dark ? 'الوضع الرملي' : 'الوضع الليلي';
        paint();
    });
    addEventListener('pagehide', () => { renderer?.destroy(); renderer = null; });
    addEventListener('pageshow', event => { if (event.persisted) paint(); });
    paint();
})();
