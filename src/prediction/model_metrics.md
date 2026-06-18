# UFC Fight Winner Model Metrics

- Training rows: 3527
- Test rows: 882
- Train date range: 1995-07-14 to 2023-03-18
- Test date range: 2023-03-25 to 2025-12-13
- Best params: {'colsample_bytree': 0.8, 'learning_rate': 0.03, 'max_depth': 2, 'n_estimators': 100, 'subsample': 0.8}

## Model Metrics
- accuracy: 0.6156
- precision: 0.6202
- recall: 0.6188
- f1: 0.6195
- roc_auc: 0.6539

## Baseline Metrics
- accuracy: 0.5057
- precision: 0.5057
- recall: 1.0000
- f1: 0.6717
- roc_auc: 0.5000

## Confusion Matrix

`[[267, 169], [170, 276]]`

## Classification Report

```text
              precision    recall  f1-score   support

           0     0.6110    0.6124    0.6117       436
           1     0.6202    0.6188    0.6195       446

    accuracy                         0.6156       882
   macro avg     0.6156    0.6156    0.6156       882
weighted avg     0.6157    0.6156    0.6157       882

```

## Feature Importance
- age_diff: 0.132677
- wins_last_5_diff: 0.085582
- sig_strikes_landed_per_fight_diff: 0.077772
- takedowns_landed_per_fight_diff: 0.065481
- total_rounds_fought_diff: 0.065268
- total_prior_fights_diff: 0.063758
- control_time_seconds_per_fight_diff: 0.063420
- win_streak_diff: 0.057105
- takedown_accuracy_diff: 0.053173
- height_cm_diff: 0.052659
- reach_cm_diff: 0.051980
- days_since_last_fight_diff: 0.051380
- knockdowns_per_fight_diff: 0.051323
- sig_strike_accuracy_diff: 0.047473
- pct_wins_by_ko_diff: 0.041408
- submission_attempts_per_fight_diff: 0.039537
- pct_wins_by_submission_diff: 0.000000
- pct_wins_by_decision_diff: 0.000000
- scheduled_rounds: 0.000000