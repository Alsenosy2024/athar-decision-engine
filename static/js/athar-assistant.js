const ATHAR_ASSISTANT = {
    name: 'اسأل أثر',
    promise: 'اقرأ مؤشر، افهم تغيّره، أو احسب أثر فرضية من بيانات شركتك.',
    providers: { rules: 'قواعد محلية', codex: 'Codex', 'rules(fallback)': 'قواعد محلية · رجوع تلقائي' },
};

function askAthar(message, selection, options = {}) {
    return api('/enterprise/chat', post({ message, ...selection, ...options }));
}

document.addEventListener('alpine:init', () => Alpine.data('atharAssistant', () => ({
    assistant: ATHAR_ASSISTANT,
    question: '', reply: null, error: '', busy: false, remote: false, selection: null,
    revision: 0,
    markup: `
        <div class="athar-assistant-inner">
            <div class="athar-assistant-title">
                <strong id="athar-assistant-title" x-text="assistant.name"></strong>
                <kbd title="اختصار لوحة المفاتيح">⌘ K</kbd>
            </div>
            <form class="athar-assistant-form" @submit.prevent="submitQuestion()">
                <label class="visually-hidden" for="athar-question">سؤالك لأثر</label>
                <input id="athar-question" data-testid="athar-question" type="text" x-model="question"
                       placeholder="الكاش كام دلوقتي؟" maxlength="2000" required :disabled="busy">
                <button class="button primary" data-testid="athar-ask" :disabled="busy || !question.trim()"
                        x-text="busy ? 'بيفكر…' : 'اسأل'"></button>
            </form>
            <details class="athar-assistant-options">
                <summary>طريقة الرد</summary>
                <label><input type="checkbox" x-model="remote" :disabled="busy"> السماح بـCodex لفهم الفرضية</label>
                <a href="#/chat" @click="$el.closest('details').open = false">افتح المحادثة</a>
                <small>سؤال الفرضية والمدخلات المتاحة ممكن يتبعتوا لـCodex لو مفعّل على الخادم.</small>
            </details>
            <div class="athar-assistant-response" x-show="busy || reply" role="status" aria-live="polite" :aria-busy="busy">
                <p x-show="busy">بيفكر…</p>
                <template x-if="reply">
                    <div>
                        <bdi class="athar-assistant-provider" x-text="assistant.providers[reply.provider]"></bdi>
                        <button class="assistant-dismiss" type="button" @click="reply = null" aria-label="اقفل رد أثر">×</button>
                        <p data-testid="athar-reply" x-text="reply.reply_ar"></p>
                    </div>
                </template>
            </div>
            <p class="notice error" role="alert" x-show="error" x-text="error"></p>
        </div>`,
    select(companyId, modelId, version, demo) {
        const selection = { company_id: Number(companyId) || null, model_id: Number(modelId) || null,
            version: Number(version) || null, demo: Boolean(demo) };
        if (JSON.stringify(selection) === JSON.stringify(this.selection)) return;
        const resolvingDefault = this.selection?.company_id === selection.company_id && this.selection.demo === selection.demo && !this.selection.model_id;
        this.selection = selection;
        if (resolvingDefault) return;
        this.revision++;
        this.question = this.error = '';
        this.reply = null;
        this.busy = this.remote = false;
    },
    focusOnShortcut(event) {
        if (!(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== 'k' || !this.$el.offsetWidth) return;
        event.preventDefault();
        this.$el.querySelector('input')?.focus();
    },
    async submitQuestion() {
        const message = this.question.trim();
        if (!message || this.busy || !this.selection?.company_id) return;
        const revision = ++this.revision;
        const focus_node = this.reply?.follow_up_metric ?? null;
        this.busy = true;
        this.reply = null;
        this.error = '';
        try {
            const { demo, ...selection } = this.selection;
            const reply = await askAthar(message, demo ? {} : selection, { remote: this.remote, focus_node });
            if (revision !== this.revision) return;
            this.reply = reply;
            if (typeof askEntityCandidates !== 'function') return;
            const candidates = askEntityCandidates(message, reply);
            const store = this.$store.app;
            const query = new URLSearchParams();
            if (!demo) query.set('company_id', selection.company_id);
            if (reply.model_id) {
                query.set('model_id', reply.model_id);
                if (reply.version) query.set('version', reply.version);
            }
            const map = await api('/enterprise/map?' + query).catch(() => null);
            if (revision !== this.revision) return;
            const entity = entityFromCandidates(map, candidates);
            if (entity) sendMapFocus(store, {...candidates, demo, keys: [entity.id]});
        } catch (error) {
            if (revision === this.revision) this.error = error.message;
        } finally {
            if (revision === this.revision) this.busy = false;
        }
    },
})));
