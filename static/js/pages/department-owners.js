document.addEventListener('alpine:init', () => {
    const root = '/api/department-owners';
    const dateFormat = new Intl.DateTimeFormat('ar-EG-u-nu-latn', {
        dateStyle: 'medium', timeStyle: 'short', timeZone: 'Asia/Riyadh',
    });

    const percent = new Intl.NumberFormat('ar-EG-u-nu-latn', {style: 'percent'});

    Alpine.data('departmentOwners', () => ({
        user: null,
        work: null,
        detail: null,
        selected: '',
        theme: 'light',
        busy: false,
        error: '',
        message: '',
        mode: 'manual',
        rows: [],
        nextRowId: 0,
        methods: [['manual', 'إدخال يدوي'], ['excel', 'رفع قالب Excel'], ['extract', 'استخراج ذكي']],
        preview: null,
        assignees: {},
        comments: {},

        get admin() { return this.user?.role === 'admin'; },
        get choices() {
            if (!this.work) return [];
            if (this.admin) return this.work.companies.map(company => ({value: String(company.id), title: company.name}));
            return this.work.sections.map(section => ({
                value: section.company_id + '/' + section.section,
                title: section.company + ' · ' + section.name,
            }));
        },
        get base() {
            const [company, section] = this.selected.split('/');
            return root + '/companies/' + company + (section ? '/sections/' + section : '');
        },
        get hasPending() { return this.detail?.submissions.some(item => item.status === 'pending'); },

        init() {
            this.theme = localStorage.getItem('decision-theme') === 'dark' ? 'dark' : 'light';
            document.documentElement.dataset.theme = this.theme;
            this.run(async () => {
                this.user = await this.api('/api/auth/me');
                await this.refreshWorkspace();
            });
        },
        toggleTheme() {
            this.theme = this.theme === 'dark' ? 'light' : 'dark';
            document.documentElement.dataset.theme = this.theme;
            localStorage.setItem('decision-theme', this.theme);
        },

        async api(path, method = 'GET', body = null) {
            const headers = {};
            if (method !== 'GET') headers['X-CSRF-Token'] = this.user.csrf_token;
            if (body !== null && !(body instanceof FormData)) {
                headers['Content-Type'] = 'application/json';
                body = JSON.stringify(body);
            }
            const response = await fetch(path, {method, body, headers});
            if (response.status === 401) {
                location.replace('/login');
                throw new Error('الجلسة انتهت. سجّل الدخول تاني.');
            }
            if (response.status === 204) return null;
            const data = await response.json();
            if (!response.ok) throw new Error(data.detail?.msg_ar || 'الطلب ماكملش. جرّب تاني.');
            return data;
        },
        async run(action) {
            if (this.busy) return;
            this.busy = true;
            this.error = '';
            this.message = '';
            try {
                await action();
            } catch (error) {
                this.error = error instanceof TypeError ? 'تعذّر الاتصال بالخادم.' : error.message;
            } finally {
                this.busy = false;
            }
        },
        async refreshWorkspace() {
            this.work = await this.api(root + '/workspace');
            if (!this.choices.some(choice => choice.value === this.selected)) this.selected = this.choices[0]?.value || '';
            await this.load();
        },
        async load() {
            this.preview = null;
            this.detail = null;
            this.comments = {};
            if (!this.selected) return;
            const detail = await this.api(this.base);
            if (this.admin) {
                this.assignees = Object.fromEntries(detail.sections.map(item => [item.id, this.ownerId(item)]));
            }
            this.detail = detail;
            if (!this.admin) {
                this.rows = [];
                this.addRow();
                this.mode = 'manual';
            }
        },
        logout() {
            this.run(async () => {
                await this.api('/api/auth/logout', 'POST');
                location.replace('/login');
            });
        },
        ownerId(section) { return section.owner ? String(section.owner.user_id) : ''; },
        assign(section) {
            this.run(async () => {
                const value = this.assignees[section.id];
                await this.api(this.base + '/sections/' + section.id + '/assignment', 'PUT',
                    {user_id: value ? Number(value) : null});
                await this.load();
                this.message = 'اتحدّث تكليف القسم.';
            });
        },
        decide(item, decision) {
            this.run(async () => {
                const result = await this.api(this.base + '/submissions/' + item.id + '/decide', 'POST',
                    {decision, comment: this.comments[item.id] || ''});
                await this.load();
                this.message = result.approved_version
                    ? 'اتحفظ التحديث في الإصدار ' + result.approved_version + '.'
                    : 'اترفض طلب التحديث.';
            });
        },
        changeMode(mode) {
            if (mode === this.mode) return;
            this.mode = mode;
            this.preview = null;
        },
        addRow() {
            const spec = this.detail.spec;
            this.rows.push({id: ++this.nextRowId, key: spec.fields[0]?.key || 'item', value: '',
                date: '', source: 'actual', suffix: ''});
        },
        previewManual() {
            this.run(async () => {
                this.preview = null;
                const records = this.rows.filter(row => row.value.trim()).map(row => ({
                    key: row.key === 'item' ? this.detail.spec.item[0] + '.' + row.suffix.trim() : row.key,
                    value: row.value.trim(), date: row.key !== 'item' && row.date ? row.date : null, source: row.source,
                }));
                if (!records.length) throw new Error('ضيف قيمة واحدة على الأقل للمراجعة.');
                this.preview = await this.api(this.base + '/manual/preview', 'POST',
                    {expected_version: this.detail.version, records});
            });
        },
        previewFile(event) {
            const form = new FormData(event.target);
            this.run(async () => {
                this.preview = null;
                form.set('expected_version', String(this.detail.version));
                if (this.mode === 'excel') form.delete('remote');
                this.preview = await this.api(this.base + '/' + this.mode + '/preview', 'POST', form);
            });
        },
        submit() {
            this.run(async () => {
                await this.api(this.base + '/submit', 'POST', {token: this.preview.token});
                await this.load();
                this.message = 'اتسجل التحديث ومستني اعتماد الأدمن.';
            });
        },
        fieldName(key) {
            const fields = this.admin ? this.detail.sections.flatMap(section => section.fields) : this.detail.spec.fields;
            return fields.find(field => field.key === key)?.label || key;
        },
        rowName(row) { return this.fieldName(row.key) + (row.date ? ' · ' + row.date : ''); },
        sourceText(row) {
            if (!row.evidence) return row.after.source === 'estimated' ? 'مقدّر' : 'فعلي';
            const confidence = percent.format(row.evidence.confidence);
            return row.evidence.source + ' · ثقة ' + confidence + (row.evidence.provider === 'codex' ? ' · Codex' : '');
        },
        score(section) { return section.score === null ? 'قسم اختياري' : 'الجاهزية ' + percent.format(section.score / 100); },
        formatDate(value) { return dateFormat.format(new Date(value)); },
        updated(section) { return section.last_updated ? 'آخر تحديث ' + this.formatDate(section.last_updated) : 'لسه مفيش تحديث'; },
        sectionName(id) {
            return this.admin ? this.detail.sections.find(item => item.id === id)?.name : this.detail.spec.name;
        },
        reminderText(reason) {
            return reason === 'stale' ? 'معدّاش تحديث من 30 يوم أو أكتر.' :
                reason === 'never_updated' ? 'القسم محتاج أول تحديث.' : '';
        },
        statusLabel(status) {
            return {pending: 'مستني اعتماد', approved: 'معتمد', rejected: 'مرفوض', superseded: 'ملغي بعد تغيير التكليف'}[status];
        },
        reviewText(item) {
            return (item.reviewer ? item.reviewer + ' · ' : '') + this.formatDate(item.decided_at)
                + (item.approved_version ? ' · إصدار ' + item.approved_version : '');
        },
    }));
});
