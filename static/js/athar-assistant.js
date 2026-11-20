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
                <small x-text="assistant.promise"></small>
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
                <small>سؤال الفرضية والمدخلات المتاحة ممكن يتبعتوا لـCodex لو مفعّل على الخادم.</small>
            </details>
            <div class="athar-assistant-response" x-show="busy || reply" role="status" aria-live="polite" :aria-busy="busy">
                <p x-show="busy">بيفكر…</p>
                <template x-if="reply">
                    <div>
                        <bdi class="athar-assistant-provider" x-text="assistant.providers[reply.provider]"></bdi>
                        <p data-testid="athar-reply" x-text="reply.reply_ar"></p>
                    </div>
                </template>
            </div>
            <p class="notice error" role="alert" x-show="error" x-text="error"></p>
        </div>`,
    select(companyId, modelId, version) {
        const selection = { company_id: Number(companyId) || null, model_id: Number(modelId) || null,
            version: Number(version) || null };
        if (JSON.stringify(selection) === JSON.stringify(this.selection)) return;
        const resolvingDefault = this.selection?.company_id === selection.company_id && !this.selection.model_id;
        this.selection = selection;
        if (resolvingDefault) return;
        this.revision++;
        this.question = this.error = '';
        this.reply = null;
        this.busy = this.remote = false;
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
            const reply = await askAthar(message, this.selection, { remote: this.remote, focus_node });
            if (revision === this.revision) this.reply = reply;
        } catch (error) {
            if (revision === this.revision) this.error = error.message;
        } finally {
            if (revision === this.revision) this.busy = false;
        }
    },
})));
