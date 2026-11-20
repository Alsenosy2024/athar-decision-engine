document.addEventListener('alpine:init', () => {
    Alpine.data('chatPage', () => workspacePage('chat', () => ({
        messages: [], input: '', reply: null, provider: '',
        scope: 'sector', models: [], modelId: '',
    }), {
        get providerLabel() {
            return { codex: 'Codex', rules: 'قواعد محلية', 'rules(fallback)': 'قواعد محلية · رجوع تلقائي',
                openai_compat: 'OpenAI-compatible' }[this.reply?.provider || this.provider] || '';
        },
        get suggestions() {
            if (this.scope === 'enterprise') return ['لو خفّضنا حد الائتمان 15% وأخّرنا المورد أسبوعين، إيه الأثر؟'];
            return ['كام إجمالي التكلفة؟', 'ليه الخطة دي أحسن؟',
                'لو الحد الائتماني قل 10% إيه اللي هيحصل؟',
                this.template === 'production' ? 'لو الطلب زاد 20%؟' : 'إيه الغرامات؟'];
        },
        async load(base, live) {
            const [history, health, models] = await Promise.all([
                api(base + '/chat/history'), api('/health'), api('/enterprise/models'),
            ]);
            if (!live()) return;
            this.messages = this.scope === 'sector' ? history : [];
            this.provider = health.provider;
            this.models = models;
        },
        switchScope() {
            this.input = '';
            this.messages = [];
            this.reply = null;
            this.perform('load', this.load);
        },
        switchModel() {
            this.messages = [];
            this.reply = null;
        },
        driverLabel(id) {
            return this.reply?.result?.nodes.find(row => row.id === id)?.name_ar || id;
        },
        driverValue(id, value) {
            const unit = this.reply?.result?.nodes.find(row => row.id === id)?.unit;
            const label = { days: 'يوم', weeks: 'أسبوع' }[unit] || enterpriseUnits[unit] || '';
            return number(value) + (unit === '%' ? '%' : ' ' + label);
        },
        send() {
            const message = this.input.trim();
            if (!message) return;
            this.perform('chat', async (base, live) => {
                const reply = this.scope === 'enterprise'
                    ? await api('/enterprise/chat', post({ message, model_id: this.modelId ? Number(this.modelId) : null }))
                    : await api(base + '/chat', post({ message }));
                if (!live()) return;
                this.messages = [...this.messages, { role: 'user', content: message }, { role: 'assistant', content: reply.reply_ar }];
                this.reply = reply;
                this.input = '';
                this.$nextTick(() => {
                    if (live()) this.$refs.thread.scrollTop = this.$refs.thread.scrollHeight;
                });
            });
        },
        openImpact() {
            this.$store.app.enterpriseChatImpact = this.reply;
        },
        confirm() {
            this.perform('confirm', async (base, live) => {
                const changes = this.reply.scenario.changes;
                const run = await latestRun(base);
                if (!live()) return;
                if (!run?.result.optimized?.plan) {
                    this.$store.app.chatScenario = { changes };
                    location.hash = '/whatif';
                    return;
                }
                const result = await api(base + '/whatif', post({ base_run_id: run.id, changes }));
                if (!live()) return;
                this.reply = { ...this.reply, action: 'whatif', run_id: result.run_id };
                this.message = 'تم تشغيل السيناريو رقم ' + number(result.run_id) + '.';
            });
        },
    }));
});
