document.addEventListener('alpine:init', () => {
    Alpine.data('chatPage', () => workspacePage('chat', () => ({
        messages: [], input: '', reply: null, provider: '',
        scope: 'enterprise', models: [], modelId: '', remote: false, pinnedRun: null,
    }), {
        assistant: ATHAR_ASSISTANT,
        get sectorAvailable() { return !this.$store.app.isOnboardingCompany && ['admin', 'planner'].includes(this.$store.app.user?.role); },
        enterpriseResultContext,
        get providerLabel() {
            if (this.scope === 'sector' && !this.remote) return 'قواعد محلية';
            return { codex: 'Codex', rules: 'قواعد محلية', 'rules(fallback)': 'قواعد محلية · رجوع تلقائي',
                openai_compat: 'OpenAI-compatible' }[this.reply?.provider || this.provider] || '';
        },
        get suggestions() {
            if (this.scope === 'enterprise') return ['الكاش كام دلوقتي؟', 'ليه الربح نزل؟', 'لو الطلب قل 20% إيه الأثر؟'];
            return ['كام إجمالي التكلفة؟', 'إيه القيود الخانقة؟', 'إيه تعارض القيود؟',
                'لو الحد الائتماني قل 10% إيه اللي هيحصل؟',
                this.template === 'production' ? 'لو الطلب زاد 20%؟' : 'إيه الغرامات؟'];
        },
        async load(base, live) {
            const handoff = this.$store.app.sectorChatRun;
            if (handoff?.company === this.$store.app.companyId) {
                this.scope = 'sector';
                this.pinnedRun = handoff.id;
                this.$store.app.sectorChatRun = null;
            }
            const [history, health, models] = await Promise.all([
                this.scope === 'sector' ? api(base + '/chat/history') : [],
                this.scope === 'sector' ? api('/health') : { provider: 'rules' },
                api('/enterprise/models'),
            ]);
            if (!live()) return;
            this.messages = history;
            this.provider = health.provider;
            this.models = models.filter(model => model.company_id === this.$store.app.companyId);
            if (!this.modelId && this.$store.app.assistantMapModel) {
                this.modelId = String(this.$store.app.assistantMapModel.id);
            }
        },
        switchScope() {
            this.input = '';
            this.pinnedRun = null;
            this.messages = [];
            this.reply = null;
            this.perform('load', this.load);
        },
        switchModel() {
            this.$store.app.assistantMapModel = this.models.find(model => String(model.id) === this.modelId) || null;
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
                    ? await askAthar(message, {
                        company_id: this.$store.app.companyId,
                        model_id: this.modelId ? Number(this.modelId) : null,
                        version: this.models.find(model => String(model.id) === this.modelId)?.version ?? null,
                    }, { remote: this.remote, focus_node: this.reply?.follow_up_metric ?? null })
                    : await api(base + '/chat', post({ message, run_id: this.pinnedRun, remote: this.remote }));
                if (!live()) return;
                this.messages = [...this.messages, { role: 'user', content: message }, { role: 'assistant', content: reply.reply_ar, context: reply.context, evidence: reply.evidence }];
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
        evidenceLabel(entry) {
            const label = metricDefinitions[entry.metric] || ({
                run_id: 'معرّف التشغيل', approved_plan_id: 'معرّف الخطة المعتمدة', credit_limit: 'حد الائتمان',
                binding_count: 'عدد الحالات عند الحد', overrun: 'التجاوز', total_delta: 'فرق التكلفة',
                total_cost_delta: 'فرق التكلفة', delayed_projects: 'المشاريع المتأخرة', late: 'التأخير',
                demand: 'الكمية المطلوبة', cold_capacity: 'سعة التخزين المبرد', late_days: 'التأخير', shipment_day: 'يوم الشحن',
            })[entry.metric] || 'قيمة المؤشر';
            return [label, entry.display, entry.unit].filter(Boolean).join(' · ');
        },
    }));
});
