document.addEventListener('alpine:init', () => {
    Alpine.data('onboardingPage', () => ({
        markup: '', sectors: [], model: null, connectors: [], sectionIndex: 0, mode: 'manual',
        creating: false, active: '', visit: 0, busy: '', error: '', message: '', review: null, rows: [],
        file: null, connectorId: '', name: '', sector: 'enterprise_company', rules: {},
        rawFiles: [], rawSheet: '', rawSheets: [], rawConnectorId: '', rawReview: null, rawReviews: [], mappingsDirty: false,
        rawDimensions: {branch: 'الفرع', department: 'القسم', account: 'الحساب',
            cost_center: 'مركز التكلفة', product: 'المنتج', supplier: 'المورد'},
        number,
        get raw() { return this.model?.sector === 'enterprise_company'; },
        get rawSource() { return this.rawFiles.length || this.rawConnectorId; },
        get rawScope() { return '/companies/' + this.model.company_id + '/ai'; },
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
        reviewNodeName(key) {
            return this.rawReview?.data.model?.graph.nodes.find(node => node.id === key)?.name_ar || key;
        },
        nodeLabel(key) { return this.model?.nodes.find(node => node.id === key)?.name_ar || key; },
        activate(page, companyId, loading) {
            const active = page === 'onboarding' && !loading ? String(companyId || 'new') : '';
            if (this.active === active) return;
            this.active = active;
            this.visit++;
            this.model = this.review = this.rawReview = null;
            this.rawFiles = this.rawSheets = this.rawReviews = [];
            this.rawSheet = this.rawConnectorId = '';
            this.mappingsDirty = false;
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
                if (this.raw) {
                    const requestedReview = new URLSearchParams(location.search).get('review_id');
                    if (!this.admin) {
                        const saved = await api('/companies/' + model.company_id + '/connectors/erp');
                        if (!live()) return;
                        this.connectors = saved;
                    }
                    await this.loadRawReviews(live);
                    if (live() && requestedReview && /^\d+$/.test(requestedReview)) {
                        await this.readRawReview(requestedReview, live);
                    }
                } else if (model) {
                    this.selectSection(0);
                }
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
        async loadRawReviews(live) {
            const reviews = await api(this.rawScope + '/reviews');
            if (live()) this.rawReviews = reviews.filter(review => review.kind === 'entry');
        },
        async readRawReview(id, live) {
            const review = await api(this.rawScope + '/reviews/' + id);
            if (!live() || !review.data.company_import) return;
            this.rawFiles = this.rawSheets = [];
            this.rawSheet = this.rawConnectorId = '';
            if (this.$refs.rawFile) this.$refs.rawFile.value = '';
            this.rawReview = review;
            this.mappingsDirty = false;
            await this.$nextTick();
            if (live()) this.$refs.rawReview?.focus();
        },
        selectRawFiles(files) {
            this.rawConnectorId = '';
            this.rawFiles = [...files];
            this.rawReview = null;
            this.rawSheet = '';
            this.rawSheets = [];
            this.mappingsDirty = false;
            if (files.length !== 1 || !/\.xlsx$/i.test(files[0].name)) return;
            return this.work('inspect', async live => {
                const data = new FormData();
                data.append('file', files[0]);
                const result = await api('/enterprise/imports/inspect', {method: 'POST', body: data});
                if (!live()) return;
                this.rawSheets = result.sheets.filter(sheet => sheet.columns.length);
                if (this.rawSheets.length === 1 && result.sheets.length > 1) this.rawSheet = this.rawSheets[0].name;
            });
        },
        selectRawConnector() {
            this.rawFiles = this.rawSheets = [];
            this.rawSheet = '';
            this.rawReview = null;
            this.mappingsDirty = false;
            if (this.$refs.rawFile) this.$refs.rawFile.value = '';
        },
        setRawColumn(source, group, kind, column) {
            if (column) source.mapping[group][kind] = column;
            else delete source.mapping[group][kind];
            this.mappingsDirty = true;
        },
        addMeasure(source) {
            source.mapping.measures.push({column: '', key: '', name: '', unit: 'SAR', aggregation: 'sum'});
            this.mappingsDirty = true;
        },
        previewRaw() {
            return this.work('preview', async live => {
                const selection = {model_id: this.model.model_id, expected_version: this.model.version};
                if (this.mappingsDirty) selection.company_mapping = JSON.stringify(Object.fromEntries(
                    this.rawReview.data.sources.map(source => [source.id, source.mapping])));
                let review;
                if (this.rawConnectorId) {
                    const path = '/companies/' + this.model.company_id + '/connectors/erp/' + this.rawConnectorId;
                    review = await api(path + '/entry', post(selection));
                } else {
                    const data = new FormData();
                    this.rawFiles.forEach(file => data.append('files', file));
                    if (this.rawSheet) data.append('sheet', this.rawSheet);
                    Object.entries(selection).forEach(([key, value]) => data.append(key, value));
                    review = await api(this.rawScope + '/entry', {method: 'POST', body: data});
                }
                if (!live()) return;
                this.rawReview = review;
                this.mappingsDirty = false;
                await this.loadRawReviews(live);
                await this.$nextTick();
                if (live()) this.$refs.rawReview?.focus();
            });
        },
        decideRaw(status) {
            return this.work('commit', async live => {
                const result = await api(this.rawScope + '/reviews/' + this.rawReview.id + '/decide', post({status}));
                if (!live()) return;
                if (result.outcome) {
                    const model = await api(this.path);
                    if (!live()) return;
                    this.model = model;
                    const company = this.$store.app.company;
                    if (company?.id === this.model.company_id) company.model_version = this.model.version;
                    this.message = 'اتعمدت البيانات واتحفظ إصدار ' + number(this.model.version) + '.';
                } else {
                    this.message = 'اترفضت المراجعة.';
                }
                this.rawReview = null;
                this.rawFiles = this.rawSheets = [];
                this.rawSheet = this.rawConnectorId = '';
                this.$refs.rawFile.value = '';
                await this.loadRawReviews(live);
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
