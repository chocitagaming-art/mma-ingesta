# UFC Fight Winner Model Metrics

- Trained at: 2026-06-27
- xgboost version: 3.2.0
- scikit-learn version: 1.9.0
- Training rows: 3386
- Calibration-holdout rows: 847
- Test rows: 1059
- Train date range: 1995-07-14 to 2020-12-19
- Calibration-holdout date range: 2020-12-19 to 2023-06-24
- Test date range: 2023-07-01 to 2026-06-20
- Best params: {'colsample_bytree': 1.0, 'learning_rate': 0.05, 'max_depth': 3, 'n_estimators': 50, 'subsample': 0.8}

## Headline accuracy

The PRODUCTION-EQUIVALENT headline (symmetrized + calibrated accuracy) is reported in the `

## Diagnostico (evaluate.py)

Evaluacion diagnostica del modelo persistido (sin reentrenar). Reconstruye el mismo test slice cronologico de `train.py` y lo puntua con `model.joblib` (modelo + imputer + calibrator + feature_columns guardados), aplicando la simetrizacion de esquinas de produccion.

- Test rows: 1059
- Test date range: 2023-07-01 to 2026-06-20
- Decision threshold: 0.5

### HEADLINE (production-equivalent: symmetrized + calibrated (PRODUCTION-EQUIVALENT))
- Brier score: 0.2266  (lower is better; 0.25 = uninformed 0.5)
- Log loss: 0.6449  (lower is better)
- Accuracy: 0.6289

### Variant comparison {raw, symmetrized} x {uncalibrated, calibrated}

`symmetrized + calibrated` matches what api.predict serves and is the headline above; the others are diagnostic references.

| Variant | Brier | Log loss | Accuracy |
| --- | ---: | ---: | ---: |
| raw, uncalibrated | 0.2317 | 0.6558 | 0.6223 |
| symmetrized, uncalibrated | 0.2307 | 0.6537 | 0.6289 |
| raw, calibrated | 0.2273 | 0.6460 | 0.6232 |
| symmetrized + calibrated (PRODUCTION-EQUIVALENT) **<-**  | 0.2266 | 0.6449 | 0.6289 |

### Calibration curve (10 uniform bins)

Mean predicted probability vs. observed positive fraction per bin (headline variant).

| Bin | Count | Mean predicted | Observed fraction |
| --- | ---: | ---: | ---: |
| [0.0, 0.1) | 0 | - | - |
| [0.1, 0.2) | 2 | 0.1898 | 0.0000 |
| [0.2, 0.3) | 59 | 0.2655 | 0.3729 |
| [0.3, 0.4) | 208 | 0.3563 | 0.3750 |
| [0.4, 0.5) | 270 | 0.4494 | 0.5259 |
| [0.5, 0.6) | 230 | 0.5501 | 0.6391 |
| [0.6, 0.7) | 206 | 0.6427 | 0.7282 |
| [0.7, 0.8) | 80 | 0.7312 | 0.8625 |
| [0.8, 0.9) | 4 | 0.8167 | 0.7500 |
| [0.9, 1.0) | 0 | - | - |

calibration_curve (non-empty bins, predicted -> observed): (0.190 -> 0.000), (0.266 -> 0.373), (0.356 -> 0.375), (0.449 -> 0.526), (0.550 -> 0.639), (0.643 -> 0.728), (0.731 -> 0.863), (0.817 -> 0.750)

### Breakdown by division (weight class)

| Division | N | Accuracy | Brier | Positive rate |
| --- | ---: | ---: | ---: | ---: |
| Lightweight | 157 | 0.6306 | 0.2302 | 0.5796 |
| Middleweight | 133 | 0.6541 | 0.2132 | 0.6090 |
| Welterweight | 128 | 0.6406 | 0.2219 | 0.5703 |
| Featherweight | 123 | 0.5447 | 0.2484 | 0.5610 |
| Bantamweight | 119 | 0.6471 | 0.2260 | 0.5294 |
| Women's Strawweight | 86 | 0.5698 | 0.2385 | 0.6395 |
| Flyweight | 77 | 0.6753 | 0.2312 | 0.5455 |
| Women's Flyweight | 64 | 0.6250 | 0.2169 | 0.6562 |
| Heavyweight | 59 | 0.6102 | 0.2272 | 0.5424 |
| Light Heavyweight | 56 | 0.5714 | 0.2446 | 0.5000 |
| Women's Bantamweight | 44 | 0.8182 | 0.1761 | 0.6136 |
| Catch Weight | 11 | 0.6364 | 0.2038 | 0.7273 |
| Women's Featherweight | 2 | 1.0000 | 0.1881 | 0.0000 |

### Breakdown by scheduled_rounds (3 vs 5)

Scheduled rounds taken from the `fights` table. scheduled_rounds is NO LONGER a model feature (dropped as zero-importance); this is a segmentation label only.

| Scheduled rounds | N | Accuracy | Brier | Positive rate |
| --- | ---: | ---: | ---: | ---: |
| 3 | 1058 | 0.6295 | 0.2266 | 0.5766 |
| 5 | 1 | 0.0000 | 0.2701 | 1.0000 |

### Breakdown by era (year ranges)

| Era | N | Accuracy | Brier | Positive rate |
| --- | ---: | ---: | ---: | ---: |
| 2020-2024 | 532 | 0.6147 | 0.2287 | 0.4850 |
| 2025+ | 527 | 0.6433 | 0.2245 | 0.6698 |

## Features (20)

Pure model: NO odds are used as an input feature (odds feed only the separate Model-vs-Market visual).

- height_cm_diff
- reach_cm_diff
- age_diff
- sig_strikes_landed_per_fight_diff
- sig_strike_accuracy_diff
- knockdowns_per_fight_diff
- takedowns_landed_per_fight_diff
- takedown_accuracy_diff
- control_time_seconds_per_fight_diff
- wins_last_5_diff
- total_prior_fights_diff
- total_rounds_fought_diff
- pct_wins_by_ko_diff
- days_since_last_fight_diff
- ranking_position_diff
- sig_strikes_absorbed_per_fight_diff
- sig_strike_defense_diff
- takedowns_absorbed_per_fight_diff
- takedown_defense_diff
- avg_opponent_prior_win_rate_diff

## Model Metrics (raw, uncalibrated, single orientation - secondary)
- accuracy: 0.6223
- precision: 0.7025
- recall: 0.5990
- f1: 0.6466
- roc_auc: 0.6729

## Majority-class baseline

Predicts the train-majority class for every test row (no odds, no ranking heuristic). Accuracy = the test rate of that class; as a constant predictor ROC-AUC is 0.5 and a constant-0.5 probability has Brier 0.25.
- majority_class: 1
- accuracy (class rate): 0.5770
- roc_auc: 0.5000
- brier (always 0.5): 0.2500

## Confusion Matrix

`[[293, 155], [245, 366]]`

## Classification Report

```text
              precision    recall  f1-score   support

           0     0.5446    0.6540    0.5943       448
           1     0.7025    0.5990    0.6466       611

    accuracy                         0.6223      1059
   macro avg     0.6236    0.6265    0.6205      1059
weighted avg     0.6357    0.6223    0.6245      1059

```

## Feature Importance
- age_diff: 0.108727
- wins_last_5_diff: 0.076666
- sig_strike_defense_diff: 0.073726
- sig_strikes_absorbed_per_fight_diff: 0.063186
- sig_strikes_landed_per_fight_diff: 0.058864
- takedowns_landed_per_fight_diff: 0.054894
- total_rounds_fought_diff: 0.052306
- control_time_seconds_per_fight_diff: 0.051681
- days_since_last_fight_diff: 0.048965
- reach_cm_diff: 0.048447
- knockdowns_per_fight_diff: 0.047396
- takedown_defense_diff: 0.046763
- total_prior_fights_diff: 0.045359
- takedowns_absorbed_per_fight_diff: 0.043759
- avg_opponent_prior_win_rate_diff: 0.040925
- sig_strike_accuracy_diff: 0.039403
- pct_wins_by_ko_diff: 0.035121
- height_cm_diff: 0.033881
- takedown_accuracy_diff: 0.029933
- ranking_position_diff: 0.000000