"""One SPO+ experiment: learn objective coefficients over a fixed candidate set."""
import numpy as np

MIN_IMPROVEMENT_PCT = 5.0
INITIAL_WEIGHTS = (0.75, 0.25, 0.0)
FEATURES = ('mean_draw_cost', 'cvar90_draw_cost', 'central_planning_cost')
COST_NORMALIZER = 10_000
EPOCHS = 200


def fit_spo_plus(examples):
    """Full-batch subgradient with L2 shrinkage towards the scenario rule.

    Each feasible decision is a single candidate (a one-hot vector). Realized
    recourse costs are labels; the feasible set and features are fixed before
    the week's outcomes. Rows of features are scaled to comparable magnitudes.
    """
    prior = np.array(INITIAL_WEIGHTS)
    weights = prior.copy()
    for epoch in range(EPOCHS):
        gradient = 0.01 * (weights - prior)
        for features, costs in examples:
            prediction = features @ weights
            oracle = np.argmin(costs)
            adversarial = np.argmin(2 * prediction - costs / COST_NORMALIZER)
            gradient += 2 * (features[oracle] - features[adversarial]) / len(examples)
        weights -= 0.25 / np.sqrt(epoch + 1) * gradient
    return weights


def assess(experiment, strongest, comparison, *, expected_windows, replay_ok, all_eligible):
    denominator = strongest['mean_regret_sar']
    regret = experiment['mean_regret_sar']
    improvement = (100 * (denominator - regret) / denominator
                   if denominator is not None and denominator > 0.01 and regret is not None else None)
    ci = comparison['ci95_sar']
    checks = {
        'minimum_regret_improvement': improvement is not None and improvement >= MIN_IMPROVEMENT_PCT,
        'paired_ci_above_zero': ci is not None and ci[0] > 0,
        'complete_pairing': comparison['paired_windows'] == expected_windows,
        'exact_references_and_recourse': all_eligible,
        'verified_replay': replay_ok,
        'no_service_decline': experiment['service_pct'] is not None and strongest['service_pct'] is not None
        and experiment['service_pct'] >= strongest['service_pct'],
        'no_tail_increase': experiment['cvar90_realized_sar'] is not None
        and strongest['cvar90_realized_sar'] is not None
        and experiment['cvar90_realized_sar'] <= strongest['cvar90_realized_sar'],
        'zero_hard_violations': experiment['eligible_windows'] == expected_windows,
    }
    return {'minimum_improvement_pct': MIN_IMPROVEMENT_PCT, 'regret_improvement_pct': improvement,
            'checks': checks, 'passed': all(checks.values()), 'enabled_by_default': False}
