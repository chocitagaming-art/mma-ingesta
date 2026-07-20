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

## Modelo de metodo (train_method.py)

Modelo de METODO de victoria (decision / ko / submission). ENSEMBLE 50%/50% de XGBoost multi:softprob y regresion logistica multinomial (C=0.001), cada mitad calibrada por separado y servido simetrizado por esquinas. Clases (orden del target y de method_classes): ['decision', 'ko', 'submission'].

- Trained at: 2026-07-20
- Training rows: 3427 | Calibration rows: 857 | Test rows: 1072
- Test date range: 2023-07-29 to 2026-07-18
- Best params: {'colsample_bytree': 0.8, 'learning_rate': 0.1, 'max_depth': 2, 'n_estimators': 50, 'subsample': 1.0}
- Calibration method: sigmoid

### HEADLINE (production-equivalent: symmetrized + calibrated)
- Accuracy: 0.5392
- Log loss: 0.9604
- Macro AUC (OvR): 0.6437

### Baselines (constant predictors)
- majority class (decision) accuracy: 0.5326
- train-priors log loss: 1.0097
- uniform log loss: 1.0986

### Variants {raw, symmetrized} x {uncalibrated, calibrated}

| Variant | Accuracy | Log loss | Macro AUC |
| --- | ---: | ---: | ---: |
| raw, uncalibrated | 0.5429 | 0.9663 | 0.6366 |
| symmetrized, uncalibrated | 0.5420 | 0.9633 | 0.6441 |
| raw, calibrated | 0.5466 | 0.9638 | 0.6358 |
| symmetrized + calibrated (PRODUCTION-EQUIVALENT) **<-**  | 0.5392 | 0.9604 | 0.6437 |

### Confusion matrix (rows=true, cols=pred; order ['decision', 'ko', 'submission'])

`[[503, 51, 17], [237, 60, 5], [163, 21, 15]]`

### Classification report

```text
              precision    recall  f1-score   support

    decision     0.5570    0.8809    0.6825       571
          ko     0.4545    0.1987    0.2765       302
  submission     0.4054    0.0754    0.1271       199

    accuracy                         0.5392      1072
   macro avg     0.4723    0.3850    0.3620      1072
weighted avg     0.5000    0.5392    0.4650      1072

```

### Feature importance (37 features)
- avg_fight_duration_s_sum: 0.086625
- pct_wins_by_ko_sum: 0.081012
- pct_went_the_distance_sum: 0.080832
- weight_kg: 0.064608
- submission_attempts_per_fight_sum: 0.049829
- sig_strikes_landed_per_fight_sum: 0.037707
- pct_losses_by_ko_sum: 0.036273
- knockdowns_per_fight_sum: 0.033754
- total_prior_fights_diff: 0.033154
- pct_wins_by_submission_sum: 0.033139
- pct_wins_by_ko_diff: 0.030226
- takedowns_landed_per_fight_diff: 0.028146
- total_prior_fights_sum: 0.027773
- control_time_seconds_per_fight_sum: 0.025676
- wins_last_5_diff: 0.025108
- days_since_last_fight_diff: 0.024776
- pct_losses_by_submission_sum: 0.023490
- control_time_seconds_per_fight_diff: 0.023409
- sig_strike_accuracy_diff: 0.022811
- takedowns_landed_per_fight_sum: 0.022614
- age_diff: 0.020719
- avg_opponent_prior_win_rate_diff: 0.020683
- reach_cm_diff: 0.020514
- total_rounds_fought_diff: 0.019593
- is_title_fight: 0.019316
- sig_strikes_absorbed_per_fight_diff: 0.019219
- sig_strikes_landed_per_fight_diff: 0.018933
- pct_wins_by_decision_sum: 0.018906
- takedowns_absorbed_per_fight_diff: 0.018868
- takedown_accuracy_diff: 0.018637
- takedown_defense_diff: 0.013649
- height_cm_diff: 0.000000
- knockdowns_per_fight_diff: 0.000000
- ranking_position_diff: 0.000000
- sig_strike_defense_diff: 0.000000
- scheduled_rounds: 0.000000
- is_female_division: 0.000000
