document.addEventListener('alpine:init', () => {
    Alpine.data('forecastPage', () => workspacePage('forecast', () => ({ forecast: null, selected: '' }), {
        methodLabel(method) {
            return { lognormal: 'لوغاريتمي طبيعي', lognormal_loo: 'لوغاريتمي طبيعي',
                croston_sba: 'كروستون', holt_damped: 'هولت المخمّد' }[method] || 'تقدير المدة';
        },
        async load(base, live) {
            const result = await api(base + '/forecast/latest?allow_missing=true');
            if (live()) this.setForecast(result);
        },
        generate() {
            this.perform('forecast', async (base, live) => {
                const result = await api(base + '/forecast', post({}));
                if (live()) this.setForecast(result);
            });
        },
        setForecast(result) {
            this.$store.app.forecast = this.forecast = result;
            this.selected = '';
            this.draw();
        },
        draw() {
            const result = this.forecast;
            const selected = this.selected;
            const company = this.$store.app.companyId;
            this.$nextTick(() => {
                if (this.$store.app.companyId === company && this.$store.app.page === 'forecast' && result?.series.length) {
                    forecastChart(this.$refs.forecast, result, selected);
                }
            });
        },
    }));
});
