document.addEventListener('alpine:init', () => {
    Alpine.data('dataPage', () => ({
        companyId: null,
        definition: null,
        datasets: [],
        validation: null,
        action: '',
        message: '',
        error: '',
        fileName: '',
        sequence: 0,

        get active() {
            return this.datasets.find(dataset => dataset.is_active);
        },

        get summary() {
            return Object.entries(this.active?.validation.summary || {}).map(([key, count]) => ({
                key, count, label: this.definition?.entities[key]?.name_ar ?? key,
            }));
        },

        async selectCompany(id, template, page) {
            if (page !== 'data' || id === this.companyId) return;
            this.companyId = id;
            const sequence = ++this.sequence;
            this.datasets = [];
            this.definition = this.validation = null;
            this.message = this.error = this.fileName = '';
            this.$refs.file.value = '';
            if (id === null) return;
            this.action = 'load';
            try {
                const [datasets, definition] = await Promise.all([
                    api(`/companies/${id}/datasets`),
                    api(`/templates/${template}`),
                ]);
                if (sequence === this.sequence) {
                    this.datasets = datasets;
                    this.definition = definition;
                }
            } catch (error) {
                if (sequence === this.sequence) {
                    this.error = error.message;
                    this.$store.app.notify(error.message);
                }
            } finally {
                if (sequence === this.sequence) this.action = '';
            }
        },

        async submit(source) {
            if (!this.companyId || this.action) return;
            const sequence = this.sequence;
            const id = this.companyId;
            const body = source === 'upload' ? new FormData(this.$refs.upload) : undefined;
            this.validation = null;
            this.error = this.message = '';
            this.action = source;
            try {
                const result = await api(`/companies/${id}/datasets/${source}`, { method: 'POST', body });
                if (sequence !== this.sequence) return;
                this.validation = result.validation;
                if (!result.validation.ok) {
                    this.$store.app.notify('الملف فيه أخطاء. راجع جدول التحقق.');
                    return;
                }
                const company = this.$store.app.companies.find(item => item.id === id);
                company.active_dataset_id = result.dataset_id;
                const datasets = await api(`/companies/${id}/datasets`);
                if (sequence === this.sequence) {
                    this.datasets = datasets;
                    this.message = source === 'demo' ? 'تم تحميل البيانات التجريبية.' : 'تم قبول الملف وتفعيله.';
                    this.$refs.upload.reset();
                    this.fileName = '';
                }
            } catch (error) {
                if (sequence === this.sequence) {
                    this.error = error.message;
                    this.$store.app.notify(error.message);
                }
            } finally {
                if (sequence === this.sequence) this.action = '';
            }
        },
    }));
});
