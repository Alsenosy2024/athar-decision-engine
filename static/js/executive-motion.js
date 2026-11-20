const executiveMotion = {
    duration: 780,
    reduced: matchMedia('(prefers-reduced-motion: reduce)'),
    ease: progress => 1 - (1 - progress) ** 3,
};

const animatedValues = new WeakMap();

function animateValue(element, target, format = number) {
    const previous = animatedValues.get(element);
    if (previous && Object.is(previous.target, target)) return;
    if (previous) cancelAnimationFrame(previous.frame);

    const from = Number.isFinite(previous?.value) ? previous.value : Number.isFinite(target) ? 0 : target;
    const state = { target, value: from, frame: 0 };
    animatedValues.set(element, state);
    const write = value => {
        state.value = value;
        element.textContent = format(value);
    };
    if (!Number.isFinite(from) || !Number.isFinite(target) || from === target || executiveMotion.reduced.matches) {
        write(target);
        return;
    }

    write(from);
    const started = performance.now();
    const tick = now => {
        if (!element.isConnected) return;
        const progress = executiveMotion.reduced.matches ? 1 : Math.min(1, (now - started) / executiveMotion.duration);
        write(progress === 1 ? target : from + (target - from) * executiveMotion.ease(progress));
        if (progress < 1) state.frame = requestAnimationFrame(tick);
    };
    state.frame = requestAnimationFrame(tick);
}

function executiveChange(before, after, higherIsWorse = false) {
    if (!Number.isFinite(before) || !Number.isFinite(after)) {
        return { tone: 'idle', text: 'مفيش فترة للمقارنة', arrow: '' };
    }
    const delta = after - before;
    const ratio = before ? delta / Math.abs(before) : delta === 0 ? 0 : null;
    const text = ratio == null ? signed(delta) + ' من صفر' : (ratio > 0 ? '+' : '') + percent(ratio);
    return {
        tone: delta === 0 ? 'idle' : (delta > 0) !== higherIsWorse ? 'positive' : 'critical',
        arrow: delta === 0 ? '→' : delta > 0 ? '↑' : '↓',
        text,
    };
}

function percentileLabel(band, higherIsWorse = false) {
    const low = higherIsWorse ? 'متفائل' : 'متشائم';
    const high = higherIsWorse ? 'متشائم' : 'متفائل';
    return band.toUpperCase() + ' · ' + { p10: low, p50: 'متوقع', p90: high }[band];
}
