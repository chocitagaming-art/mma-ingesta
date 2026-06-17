# UFC Fight Winner Model Metrics

- Training rows: 196
- Test rows: 50
- Train date range: 2024-06-15 to 2025-10-11
- Test date range: 2025-10-11 to 2025-12-13
- Best params: {'colsample_bytree': 1.0, 'learning_rate': 0.03, 'max_depth': 3, 'n_estimators': 50, 'subsample': 1.0}

## Model Metrics
- accuracy: 0.5200
- precision: 0.5306
- recall: 0.9630
- f1: 0.6842
- roc_auc: 0.5378

## Baseline Metrics
- accuracy: 0.5400
- precision: 0.5400
- recall: 1.0000
- f1: 0.7013
- roc_auc: 0.5000

## Confusion Matrix

`[[0, 23], [1, 26]]`

## Classification Report

```text
              precision    recall  f1-score   support

           0     0.0000    0.0000    0.0000        23
           1     0.5306    0.9630    0.6842        27

    accuracy                         0.5200        50
   macro avg     0.2653    0.4815    0.3421        50
weighted avg     0.2865    0.5200    0.3695        50

```

## Feature Importance
- takedown_accuracy_diff: 0.150041
- knockdowns_per_fight_diff: 0.115844
- wins_last_5_diff: 0.099560
- sig_strike_accuracy_diff: 0.090242
- total_rounds_fought_diff: 0.086770
- win_streak_diff: 0.076920
- sig_strikes_landed_per_fight_diff: 0.071034
- days_since_last_fight_diff: 0.070622
- age_diff: 0.063127
- reach_cm_diff: 0.056848
- control_time_seconds_per_fight_diff: 0.049652
- submission_attempts_per_fight_diff: 0.036104
- takedowns_landed_per_fight_diff: 0.017102
- total_prior_fights_diff: 0.016133
- pct_wins_by_ko_diff: 0.000000
- pct_wins_by_submission_diff: 0.000000
- pct_wins_by_decision_diff: 0.000000
- scheduled_rounds: 0.000000