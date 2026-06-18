# UFC Fight Winner Model Metrics

- Training rows: 3527
- Test rows: 882
- Train date range: 1995-07-14 to 2023-03-18
- Test date range: 2023-03-25 to 2025-12-13
- Best params: {'colsample_bytree': 0.8, 'learning_rate': 0.03, 'max_depth': 3, 'n_estimators': 50, 'subsample': 0.8}

## Model Metrics
- accuracy: 0.5204
- precision: 0.5241
- recall: 0.5605
- f1: 0.5417
- roc_auc: 0.5259

## Baseline Metrics
- accuracy: 0.5057
- precision: 0.5057
- recall: 1.0000
- f1: 0.6717
- roc_auc: 0.5000

## Confusion Matrix

`[[209, 227], [196, 250]]`

## Classification Report

```text
              precision    recall  f1-score   support

           0     0.5160    0.4794    0.4970       436
           1     0.5241    0.5605    0.5417       446

    accuracy                         0.5204       882
   macro avg     0.5201    0.5199    0.5194       882
weighted avg     0.5201    0.5204    0.5196       882

```

## Feature Importance
- sig_strike_accuracy_diff: 0.082179
- reach_cm_diff: 0.076803
- total_prior_fights_diff: 0.073873
- control_time_seconds_per_fight_diff: 0.073160
- sig_strikes_landed_per_fight_diff: 0.069934
- total_rounds_fought_diff: 0.069367
- age_diff: 0.066379
- takedown_accuracy_diff: 0.065145
- submission_attempts_per_fight_diff: 0.057976
- knockdowns_per_fight_diff: 0.057604
- height_cm_diff: 0.056425
- days_since_last_fight_diff: 0.054601
- takedowns_landed_per_fight_diff: 0.053318
- win_streak_diff: 0.049228
- pct_wins_by_ko_diff: 0.047562
- wins_last_5_diff: 0.046446
- pct_wins_by_submission_diff: 0.000000
- pct_wins_by_decision_diff: 0.000000
- scheduled_rounds: 0.000000