import statistics

ROLLING_WINDOW_COUNT = 10
MIN_ROLLING_WINDOWS = 5
STD_MULTIPLIER = 3


def evaluate_window_baseline(
    endpoint: str,
    detected_at,
    current_avg_latency: float,
    current_error_rate: float,
    historical_windows: list,
) -> list:
    if len(historical_windows) < MIN_ROLLING_WINDOWS:
        return []

    anomalies = []
    sample_size = len(historical_windows)

    latencies = [w["avg_latency"] for w in historical_windows]
    latency_mean = statistics.mean(latencies)
    latency_std = statistics.pstdev(latencies)
    latency_threshold = latency_mean + STD_MULTIPLIER * latency_std

    if current_avg_latency > latency_threshold:
        anomalies.append({
            "detected_at": detected_at,
            "endpoint": endpoint,
            "anomaly_type": "dynamic_baseline_latency",
            "severity": "medium",
            "detection_method": "dynamic_baseline",
            "anomaly_score": float(current_avg_latency),
            "description": (
                f"avg_latency {current_avg_latency:.1f}ms > rolling baseline "
                f"{latency_mean:.1f}ms + {STD_MULTIPLIER}*{latency_std:.1f}ms "
                f"(threshold {latency_threshold:.1f}ms) over last {sample_size} windows"
            ),
            "request_id": None,
        })

    error_rates = [w["error_rate"] for w in historical_windows]
    error_rate_mean = statistics.mean(error_rates)
    error_rate_std = statistics.pstdev(error_rates)
    error_rate_threshold = error_rate_mean + STD_MULTIPLIER * error_rate_std

    if current_error_rate > error_rate_threshold:
        anomalies.append({
            "detected_at": detected_at,
            "endpoint": endpoint,
            "anomaly_type": "dynamic_baseline_error_rate",
            "severity": "high",
            "detection_method": "dynamic_baseline",
            "anomaly_score": float(current_error_rate),
            "description": (
                f"error_rate {current_error_rate:.2f} > rolling baseline "
                f"{error_rate_mean:.2f} + {STD_MULTIPLIER}*{error_rate_std:.2f} "
                f"(threshold {error_rate_threshold:.2f}) over last {sample_size} windows"
            ),
            "request_id": None,
        })

    return anomalies
