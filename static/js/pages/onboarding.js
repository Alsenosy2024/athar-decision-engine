document.addEventListener('alpine:init', () => {
    Alpine.data('onboardingPage', () => ({
        markup: '', sectors: [], model: null, connectors: [], sectionIndex: 0, mode: 'manual',
        creating: false, active: '', visit: 0, busy: '', error: '', message: '', review: null, rows: [],
        file: null, connectorId: '', name: '', sector: 'trading', rules: {},
        number,
        get admin() { return this.$store.app.user?.role === 'admin'; },
        get editable() { return ['admin', 'planner'].includes(this.$store.app.user?.role); },
        get section() { return this.model?.readiness.sections[this.sectionIndex]; },
        get fields() { return this.model?.readiness.fields[this.sectionIndex]; },
        get path() { return '/companies/' + this.model.company_id + '/onboarding'; },
        get templateUrl() {
            return '/api/onboarding/templates/' + this.model.sector + '/workbook'
                + (this.mode === 'full' ? '' : '?section=' + this.section.id);
        },
        get validRows() { return this.rows.filter(row => String(row.value ?? '').trim() !== ''); },
        source(value) { return {actual: 'فعلي', estimated: 'مقدّر', missing: 'ناقص'}[value] || '—'; },
        nodeLabel(key) { return this.model?.nodes.find(node => node.id === key)?.name_ar || key; },
        activate(page, companyId, loading) {
            const active = page === 'onboarding' && !loading ? String(companyId || 'new') : '';
            if (this.active === active) return;
            this.active = active;
            this.visit++;
            this.model = this.review = null;
            this.creating = false;
            this.busy = this.error = this.message = '';
            this.sectionIndex = 0;
            this.mode = 'manual';
            if (active) queueMicrotask(() => { if (this.active === active) this.load(); });
        },
        async work(action, run) {
            if (this.busy) return;
            const visit = this.visit;
            const live = () => visit === this.visit;
            this.busy = action;
            this.error = this.message = '';
            try { await run(live); }
            catch (error) { if (live()) this.error = error.message; }
            finally { if (live()) this.busy = ''; }
        },
        load() {
            return this.work('load', async live => {
                const app = this.$store.app;
                const [markup, sectors, model, connectors] = await Promise.all([
                    this.markup || fetch('/static/partials/onboarding.html').then(response => {
                        if (!response.ok) throw new Error('تعذّر تحميل تجهيز الشركة.');
                        return response.text();
                    }),
                    api('/onboarding/templates'),
                    app.isOnboardingCompany ? api('/companies/' + app.companyId + '/onboarding') : null,
                    this.admin && app.isOnboardingCompany ? api('/companies/' + app.companyId + '/connectors/erp') : [],
                ]);
                if (!live()) return;
                Object.assign(this, { markup, sectors, model, connectors });
                if (model) this.selectSection(0);
            });
        },
        create() {
            return this.work('create', async live => {
                const model = await api('/onboarding/companies', post({name: this.name, sector: this.sector}));
                if (!live()) return;
                this.name = '';
                await this.$store.app.loadCompanies(model.company_id);
            });
        },
        selectSection(index) {
            this.sectionIndex = index;
            this.mode = 'manual';
            this.resetInputs();
        },
        resetInputs() {
            this.review = this.file = null;
            this.error = this.message = this.connectorId = '';
            if (this.$refs.file) this.$refs.file.value = '';
            if (!this.model) return;
            this.rows = this.fields.fields.map(field => {
                const stored = this.model.records.filter(record => record.key === field.key).at(-1);
                return { ...field, value: stored?.value ?? '', date: stored?.date ?? '',
                    source: stored?.source || 'actual', entry: false };
            });
            if (this.fields.item) {
                const [prefix, label] = this.fields.item;
                this.rows.push(...this.model.records.filter(record => record.key.startsWith(prefix + '.'))
                    .map(record => ({ ...record, label, date: '', entry: true, stored: true })));
            }
            this.rules = Object.fromEntries(this.model.edges.map(edge => [
                edge.source + ':' + edge.target, edge.rule.multiplier,
            ]));
        },
        addItem() {
            this.rows.push({key: '', value: '', date: '', source: 'actual', label: this.fields.item[1], entry: true});
        },
        selectMode(mode) {
            this.mode = mode;
            this.resetInputs();
        },
        preview(suffix, options) {
            this.review = null;
            return this.work('preview', async live => {
                const review = await api(this.path + suffix, options);
                if (!live()) return;
                this.review = review;
                if (review.validation.ok && !review.token) this.message = 'مفيش تغيير عن الإصدار الحالي.';
                await this.$nextTick();
                if (live() && review.token) this.$refs.review?.focus();
            });
        },
        previewManual() {
            const records = this.validRows.map(({key, value, date, source, entry, stored}) => ({
                key: entry && !stored ? this.fields.item[0] + '.' + key : key,
                value, date: date || null, source,
            }));
            return this.preview('/sections/' + this.section.id + '/manual/preview',
                post({expected_version: this.model.version, records}));
        },
        previewExcel() {
            const data = new FormData();
            data.append('file', this.file);
            data.append('expected_version', this.model.version);
            if (this.mode === 'excel') data.append('section', this.section.id);
            return this.preview('/excel/preview', {method: 'POST', body: data});
        },
        previewConnector() {
            return this.preview('/sections/' + this.section.id + '/erp/' + this.connectorId + '/preview',
                post({expected_version: this.model.version}));
        },
        previewRules() {
            return this.preview('/rules/preview', post({
                expected_version: this.model.version,
                multipliers: Object.fromEntries(Object.entries(this.rules).map(([key, value]) => [key, Number(value)])),
            }));
        },
        async commit() {
            return this.work('commit', async live => {
                const model = await api(this.path + '/commit', post({token: this.review.token}));
                if (!live()) return;
                this.model = model;
                const company = this.$store.app.company;
                if (company?.id === model.company_id) company.model_version = model.version;
                this.resetInputs();
                this.message = 'اتحفظ الإصدار ' + number(model.version) + '.';
            });
        },
        openImpact() {
            this.$store.app.onboardingImpactModel = this.model.model_id;
        },
    }));
});
