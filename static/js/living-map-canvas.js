/* Canvas roads follow the DOM cards, so the same graph can reflow on a phone. */
function createLivingMapCanvas(stage) {
    const canvas = stage.querySelector('canvas');
    const ctx = canvas.getContext('2d');
    const reduced = matchMedia('(prefers-reduced-motion: reduce)');
    let frame = 0, width = 0, height = 0, roads = [], view = {}, colors = {};
    let geometryDirty = true, started = performance.now();
    let contours = new Path2D();
    const limit = (n, min, max) => Math.max(min, Math.min(max, n));

    function trace(points) {
        const path = new Path2D();
        path.moveTo(...points[0]);
        for (const point of points.slice(1)) path.lineTo(...point);
        return path;
    }

    // Round the corners of the lanes and sample by distance for constant-speed comets.
    function roundRoute(vertices) {
        const points = [vertices[0]];
        for (let i = 1; i < vertices.length - 1; i++) {
            const [a, b, c] = vertices.slice(i - 1, i + 2);
            const toward = end => {
                const distance = Math.hypot(end[0] - b[0], end[1] - b[1]);
                const t = distance ? Math.min(.38, 27 / distance) : 0;
                return b.map((value, axis) => value + (end[axis] - value) * t);
            };
            const first = toward(a), last = toward(c);
            for (let sample = 0; sample <= 7; sample++) {
                const t = sample / 7, u = 1 - t;
                points.push(b.map((value, axis) => u * u * first[axis] + 2 * u * t * value + t * t * last[axis]));
            }
        }
        points.push(vertices.at(-1));
        const lengths = [0];
        for (let i = 1; i < points.length; i++) {
            lengths.push(lengths[i - 1] + Math.hypot(points[i][0] - points[i - 1][0], points[i][1] - points[i - 1][1]));
        }
        return { points, lengths, path: trace(points) };
    }

    function position(road, t) {
        const target = limit(t, 0, 1) * road.lengths.at(-1);
        let i = 1;
        while (i < road.lengths.length - 1 && road.lengths[i] < target) i++;
        const start = road.lengths[i - 1], end = road.lengths[i];
        const ratio = end > start ? (target - start) / (end - start) : 0;
        const [a, b] = road.points.slice(i - 1, i + 1);
        return a.map((value, axis) => value + ratio * (b[axis] - value));
    }

    function measure() {
        geometryDirty = false;
        const bounds = stage.getBoundingClientRect();
        width = bounds.width;
        height = bounds.height;
        if (!width || !height) return;
        const ratio = Math.min(2, devicePixelRatio || 1);
        canvas.width = Math.round(width * ratio);
        canvas.height = Math.round(height * ratio);
        ctx.setTransform(ratio, 0, 0, ratio, 0, 0);

        const cards = new Map([...stage.querySelectorAll('[data-department]')].map(element => [
            element.dataset.department,
            { x: element.offsetLeft, y: element.offsetTop, w: element.offsetWidth, h: element.offsetHeight },
        ]));
        const lanes = new Map();
        roads = view.edges.flatMap((edge, index) => {
            const a = cards.get(edge.from), b = cards.get(edge.to);
            if (!a?.w || !b?.w) return [];
            const ax = a.x + a.w / 2, bx = b.x + b.w / 2;
            const ay = a.y + a.h / 2, by = b.y + b.h / 2;
            const pair = [edge.from, edge.to].sort().join(':');
            const slot = lanes.get(pair) || 0;
            lanes.set(pair, slot + 1);
            const offset = slot % 2 ? 6 : -6;
            let vertices;
            if (ay === by && Math.abs(ax - bx) < a.w * 2) {
                const direction = Math.sign(bx - ax);
                const lane = (ax + bx) / 2 + offset;
                vertices = [[ax + direction * a.w / 2, ay + offset], [lane, ay + offset],
                            [lane, by + offset], [bx - direction * b.w / 2, by + offset]];
            } else if (Math.abs(a.y - b.y) <= a.h * 2 && ay !== by) {
                const direction = Math.sign(by - ay);
                const lane = (ay + by) / 2 + offset;
                vertices = [[ax + offset, ay + direction * a.h / 2], [ax + offset, lane],
                            [bx + offset, lane], [bx + offset, by - direction * b.h / 2]];
            } else {
                const direction = bx >= width / 2 ? 1 : -1;
                const lane = direction === 1 ? width - 12 - slot * 6 : 12 + slot * 6;
                const y1 = a.y - 12 - slot * 7, y2 = b.y - 12 - slot * 7;
                vertices = [[ax + offset, a.y], [ax + offset, y1], [lane, y1], [lane, y2],
                            [bx + offset, y2], [bx + offset, b.y]];
            }
            return [{ ...edge, ...roundRoute(vertices), index }];
        });
        stage.dataset.roadCount = roads.length;

        // Rebuild terrain only on resize. The irregular contours echo the prototype.
        contours = new Path2D();
        for (let ring = 1; ring < 17; ring++) {
            for (let i = 0; i <= 84; i++) {
                const angle = i / 84 * Math.PI * 2;
                const radius = ring / 15 * (1 + .08 * Math.sin(4 * angle) + .065 * Math.cos(5 * angle));
                const x = width * (.51 + Math.cos(angle) * radius * .7);
                const y = height * (.47 + Math.sin(angle) * radius * .68);
                if (i) contours.lineTo(x, y);
                else contours.moveTo(x, y);
            }
        }
    }

    function recolor() {
        const style = getComputedStyle(stage);
        colors = Object.fromEntries(['road', 'road-core', 'terrain', 'glow', 'surge', 'alert']
            .map(key => [key, style.getPropertyValue('--lm-' + key).trim()]));
    }

    function dot(x, y, radius, color, glow = 0) {
        ctx.shadowBlur = glow;
        ctx.shadowColor = color;
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.arc(x, y, radius, 0, Math.PI * 2);
        ctx.fill();
        ctx.shadowBlur = 0;
    }

    function draw(now) {
        frame = 0;
        if (geometryDirty) measure();
        if (!width || !height || document.hidden) return;
        const elapsed = (now - started) / 1000;
        ctx.clearRect(0, 0, width, height);
        ctx.strokeStyle = colors.terrain;
        ctx.lineWidth = 1;
        ctx.stroke(contours);
        const intro = reduced.matches ? 1 : limit(elapsed / .65, 0, 1);
        for (const road of roads) {
            const focused = !view.focus || view.focus === road.from || view.focus === road.to;
            const lineWidth = 2 + road.weight * 4;
            ctx.globalAlpha = intro * (focused ? .95 : .24);
            ctx.lineCap = 'round';
            ctx.strokeStyle = colors.glow;
            ctx.lineWidth = lineWidth + 6;
            ctx.stroke(road.path);
            ctx.strokeStyle = colors.road;
            ctx.lineWidth = lineWidth;
            ctx.stroke(road.path);
            ctx.strokeStyle = colors['road-core'];
            ctx.lineWidth = 1;
            ctx.setLineDash([2, 6]);
            ctx.stroke(road.path);
            ctx.setLineDash([]);

            const active = view.effects.get(road.key);
            if (active) {
                const tone = active.adverse ? colors.alert : colors.surge;
                ctx.globalAlpha = active.progress >= 1 ? .85 : 1;
                const lead = position(road, active.progress);
                const trail = road.points.filter((_, i) => road.lengths[i] < active.progress * road.lengths.at(-1));
                if (trail.length) {
                    ctx.strokeStyle = tone;
                    ctx.lineWidth = lineWidth + 2.5;
                    ctx.stroke(trace([...trail, lead]));
                }
                if (active.progress < 1 && !reduced.matches) {
                    dot(...lead, 6, tone, 12);
                    dot(...lead, 2.3, colors['road-core']);
                }
                if (active.pulse && !reduced.matches) {
                    const [x, y] = position(road, 1);
                    ctx.globalAlpha = .8 * (1 - active.pulse);
                    ctx.strokeStyle = tone;
                    ctx.lineWidth = 2;
                    ctx.beginPath();
                    ctx.arc(x, y, 5 + active.pulse * 24, 0, Math.PI * 2);
                    ctx.stroke();
                }
            }

            ctx.globalAlpha = focused ? .8 : .25;
            const t = .72, [x, y] = position(road, t), [nextX, nextY] = position(road, t + .02);
            ctx.save();
            ctx.translate(x, y);
            ctx.rotate(Math.atan2(nextY - y, nextX - x));
            ctx.fillStyle = colors['road-core'];
            ctx.fill(trace([[4, 0], [-3, -2.8], [-1, 0], [-3, 2.8], [4, 0]]));
            ctx.restore();

            if (!reduced.matches && focused && !active) {
                const progress = (elapsed * (.17 + .11 * road.weight) + road.index * .37) % 1;
                dot(...position(road, progress), 2.2, colors['road-core'], 5);
            }
        }
        ctx.globalAlpha = 1;
        if (!reduced.matches) schedule();
    }

    function schedule() {
        if (!frame) frame = requestAnimationFrame(draw);
    }

    const resize = new ResizeObserver(() => { geometryDirty = true; schedule(); });
    resize.observe(stage);
    const onVisibility = () => { if (!document.hidden) schedule(); };
    document.addEventListener('visibilitychange', onVisibility);
    reduced.addEventListener('change', schedule);

    return {
        update(next) {
            if (next.edges !== view.edges || next.layout !== view.layout) {
                geometryDirty = true;
                if (next.edges !== view.edges) started = performance.now();
            }
            if (next.theme !== view.theme) recolor();
            view = next;
            schedule();
        },
        destroy() {
            cancelAnimationFrame(frame);
            resize.disconnect();
            document.removeEventListener('visibilitychange', onVisibility);
            reduced.removeEventListener('change', schedule);
        },
    };
}
