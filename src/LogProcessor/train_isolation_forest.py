import sys
import time

import joblib
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.model_selection import train_test_split
from sqlalchemy import create_engine

import config
import ml_features

MIN_TRAINING_ROWS = 10
TEST_SIZE = 0.2
RANDOM_STATE = 42
CONTAMINATION = "auto"

# A row's window is "known anomalous" if any anomaly (rule-based or ML) was ever recorded
# for that (endpoint, window_start) pair - every anomalies row carries window_start/endpoint
# regardless of anomaly_type, so this single EXISTS check covers both per-request and
# window-level anomalies. Excluding these rows keeps the model from learning "anomalous" as
# part of its definition of normal, per the design doc's retraining rule (section 7).
TRAINING_QUERY = """
    SELECT
        r.id, r.timestamp, r.endpoint, r.status_code, r.response_time_ms,
        COALESCE(a.request_count, 1) AS window_request_count,
        COALESCE(a.error_rate, 0) AS window_error_rate,
        EXISTS (
            SELECT 1 FROM anomalies an
            WHERE an.endpoint = r.endpoint
              AND an.window_start = date_trunc('minute', r.timestamp)
        ) AS is_anomalous_window
    FROM logs_raw r
    LEFT JOIN logs_agg a
        ON a.endpoint = r.endpoint
        AND a.window_start = date_trunc('minute', r.timestamp)
    ORDER BY r.id
"""


def load_training_data() -> pd.DataFrame:
    url = (
        f"postgresql+psycopg2://{config.POSTGRES_USER}:{config.POSTGRES_PASSWORD}"
        f"@{config.POSTGRES_HOST}:{config.POSTGRES_PORT}/{config.POSTGRES_DB}"
    )
    engine = create_engine(url)
    try:
        return pd.read_sql(TRAINING_QUERY, engine)
    finally:
        engine.dispose()


def build_feature_matrix(df: pd.DataFrame) -> list:
    return [
        ml_features.build_feature_vector(
            status_code=row.status_code,
            response_time_ms=row.response_time_ms,
            endpoint=row.endpoint,
            window_request_count=row.window_request_count,
            window_error_rate=row.window_error_rate,
        )
        for row in df.itertuples()
    ]


def main() -> None:
    print("[TrainIsolationForest] Loading historical data from logs_raw (joined with logs_agg window stats)...")
    df = load_training_data()
    rows_loaded = len(df)
    print(f"[TrainIsolationForest] Rows loaded: {rows_loaded}")

    clean_df = df[~df["is_anomalous_window"]]
    rows_excluded = rows_loaded - len(clean_df)
    print(f"[TrainIsolationForest] Rows excluded (known anomalous windows): {rows_excluded}")

    if len(clean_df) < MIN_TRAINING_ROWS:
        print(
            f"[TrainIsolationForest] Not enough clean (non-anomalous-window) data to train "
            f"(need >= {MIN_TRAINING_ROWS} rows, have {len(clean_df)}). Run TrafficSimulator "
            f"in 'normal' mode to generate more clean traffic first."
        )
        sys.exit(1)

    feature_matrix = build_feature_matrix(clean_df)
    print(
        f"[TrainIsolationForest] Built feature matrix: {len(feature_matrix)} rows x "
        f"{len(ml_features.FEATURE_NAMES)} features"
    )
    print(f"[TrainIsolationForest] Features: {ml_features.FEATURE_NAMES}")

    train_features, holdout_features = train_test_split(
        feature_matrix, test_size=TEST_SIZE, random_state=RANDOM_STATE
    )
    print(f"[TrainIsolationForest] Rows used for training: {len(train_features)}")
    print(f"[TrainIsolationForest] Rows used for evaluation (holdout): {len(holdout_features)}")
    print(f"[TrainIsolationForest] Contamination: {CONTAMINATION}")

    start = time.time()
    model = IsolationForest(n_estimators=100, contamination=CONTAMINATION, random_state=RANDOM_STATE)
    model.fit(train_features)
    duration = time.time() - start
    print(f"[TrainIsolationForest] Training complete in {duration:.2f}s")

    train_scores = model.decision_function(train_features)
    train_predictions = model.predict(train_features)
    train_flagged = int((train_predictions == -1).sum())
    print(
        f"[TrainIsolationForest] Training set score distribution: min={train_scores.min():.4f} "
        f"max={train_scores.max():.4f} mean={train_scores.mean():.4f}"
    )
    print(
        f"[TrainIsolationForest] Training set flagged anomalous: "
        f"{train_flagged}/{len(train_features)} ({train_flagged / len(train_features):.1%})"
    )

    holdout_predictions = model.predict(holdout_features)
    holdout_flagged = int((holdout_predictions == -1).sum())
    holdout_anomaly_rate = holdout_flagged / len(holdout_features) if holdout_features else 0.0
    print(
        f"[TrainIsolationForest] Holdout set flagged anomalous: "
        f"{holdout_flagged}/{len(holdout_features)} ({holdout_anomaly_rate:.1%})"
    )

    joblib.dump(
        {
            "model": model,
            "feature_names": ml_features.FEATURE_NAMES,
            "trained_rows": len(train_features),
            "rows_loaded": rows_loaded,
            "rows_excluded_anomalous": rows_excluded,
            "holdout_rows": len(holdout_features),
            "contamination": CONTAMINATION,
            "holdout_anomaly_rate": holdout_anomaly_rate,
        },
        config.ISOLATION_FOREST_MODEL_PATH,
    )
    print(f"[TrainIsolationForest] Model saved to '{config.ISOLATION_FOREST_MODEL_PATH}'")


if __name__ == "__main__":
    main()
