# UFC Fight Winner Model Metrics

- Training rows: 83
- Test rows: 21
- Train date range: 2024-11-23 to 2025-11-01
- Test date range: 2025-11-01 to 2025-12-13
- Best params: {'colsample_bytree': 1.0, 'learning_rate': 0.03, 'max_depth': 2, 'n_estimators': 50, 'subsample': 0.8}

## Model Metrics
- accuracy: 0.4286
- precision: 0.4118
- recall: 0.7778
- f1: 0.5385
- roc_auc: 0.3981

## Baseline Metrics
- accuracy: 0.4286
- precision: 0.4286
- recall: 1.0000
- f1: 0.6000
- roc_auc: 0.5000

## Confusion Matrix

`[[2, 10], [2, 7]]`

## Classification Report

```text
              precision    recall  f1-score   support

           0     0.5000    0.1667    0.2500        12
           1     0.4118    0.7778    0.5385         9

    accuracy                         0.4286        21
   macro avg     0.4559    0.4722    0.3942        21
weighted avg     0.4622    0.4286    0.3736        21

```

## Feature Importance
- sig_strike_accuracy_diff: 0.108263
- reach_cm_diff: 0.091987
- sig_strikes_landed_per_fight_diff: 0.088255
- pct_wins_by_ko_diff: 0.085457
- takedown_accuracy_diff: 0.082744
- total_rounds_fought_diff: 0.080291
- age_diff: 0.076684
- knockdowns_per_fight_diff: 0.073109
- win_streak_diff: 0.072939
- submission_attempts_per_fight_diff: 0.068913
- takedowns_landed_per_fight_diff: 0.068101
- control_time_seconds_per_fight_diff: 0.060917
- days_since_last_fight_diff: 0.042342
- wins_last_5_diff: 0.000000
- total_prior_fights_diff: 0.000000
- pct_wins_by_submission_diff: 0.000000
- pct_wins_by_decision_diff: 0.000000
- scheduled_rounds: 0.000000