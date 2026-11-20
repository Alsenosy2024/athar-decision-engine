const auditLabels = {
    'plan.create': 'حفظ مسودة', 'plan.approve': 'اعتماد خطة', 'plan.reject': 'رفض خطة',
    'model.update': 'تحديث النموذج', 'actuals.upload': 'رفع البيانات الفعلية',
    'calibration.update': 'تحديث المعايرة', 'dataset.demo': 'تحميل بيانات تجريبية',
    'dataset.upload': 'رفع مجموعة بيانات', 'company.create': 'إنشاء شركة',
    'run.optimize': 'تشغيل التحسين', 'run.whatif': 'تشغيل سيناريو',
    'run.montecarlo': 'محاكاة المخاطر', 'forecast.run': 'تشغيل التوقع', 'chat.run': 'تشغيل المساعد',
    'enterprise.create': 'حفظ نموذج مؤسسي', 'enterprise.update': 'تحديث نموذج مؤسسي',
    'enterprise.archive': 'أرشفة نموذج مؤسسي', 'enterprise.company.assign': 'ربط نموذج بشركة',
    'enterprise.scenario.create': 'حفظ سيناريو مؤسسي',
};
const planStatus = status => ({ draft: 'مسودة', approved: 'معتمدة', rejected: 'مرفوضة' })[status];
const variantLabel = variant => variant === 'optimized' ? 'خطة محسّنة' : 'سيناريو';

document.addEventListener('alpine:init', () => {
    Alpine.data('approvalsPage', () => workspacePage('approvals', () => ({
        plans: [], audit: [], selected: null, comment: '', decision: 'approved',
    }), {
        planStatus, variantLabel,
        get formats() {
            const formats = [['xlsx', 'Excel'], ['csv', 'CSV'], ['json', 'JSON']];
            return this.template === 'contracting' ? [...formats, ['p6.csv', 'P6 CSV'], ['p6.xml', 'P6 XML']] : formats;
        },
        auditLabel(action) {
            return action.startsWith('plan.export.') ? 'تصدير خطة · ' + action.slice('plan.export.'.length).toUpperCase()
                : auditLabels[action] ?? action;
        },
        async load(base, live) {
            const [plans, audit] = await Promise.all([api(base + '/plans'), api(base + '/audit')]);
            if (live()) {
                this.$store.app.plans = this.plans = plans;
                this.audit = audit;
            }
        },
        review(id) {
            this.selected = id;
            this.comment = '';
            this.decision = 'approved';
            this.$nextTick(() => this.$refs.decision.focus());
        },
        saveDecision() {
            this.perform('decision', async (base, live) => {
                const updated = await api('/plans/' + this.selected + '/decide',
                    post({ comment: this.comment.trim(), decision: this.decision }));
                if (!live()) return;
                this.selected = null;
                this.plans = this.plans.map(plan => plan.id === updated.id ? updated : plan);
                this.message = 'تم تسجيل القرار للخطة رقم ' + number(updated.id) + '.';
                await this.load(base, live);
            });
        },
        download(id, format) {
            this.perform('export', async (base, live) => {
                const response = await request('/plans/' + id + '/export.' + format);
                if (!response.ok) {
                    const body = await response.json().catch(() => null);
                    throw new Error(body?.detail?.msg_ar || 'تعذّر تنزيل الملف.');
                }
                const file = await response.blob();
                if (!live()) return;
                const url = URL.createObjectURL(file);
                const link = document.createElement('a');
                link.href = url;
                link.download = 'plan-' + id + '.' + format;
                link.click();
                setTimeout(() => URL.revokeObjectURL(url), 1000);
                this.message = 'الملف جاهز للتنزيل.';
                const audit = await api(base + '/audit');
                if (live()) this.audit = audit;
            });
        },
    }));
});
