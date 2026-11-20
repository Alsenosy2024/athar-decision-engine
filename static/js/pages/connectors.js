document.addEventListener('alpine:init', () => {
    Alpine.data('connectorsPage', () => ({
        markup: '', mode: 'file', busy: '', error: '', message: '',
        models: [], modelId: '', model: null, file: null, sheets: [], sheet: '',
        fileMapping: {node: '', value: '', date: ''}, result: null,
        connectors: [], connectorId: '', config: {},
        p6File: null, p6Result: null, p6Imports: [], p6CompanyId: '', p6Selection: '',
        number,
        get columns() { return this.sheets.find(item => item.name === this.sheet)?.columns ?? []; },
        get contracting() { return this.$store.app.companies.filter(item => item.template === 'contracting'); },
        async run(action, work) {
            if (this.busy) return;
            this.busy = action;
            this.error = this.message = '';
            try { await work(); }
            catch (error) { this.error = error.message; }
            finally { this.busy = ''; }
        },
        load() {
            return this.run('load', async () => {
                const [fragment, models, connectors] = await Promise.all([
                    this.markup || fetch('/static/partials/connectors.html').then(response => {
                        if (!response.ok) throw new Error('تعذّر فتح صفحة ربط البيانات.');
                        return response.text();
                    }), api('/enterprise/models'), api('/connectors/erp'),
                ]);
                this.models = models;
                if (!models.some(item => String(item.id) === this.modelId)) {
                    this.modelId = String(models[0]?.id ?? '');
                    this.result = null;
                }
                const model = this.modelId ? await api(this.modelPath()) : null;
                if (this.model?.version !== model?.version) this.result = null;
                this.model = model;
                this.connectors = connectors;
                this.selectConnector(this.connectorId);
                this.markup = fragment;
            });
        },
        setMode(mode) {
            this.mode = mode;
            this.result = null;
            this.error = this.message = '';
            if (mode === 'p6' && !this.p6CompanyId && this.contracting.length) {
                this.p6CompanyId = String(this.contracting[0].id);
                this.loadP6();
            }
        },
        modelPath() { return '/enterprise/models/' + this.modelId; },
        selectModel(id) {
            this.modelId = id;
            this.model = this.result = null;
            if (id) return this.run('model', async () => { this.model = await api(this.modelPath()); });
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
            return this.run('preview', async () => {
                const data = this.form(this.file);
                data.append('sheet', this.sheet);
                data.append('columns', JSON.stringify(this.columnMapping(this.fileMapping)));
                this.result = await api(this.modelPath() + '/imports/preview', {method: 'POST', body: data});
            });
        },
        apply() {
            return this.run('apply', async () => {
                const {version, updates} = this.result;
                this.model = await api(this.modelPath() + '/imports/apply', post({expected_version: version, updates}));
                this.result = null;
                this.message = 'اتحفظ إصدار النموذج رقم ' + this.model.version + '.';
            });
        },
        selectConnector(id = '') {
            this.connectorId = id;
            const saved = this.connectors.find(item => String(item.id) === id);
            this.config = saved ? {...saved, mapping: {...saved.mapping}} : {
                kind: 'rest', name: '', url_env: '', token_env: '', rows_path: '',
                table: '', schema_name: '', mapping: {node: '', value: '', date: ''},
            };
            this.result = null;
        },
        saveConnector() {
            return this.run('erp-save', async () => {
                const config = this.config;
                const body = {kind: config.kind, name: config.name, url_env: config.url_env,
                    mapping: this.columnMapping(config.mapping)};
                if (config.kind === 'rest') Object.assign(body, {
                    token_env: config.token_env || null, rows_path: config.rows_path || '',
                });
                else Object.assign(body, {table: config.table, schema_name: config.schema_name || null});
                const options = post(body);
                if (this.connectorId) options.method = 'PUT';
                const saved = await api('/connectors/erp' + (this.connectorId ? '/' + this.connectorId : ''), options);
                this.connectors = await api('/connectors/erp');
                this.selectConnector(String(saved.id));
                this.message = 'اتحفظ إعداد الموصل.';
            });
        },
        previewErp() {
            this.result = null;
            return this.run('erp-pull', async () => {
                this.result = await api(this.modelPath() + '/imports/erp/' + this.connectorId + '/preview', post());
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
