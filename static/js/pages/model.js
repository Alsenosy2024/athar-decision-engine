document.addEventListener('alpine:init', () => {
    Alpine.data('modelPage', () => ({
        companyId: null,
        definition: null,
        model: null,
        draft: null,
        busy: false,
        error: '',
        message: '',
        sequence: 0,

        setModel(model) {
            this.draft = {
                parameters: { ...model.parameters },
                constraints: { ...model.constraints },
                weights: { ...model.weights },
            };
            for (const param of this.definition.parameters) {
                if (param.type === 'dict') this.draft.parameters[param.id] = JSON.stringify(model.parameters[param.id]);
            }
            this.model = model;
        },

        async load(id, template, page) {
            if (page !== 'model' || !id || this.companyId === id) return;
            this.companyId = id;
            const sequence = ++this.sequence;
            this.model = null;
            this.error = this.message = '';
            this.busy = true;
            try {
                const [definition, model] = await Promise.all([
                    api(`/templates/${template}`),
                    api(`/companies/${id}/model`),
                ]);
                if (sequence === this.sequence) {
                    this.definition = definition;
                    this.setModel(model);
                }
            } catch (error) {
                if (sequence === this.sequence) {
                    this.error = error.message;
                    this.$store.app.notify(error.message);
                }
            } finally {
                if (sequence === this.sequence) this.busy = false;
            }
        },

        async save() {
            if (this.busy) return;
            const sequence = this.sequence;
            const id = this.companyId;
            this.error = this.message = '';
            const parameters = { ...this.draft.parameters };
            for (const param of this.definition.parameters) {
                if (param.type !== 'dict') continue;
                try {
                    parameters[param.id] = JSON.parse(parameters[param.id]);
                } catch {
                    this.error = `راجع صيغة JSON في «${param.name_ar}».`;
                    return;
                }
            }
            const constraints = Object.fromEntries(
                this.definition.constraints.filter(rule => rule.kind === 'hard_soft' && rule.editable)
                    .map(rule => [rule.id, this.draft.constraints[rule.id]])
            );
            this.busy = true;
            try {
                const model = await api(`/companies/${id}/model`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ parameters, constraints, weights: this.draft.weights }),
                });
                const company = this.$store.app.companies.find(item => item.id === id);
                company.model_version = model.version;
                if (sequence === this.sequence) {
                    this.setModel(model);
                    this.message = 'تم حفظ إصدار جديد من النموذج.';
                }
            } catch (error) {
                if (sequence === this.sequence) {
                    this.error = error.message;
                    this.$store.app.notify(error.message);
                }
            } finally {
                if (sequence === this.sequence) this.busy = false;
            }
        },
    }));
});
