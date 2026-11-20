const chartSpecs = new WeakMap();
const chartFont = 'Thmanyah Serif Text';

document.addEventListener('DOMContentLoaded', () => {
    Chart.defaults.font.family = chartFont;
    Chart.defaults.font.weight = '700';
    Chart.defaults.font.size = 13;
    Chart.defaults.locale = locale;
    for (const plugin of [Chart.defaults.plugins.legend, Chart.defaults.plugins.tooltip]) {
        plugin.rtl = true;
        plugin.textDirection = 'rtl';
    }
});

function colors() {
    const css = getComputedStyle(document.documentElement);
    const read = key => css.getPropertyValue('--' + key).trim();
    return {
        ink: read('ink'), muted: read('muted'), line: read('line'), paper: read('paper'),
        main: read('chart-main'), reference: read('chart-reference'), band: read('chart-band'),
        danger: read('chart-danger'), palette: Array.from({ length: 6 }, (_, i) => read('series-' + (i + 1))),
    };
}

function recolorCharts() {
    document.querySelectorAll('canvas').forEach(canvas => {
        const spec = chartSpecs.get(canvas);
        if (spec && canvas.offsetWidth) drawChart(canvas, spec);
    });
}

function drawChart(canvas, spec) {
    if (!canvas) return;
    chartSpecs.set(canvas, spec);
    const tone = colors();
    const config = spec(tone);
    const options = config.options || {};
    Chart.getChart(canvas)?.destroy();
    const chart = new Chart(canvas, {
        ...config,
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: false,
            color: tone.ink,
            ...options,
            scales: Object.fromEntries(Object.entries(options.scales || { x: {}, y: {} }).map(([axis, scale]) => [
                axis, {
                    ...scale,
                    grid: { color: tone.line, ...scale.grid },
                    ticks: { color: tone.muted, padding: 5, maxRotation: 0, ...scale.ticks },
                    title: { color: tone.muted, ...scale.title },
                },
            ])),
            plugins: {
                legend: {
                    position: 'bottom', ...options.plugins?.legend,
                    labels: { color: tone.ink, boxWidth: 12, boxHeight: 12, padding: 16, ...options.plugins?.legend?.labels },
                },
                tooltip: options.plugins?.tooltip || {},
            },
        },
    });
    document.fonts.load('700 13px "' + chartFont + '"', 'أسبوع').then(() => {
        if (Chart.getChart(canvas) === chart && canvas.offsetWidth) chart.update('none');
    });
}

const axisTitle = text => ({ display: true, text });
const reverseTime = title => ({
    reverse: true, offset: true, title: axisTitle(title), grid: { display: false }, ticks: { maxTicksLimit: 16 },
});

function comparisonChart(canvas, labels, before, after, legends, unit) {
    drawChart(canvas, c => ({
        type: 'bar',
        data: { labels, datasets: [
            { label: legends[0], data: before, backgroundColor: c.reference, borderRadius: 3, maxBarThickness: 38 },
            { label: legends[1], data: after, backgroundColor: c.main, borderRadius: 3, maxBarThickness: 38 },
        ] },
        options: {
            scales: {
                x: { reverse: true, grid: { display: false }, ticks: { callback(value) {
                    const label = this.getLabelForValue(value) ?? '';
                    if (this.chart.width < 480) return label.split(' ');
                    return label.length > 14 ? label.split(' ').reduce((lines, word) => {
                        if (lines.at(-1).length + word.length < 14) lines[lines.length - 1] += ' ' + word;
                        else lines.push(word);
                        return lines;
                    }, ['']).filter(Boolean) : label;
                } } },
                y: { beginAtZero: true, title: axisTitle(unit) },
            },
        },
    }));
}

function ganttChart(canvas, activities, projects) {
    const groups = Object.fromEntries(projects.map((project, index) => [project.id, { ...project, index }]));
    const rows = [...activities].sort((a, b) => (groups[a.project]?.index ?? 0) - (groups[b.project]?.index ?? 0)
        || a.start_week - b.start_week);
    const maxWeek = Math.max(...rows.map(row => row.finish_week), ...projects.map(row => row.deadline_week));
    const deadlineTicks = {
        id: 'deadlineTicks',
        afterDatasetsDraw(chart) {
            const { ctx, scales, chartArea } = chart;
            ctx.save();
            ctx.strokeStyle = colors().danger;
            ctx.lineWidth = 2;
            for (const [index, row] of rows.entries()) {
                if (!groups[row.project] || rows[index + 1]?.project === row.project) continue;
                const left = scales.x.getPixelForValue(groups[row.project].deadline_week);
                const top = scales.y.getPixelForValue(index);
                if (left < chartArea.left || left > chartArea.right) continue;
                ctx.beginPath();
                ctx.moveTo(left, top - 8);
                ctx.lineTo(left, top + 8);
                ctx.stroke();
            }
            ctx.restore();
        },
    };
    drawChart(canvas, c => ({
        type: 'bar', plugins: [deadlineTicks],
        data: {
            labels: rows.map(row => row.activity_id),
            datasets: [{ data: rows.map(row => [row.start_week, row.finish_week]),
                backgroundColor: rows.map(row => c.palette[(groups[row.project]?.index || 0) % c.palette.length]),
                borderRadius: 3, barThickness: 13 }],
        },
        options: {
            indexAxis: 'y',
            scales: { x: { min: 0, suggestedMax: maxWeek + 1, reverse: true, title: axisTitle('الأسبوع'), ticks: { precision: 0 } },
                y: { position: 'right', grid: { display: false }, ticks: { autoSkip: false } } },
            plugins: { legend: { display: false }, tooltip: { callbacks: {
                title: items => {
                    const row = rows[items[0].dataIndex];
                    return (groups[row.project]?.name_ar || row.project) + ' · ' + row.activity_id;
                },
                label: item => {
                    const row = rows[item.dataIndex];
                    return 'الأسبوع ' + number(row.start_week) + ' إلى ' + number(row.finish_week) +
                        ' · التسليم ' + number(groups[row.project]?.deadline_week);
                },
            } } },
        },
    }));
}

function productionChart(canvas, schedule, days, dataset) {
    const lines = dataset.lines;
    const products = dataset.skus.filter(sku => schedule.some(row => row.sku === sku.id));
    drawChart(canvas, c => ({
        type: 'bar',
        data: {
            labels: lines.map(line => line.name_ar || line.id),
            datasets: products.map((sku, index) => ({
                label: sku.name_ar || sku.id,
                data: schedule.filter(row => row.sku === sku.id).map(row => ({
                    x: [row.day - .44, row.day + .44], y: lines.findIndex(line => line.id === row.line), row,
                })),
                backgroundColor: c.palette[index % c.palette.length],
                grouped: false, barThickness: 30, borderRadius: 3,
            })),
        },
        options: {
            indexAxis: 'y',
            scales: {
                x: { min: -.5, max: days - .5, reverse: true, title: axisTitle('يوم التشغيل'),
                    afterBuildTicks(scale) {
                        const tickLimit = scale.chart.width < 480 ? 9 : 20;
                        const interval = Math.max(1, Math.ceil(days / tickLimit));
                        scale.ticks = Array.from({ length: Math.ceil(days / interval) }, (_, i) => ({ value: i * interval }));
                    },
                    ticks: { autoSkip: false, callback: number } },
                y: { position: 'right', grid: { display: false } },
            },
            plugins: { tooltip: { callbacks: {
                title: items => 'اليوم ' + number(items[0].raw.row.day) + ' · ' + items[0].label,
                label: item => item.dataset.label + ' · ' + number(item.raw.row.qty) + ' وحدة',
            } } },
        },
    }));
}

function cashChart(canvas, before = [], after = [], template, unit) {
    drawChart(canvas, c => ({
        type: 'line',
        data: {
            labels: Array.from({ length: Math.max(before.length, after.length) }, (_, index) => number(index)),
            datasets: [
                { label: 'الخطة المرجعية', data: before, borderColor: c.reference, borderDash: [6, 4], pointRadius: 0 },
                { label: 'الخطة المحسّنة', data: after, borderColor: c.main, backgroundColor: c.band, fill: 'origin', pointRadius: 0 },
            ],
        },
        options: {
            interaction: { mode: 'index', intersect: false },
            scales: { x: reverseTime(template === 'contracting' ? 'الأسبوع' : 'اليوم'), y: { title: axisTitle(unit) } },
        },
    }));
}

function forecastChart(canvas, forecast, key) {
    const series = forecast.series.find(item => item.key === key);
    drawChart(canvas, c => {
        if (forecast.method === 'lognormal') {
            const rows = series ? [series] : forecast.series;
            return { type: 'bar', data: {
                labels: rows.map(row => row.key),
                datasets: [['p10', c.reference], ['p50', c.main], ['p90', c.palette[2]]].map(([quantile, color]) => ({
                    label: quantile.toUpperCase(), data: rows.map(row => row[quantile][0]),
                    backgroundColor: color, maxBarThickness: 35, borderRadius: 3,
                })),
            }, options: { scales: { x: reverseTime('النشاط'), y: { beginAtZero: true, title: axisTitle('أسبوع') } } } };
        }
        const sku = series || forecast.series[0];
        const historyWeeks = sku.history_weeks;
        const end = historyWeeks.at(-1) || 0;
        const pad = Array(Math.max(0, sku.history.length - 1)).fill(null);
        const projected = band => sku.history.length ? [...pad, sku.history.at(-1), ...sku[band]] : sku[band];
        return { type: 'line', data: {
            labels: [...historyWeeks, ...sku.p50.map((_, i) => end + i + 1)].map(number),
            datasets: [
                { label: 'الطلب الفعلي', data: sku.history, borderColor: c.reference, pointRadius: 0 },
                { label: 'P90', data: projected('p90'), borderColor: c.main, borderWidth: 1, pointRadius: 0, fill: '+1', backgroundColor: c.band },
                { label: 'P10', data: projected('p10'), borderColor: c.main, borderWidth: 1, pointRadius: 0 },
                { label: 'P50', data: projected('p50'), borderColor: c.main, pointRadius: 3, borderDash: [5, 3] },
            ],
        }, options: {
            interaction: { mode: 'index', intersect: false },
            scales: { x: reverseTime('الأسبوع'), y: { beginAtZero: true, title: axisTitle('وحدة') } },
        } };
    });
}

function histogramChart(canvas, risk) {
    const series = risk.late_weeks || risk.late_orders;
    drawChart(canvas, c => ({
        type: 'bar',
        data: {
            datasets: [{ label: 'عدد المحاكاة', data: series.hist.map(item => ({ x: item.bin, y: item.count })),
                backgroundColor: c.main, maxBarThickness: 40, borderRadius: 3 }],
        },
        options: { plugins: { legend: { display: false } },
            scales: { x: { ...reverseTime(risk.late_weeks ? 'إجمالي أسابيع التأخير' : 'عدد الطلبات المتأخرة'),
                type: 'linear', ticks: { maxTicksLimit: 10, precision: 0 } },
                y: { beginAtZero: true, title: axisTitle('عدد المحاكاة'), ticks: { precision: 0 } } } },
    }));
}
