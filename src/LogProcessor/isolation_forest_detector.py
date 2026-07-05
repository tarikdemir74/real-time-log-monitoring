import os

import joblib

import config
import ml_features

_model_bundle = None
_model_load_attempted = False


def _load_model():
    global _model_bundle, _model_load_attempted
    if _model_load_attempted:
        return _model_bundle

    _model_load_attempted = True
    model_path = config.ISOLATION_FOREST_MODEL_PATH

    if not os.path.exists(model_path):
        print(
            f"[LogProcessor] Isolation Forest model not found at '{model_path}'. "
            f"Skipping ML-based anomaly detection until a model is trained "
            f"(run: python train_isolation_forest.py). Rule-based detection is unaffected."
        )
        return None

    try:
        _model_bundle = joblib.load(model_path)
        holdout_rate = _model_bundle.get("holdout_anomaly_rate")
        holdout_suffix = f", holdout anomaly rate {holdout_rate:.1%}" if holdout_rate is not None else ""
        print(
            f"[LogProcessor] Loaded Isolation Forest model from '{model_path}' "
            f"(trained on {_model_bundle.get('trained_rows')} rows{holdout_suffix})."
        )
    except Exception as exc:
        print(f"[LogProcessor] Failed to load Isolation Forest model ({exc}); skipping ML-based detection.")
        _model_bundle = None

    return _model_bundle


def evaluate(entry: dict, detected_at, window_request_count: int, window_error_rate: float) -> list:
    bundle = _load_model()
    if bundle is None:
        return []

    try:
        model = bundle["model"]
        features = ml_features.build_feature_vector(
            status_code=entry.get("statusCode") or 0,
            response_time_ms=entry.get("responseTimeMs") or 0,
            endpoint=entry.get("endpoint"),
            window_request_count=window_request_count,
            window_error_rate=window_error_rate,
        )

        prediction = model.predict([features])[0]
        if prediction != -1:
            return []

        score = float(model.decision_function([features])[0])
    except Exception as exc:
        print(f"[LogProcessor] Isolation Forest inference failed ({exc}); skipping ML-based detection for this event.")
        return []

    endpoint = entry.get("endpoint")
    return [{
        "detected_at": detected_at,
        "endpoint": endpoint,
        "anomaly_type": "isolation_forest_anomaly",
        "severity": "medium",
        "detection_method": "isolation_forest",
        "anomaly_score": score,
        "description": (
            f"Isolation Forest flagged this request as an outlier (score={score:.4f}, more negative = "
            f"more anomalous). Features: status_code={entry.get('statusCode')}, "
            f"response_time_ms={entry.get('responseTimeMs')}, endpoint={endpoint}, "
            f"window_request_count={window_request_count}, window_error_rate={window_error_rate:.2f}"
        ),
        "request_id": entry.get("requestId"),
    }]
