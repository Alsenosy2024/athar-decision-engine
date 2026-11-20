const eligibilityReasons = {
    inputs_unapproved: 'المدخلات محتاجة اعتماد',
    shadow_only: 'للمراجعة التجريبية فقط',
    no_solution: 'مفيش خطة مرشحة',
    solver_no_solution: 'السولفر ما لاقاش حل في المهلة. المتاح خطة إرشادية للمراجعة',
    invalid_schedule: 'جدول الخطة ما عداش التحقق',
    mandatory_violation: 'فيه تجاوز لقيد إلزامي',
};

const eligibilityLabel = verdict => verdict?.eligible ? 'مؤهلة للاعتماد' : 'غير مؤهلة للاعتماد';
const eligibilityText = verdict => verdict?.reasons.map(key => eligibilityReasons[key]).join(' · ') || '';

const searchStatusLabel = status => ({
    OPTIMAL: 'حل أمثل للنموذج', FEASIBLE: 'عثر على حل للنموذج',
}[status] || solverLabel(status));

document.addEventListener('alpine:init', () => {
    Alpine.data('solutionRecordPanel', (source) => ({
        get record() { return source(); },
        get verdict() { return this.record?.optimized; },
        get checks() {
            return Object.entries(this.record?.policy || {}).map(([key, rule]) =>
                [key, {...rule, ...this.verdict?.constraints[key]}]);
        },
        get gap() {
            const value = this.record?.solver.relative_gap;
            return value == null ? '—' : number(100 * value) + '%';
        },
        eligibilityLabel, eligibilityText, searchStatusLabel,
        formatValue(value) { return value == null ? '—' : number(value); },
    }));
});
