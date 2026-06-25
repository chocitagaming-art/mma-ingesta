# UFC Fight Winner Model Metrics

- Training rows: 4233
- Test rows: 1059
- Train date range: 1995-07-14 to 2023-06-24
- Test date range: 2023-07-01 to 2026-06-20
- Best params: {'colsample_bytree': 0.8, 'learning_rate': 0.03, 'max_depth': 2, 'n_estimators': 100, 'subsample': 0.8}

## Model Metrics
- accuracy: 0.6327
- precision: 0.6975
- recall: 0.6416
- f1: 0.6684
- roc_auc: 0.6824

## Baseline Metrics
- accuracy: 0.5770
- precision: 0.5770
- recall: 1.0000
- f1: 0.7317
- roc_auc: 0.5000

## Confusion Matrix

`[[278, 170], [219, 392]]`

## Classification Report

```text
              precision    recall  f1-score   support

           0     0.5594    0.6205    0.5884       448
           1     0.6975    0.6416    0.6684       611

    accuracy                         0.6327      1059
   macro avg     0.6284    0.6311    0.6284      1059
weighted avg     0.6391    0.6327    0.6345      1059

```

## Feature Importance
- age_diff: 0.114615
- sig_strike_defense_diff: 0.072722
- wins_last_5_diff: 0.068563
- takedowns_landed_per_fight_diff: 0.066721
- sig_strikes_landed_per_fight_diff: 0.066083
- sig_strikes_absorbed_per_fight_diff: 0.063181
- sig_strike_accuracy_diff: 0.058055
- control_time_seconds_per_fight_diff: 0.051726
- takedowns_absorbed_per_fight_diff: 0.046864
- takedown_defense_diff: 0.046578
- reach_cm_diff: 0.046156
- total_rounds_fought_diff: 0.045385
- avg_opponent_prior_win_rate_diff: 0.041443
- days_since_last_fight_diff: 0.039657
- knockdowns_per_fight_diff: 0.038171
- total_prior_fights_diff: 0.037091
- pct_wins_by_ko_diff: 0.035425
- height_cm_diff: 0.034517
- takedown_accuracy_diff: 0.027046
- submission_attempts_per_fight_diff: 0.000000
- win_streak_diff: 0.000000
- pct_wins_by_submission_diff: 0.000000
- pct_wins_by_decision_diff: 0.000000
- scheduled_rounds: 0.000000

## Diagnostico (evaluate.py)

Evaluacion diagnostica del modelo persistido (sin reentrenar). Reconstruye el mismo test slice cronologico de `train.py` y lo puntua con `model.joblib` (modelo + imputer + feature_columns guardados).

- Test rows: 1059
- Test date range: 2023-07-01 to 2026-06-20
- Decision threshold: 0.5

### Probabilistic metrics (test)
- Brier score: 0.2301  (lower is better; 0.25 = uninformed 0.5)
- Log loss: 0.6527  (lower is better)
- Accuracy: 0.6327

### Calibration curve (10 uniform bins)

Mean predicted probability vs. observed positive fraction per bin.

| Bin | Count | Mean predicted | Observed fraction |
| --- | ---: | ---: | ---: |
| [0.0, 0.1) | 0 | - | - |
| [0.1, 0.2) | 0 | - | - |
| [0.2, 0.3) | 3 | 0.2788 | 0.0000 |
| [0.3, 0.4) | 144 | 0.3658 | 0.3472 |
| [0.4, 0.5) | 350 | 0.4508 | 0.4829 |
| [0.5, 0.6) | 402 | 0.5485 | 0.6468 |
| [0.6, 0.7) | 157 | 0.6304 | 0.8217 |
| [0.7, 0.8) | 3 | 0.7071 | 1.0000 |
| [0.8, 0.9) | 0 | - | - |
| [0.9, 1.0) | 0 | - | - |

calibration_curve (non-empty bins, predicted -> observed): (0.279 -> 0.000), (0.366 -> 0.347), (0.451 -> 0.483), (0.548 -> 0.647), (0.630 -> 0.822), (0.707 -> 1.000)

### Breakdown by division (weight class)

| Division | N | Accuracy | Brier | Positive rate |
| --- | ---: | ---: | ---: | ---: |
| Lightweight | 157 | 0.6178 | 0.2302 | 0.5796 |
| Middleweight | 133 | 0.6767 | 0.2216 | 0.6090 |
| Welterweight | 128 | 0.6328 | 0.2260 | 0.5703 |
| Featherweight | 123 | 0.5447 | 0.2435 | 0.5610 |
| Bantamweight | 119 | 0.6387 | 0.2271 | 0.5294 |
| Women's Strawweight | 86 | 0.5930 | 0.2394 | 0.6395 |
| Flyweight | 77 | 0.6753 | 0.2354 | 0.5455 |
| Women's Flyweight | 64 | 0.6094 | 0.2264 | 0.6562 |
| Heavyweight | 59 | 0.6271 | 0.2307 | 0.5424 |
| Light Heavyweight | 56 | 0.6607 | 0.2436 | 0.5000 |
| Women's Bantamweight | 44 | 0.7500 | 0.2040 | 0.6136 |
| Catch Weight | 11 | 0.7273 | 0.2185 | 0.7273 |
| Women's Featherweight | 2 | 1.0000 | 0.1883 | 0.0000 |

### Breakdown by scheduled_rounds (3 vs 5)

Scheduled rounds taken from the `fights` table (the CSV feature is degenerate, all 3).

| Scheduled rounds | N | Accuracy | Brier | Positive rate |
| --- | ---: | ---: | ---: | ---: |
| 3 | 1058 | 0.6323 | 0.2301 | 0.5766 |
| 5 | 1 | 1.0000 | 0.2494 | 1.0000 |

### Breakdown by era (year ranges)

| Era | N | Accuracy | Brier | Positive rate |
| --- | ---: | ---: | ---: | ---: |
| 2020-2024 | 532 | 0.6165 | 0.2312 | 0.4850 |
| 2025+ | 527 | 0.6490 | 0.2290 | 0.6698 |
