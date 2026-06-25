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

## Diagnostico (evaluate.py)

Evaluacion diagnostica del modelo persistido (sin reentrenar). Reconstruye el mismo test slice cronologico de `train.py` y lo puntua con `model.joblib` (modelo + imputer + feature_columns guardados).

- Test rows: 882
- Test date range: 2023-03-25 to 2025-12-13
- Decision threshold: 0.5

### Probabilistic metrics (test)
- Brier score: 0.2340  (lower is better; 0.25 = uninformed 0.5)
- Log loss: 0.6606  (lower is better)
- Accuracy: 0.6156

### Calibration curve (10 uniform bins)

Mean predicted probability vs. observed positive fraction per bin.

| Bin | Count | Mean predicted | Observed fraction |
| --- | ---: | ---: | ---: |
| [0.0, 0.1) | 0 | - | - |
| [0.1, 0.2) | 0 | - | - |
| [0.2, 0.3) | 4 | 0.2791 | 0.2500 |
| [0.3, 0.4) | 139 | 0.3614 | 0.2806 |
| [0.4, 0.5) | 294 | 0.4559 | 0.4422 |
| [0.5, 0.6) | 320 | 0.5506 | 0.5813 |
| [0.6, 0.7) | 122 | 0.6341 | 0.7131 |
| [0.7, 0.8) | 3 | 0.7071 | 1.0000 |
| [0.8, 0.9) | 0 | - | - |
| [0.9, 1.0) | 0 | - | - |

calibration_curve (non-empty bins, predicted -> observed): (0.279 -> 0.250), (0.361 -> 0.281), (0.456 -> 0.442), (0.551 -> 0.581), (0.634 -> 0.713), (0.707 -> 1.000)

### Breakdown by division (weight class)

| Division | N | Accuracy | Brier | Positive rate |
| --- | ---: | ---: | ---: | ---: |
| Lightweight | 125 | 0.6400 | 0.2219 | 0.5040 |
| Welterweight | 113 | 0.6549 | 0.2280 | 0.4690 |
| Middleweight | 111 | 0.6216 | 0.2294 | 0.5405 |
| Featherweight | 105 | 0.5619 | 0.2441 | 0.4952 |
| Bantamweight | 101 | 0.6436 | 0.2337 | 0.4158 |
| Women's Strawweight | 77 | 0.6104 | 0.2405 | 0.5844 |
| Women's Flyweight | 58 | 0.6034 | 0.2382 | 0.6379 |
| Flyweight | 56 | 0.5714 | 0.2377 | 0.5357 |
| Heavyweight | 49 | 0.5510 | 0.2387 | 0.4694 |
| Light Heavyweight | 41 | 0.6098 | 0.2541 | 0.4634 |
| Women's Bantamweight | 32 | 0.6562 | 0.2249 | 0.4688 |
| Catch Weight | 11 | 0.7273 | 0.2224 | 0.6364 |
| Women's Featherweight | 3 | 0.3333 | 0.2620 | 0.0000 |

### Breakdown by scheduled_rounds (3 vs 5)

Scheduled rounds taken from the `fights` table (the CSV feature is degenerate, all 3).

| Scheduled rounds | N | Accuracy | Brier | Positive rate |
| --- | ---: | ---: | ---: | ---: |
| 3 | 882 | 0.6156 | 0.2340 | 0.5057 |

### Breakdown by era (year ranges)

| Era | N | Accuracy | Brier | Positive rate |
| --- | ---: | ---: | ---: | ---: |
| 2020-2024 | 564 | 0.6082 | 0.2361 | 0.4805 |
| 2025+ | 318 | 0.6289 | 0.2302 | 0.5503 |
