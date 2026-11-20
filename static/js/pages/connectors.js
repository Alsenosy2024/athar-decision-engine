document.addEventListener('alpine:init', () => {
    Alpine.data('connectorsPage', () => ({
        markup: '', mode: 'file', busy: '', error: '', message: '',
        models: [], modelId: '', model: null, file: null, sheets: [], sheet: '',
        fileMapping: {node: '', value: '', date: ''}, result: null,
        connectors: [], legacyConnectors: [], legacyId: '', connectorId: '', config: {},
        p6File: null, p6Result: null, p6Imports: [], p6CompanyId: '', p6Selection: '',
        number,
        get integrationAdmin() { return this.$store.app.user?.role === 'admin'; },
        get rawCompany() { return this.$store.app.template === 'enterprise_company'; },
        get companyModels() {
            return this.mode === 'erp'
                ? this.models.filter(item => item.company_id === this.$store.app.companyId)
                : this.models;
        },
        connectorPath() { return '/companies/' + this.$store.app.companyId + '/connectors/erp'; },
        get columns() { return this.sheets.find(item => item.name === this.sheet)?.columns ?? []; },
        get contracting() { return this.$store.app.companies.filter(item => item.template === 'contracting'); },
        async run(action, work) {
            if (this.busy && action !== 'load') return;
            const generation = (this.generation ?? 0) + 1;
            this.generation = generation;
            const companyId = this.$store.app.companyId;
            const live = () => this.generation === generation && this.$store.app.companyId === companyId;
            this.busy = action;
            this.error = this.message = '';
            try { await work(live); }
            catch (error) { if (live()) this.error = error.message; }
            finally { if (live()) this.busy = ''; }
        },
        activate(page, companyId, loading) {
            if (page !== 'connectors' || loading) return;
            queueMicrotask(() => {
                if (this.$store.app.page !== 'connectors' || this.$store.app.companyId !== companyId) return;
                this.result = this.model = null;
                this.connectors = [];
                this.selectConnector();
                this.load();
            });
        },
        load() {
            return this.run('load', async live => {
                const companyId = this.$store.app.companyId;
                const [fragment, models, connectors, legacy] = await Promise.all([
                    this.markup || fetch('/static/partials/connectors.html').then(response => {
                        if (!response.ok) throw new Error('تعذّر فتح صفحة ربط البيانات.');
                        return response.text();
                    }), api('/enterprise/models'), companyId ? api(this.connectorPath()) : [],
                    this.integrationAdmin ? api('/erp/legacy-connectors') : [],
                ]);
                if (!live()) return;
                this.models = models;
                if (!this.companyModels.some(item => String(item.id) === this.modelId)) {
                    this.modelId = String(this.companyModels[0]?.id ?? '');
                    this.result = null;
                }
                const model = this.modelId ? await api(this.modelPath()) : null;
                if (!live()) return;
                if (this.model?.version !== model?.version) this.result = null;
                this.model = model;
                this.connectors = connectors;
                this.legacyConnectors = legacy;
                this.legacyId = '';
                this.selectConnector(this.connectorId);
                this.markup = fragment;
            });
        },
        setMode(mode) {
            this.mode = mode;
            this.result = null;
            this.error = this.message = '';
            if (mode !== 'p6' && !this.companyModels.some(item => String(item.id) === this.modelId)) {
                this.selectModel(String(this.companyModels[0]?.id ?? ''));
            }
            if (mode === 'p6' && !this.p6CompanyId && this.contracting.length) {
                this.p6CompanyId = String(this.contracting[0].id);
                this.loadP6();
            }
        },
        modelPath() { return '/enterprise/models/' + this.modelId; },
        selectModel(id) {
            this.modelId = id;
            this.model = this.result = null;
            if (id) return this.run('model', async live => {
                const model = await api(this.modelPath());
                if (live()) this.model = model;
            });
        },
        node(id) { return this.model?.graph.nodes.find(item => item.id === id); },
        form(file) {
            const data = new FormData();
            data.append('file', file);
            return data;
        },
        inspect(file) {
            this.file = file;
            this.sheets = [];
            this.selectSheet('');
            if (!file) return;
            return this.run('inspect', async () => {
                const response = await api('/enterprise/imports/inspect', {method: 'POST', body: this.form(file)});
                this.sheets = response.sheets;
                this.selectSheet(this.sheets.find(sheet => sheet.columns.length)?.name ?? '');
            });
        },
        selectSheet(sheet) {
            this.sheet = sheet;
            this.result = null;
            this.fileMapping = Object.fromEntries(['node', 'value', 'date'].map(field => [
                field, this.columns.includes(field) ? field : '',
            ]));
        },
        columnMapping(mapping) { return {...mapping, date: mapping.date || null}; },
        previewFile() {
            this.result = null;
            return this.run('preview', async live => {
                const data = this.form(this.file);
                data.append('sheet', this.sheet);
                data.append('columns', JSON.stringify(this.columnMapping(this.fileMapping)));
                const result = await api(this.modelPath() + '/imports/preview', {method: 'POST', body: data});
                if (live()) this.result = result;
            });
        },
        apply() {
            return this.run('apply', async live => {
                const {version, updates} = this.result;
                const model = await api(this.modelPath() + '/imports/apply', post({expected_version: version, updates}));
                if (!live()) return;
                this.model = model;
                this.result = null;
                this.message = 'اتحفظ إصدار النموذج رقم ' + this.model.version + '.';
            });
        },
        selectConnector(id = '') {
            this.connectorId = id;
            const saved = this.connectors.find(item => String(item.id) === id);
            this.config = saved ? {...saved, sourceColumns: (saved.source_columns || []).join(', '), mapping: {...saved.mapping}} : {
                kind: 'rest', name: '', url_env: '', token_env: '', rows_path: '',
                table: '', schema_name: '', mapping: {node: '', value: '', date: ''},
            };
            this.result = null;
        },
        saveConnector() {
            if (!this.integrationAdmin || !this.$store.app.companyId) return;
            return this.run('erp-save', async live => {
                const path = this.connectorPath();
                const config = this.config;
                const body = {kind: config.kind, name: config.name, url_env: config.url_env,
                    mapping: this.columnMapping(config.mapping)};
                if (this.rawCompany || config.sourceColumns) body.source_columns = (config.sourceColumns || '')
                    .split(',').map(column => column.trim()).filter(Boolean);
                if (config.kind === 'rest') Object.assign(body, {
                    token_env: config.token_env || null, rows_path: config.rows_path || '',
                });
                else Object.assign(body, {table: config.table, schema_name: config.schema_name || null});
                const options = post(body);
                if (this.connectorId) options.method = 'PUT';
                const saved = await api(path + (this.connectorId ? '/' + this.connectorId : ''), options);
                if (!live()) return;
                const connectors = await api(path);
                if (!live()) return;
                this.connectors = connectors;
                this.selectConnector(String(saved.id));
                this.message = 'اتحفظ إعداد الموصل.';
            });
        },
        claimLegacy() {
            return this.run('erp-claim', async live => {
                const path = this.connectorPath();
                const claimed = await api(path + '/' + this.legacyId + '/claim', post());
                const [connectors, legacy] = await Promise.all([api(path), api('/erp/legacy-connectors')]);
                if (!live()) return;
                this.connectors = connectors;
                this.legacyConnectors = legacy;
                this.legacyId = '';
                this.selectConnector(String(claimed.id));
                this.message = 'اتسند الموصل للشركة.';
            });
        },
        previewErp() {
            this.result = null;
            return this.run('erp-pull', async live => {
                const result = await api(this.modelPath() + '/imports/erp/' + this.connectorId + '/preview', post());
                if (live()) this.result = result;
            });
        },
        p6Path() { return '/companies/' + this.p6CompanyId + '/p6-imports'; },
        loadP6() {
            this.p6File = this.p6Result = null;
            this.p6Selection = '';
            if (this.$refs.p6File) this.$refs.p6File.value = '';
            return this.run('p6-load', async () => { this.p6Imports = await api(this.p6Path()); });
        },
        sendP6(commit) {
            return this.run('p6-import', async () => {
                const data = this.form(this.p6File);
                data.append('commit', String(commit));
                this.p6Selection = '';
                this.p6Result = await api(this.p6Path(), {method: 'POST', body: data});
                if (this.p6Result.id) {
                    this.p6Imports = await api(this.p6Path());
                    this.p6Selection = String(this.p6Result.id);
                    this.message = 'اتحفظ جدول Primavera.';
                }
            });
        },
        openP6(id) {
            this.p6Selection = id;
            this.p6File = this.p6Result = null;
            this.$refs.p6File.value = '';
            if (id) return this.run('p6-open', async () => {
                this.p6Result = await api(this.p6Path() + '/' + id);
            });
        },
    }));
});
