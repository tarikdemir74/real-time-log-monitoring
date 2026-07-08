HIGH_LATENCY_THRESHOLD_MS = 1500
ERROR_STATUS_THRESHOLD = 500
HIGH_ERROR_RATE_THRESHOLD = 0.5
HIGH_ERROR_RATE_MIN_REQUESTS = 3


def evaluate_request_rules(entry: dict, detected_at) -> list:
    anomalies = []

    response_time_ms = entry.get("responseTimeMs") or 0
    status_code = entry.get("statusCode") or 0
    endpoint = entry.get("endpoint")
    request_id = entry.get("requestId")

    if response_time_ms >= HIGH_LATENCY_THRESHOLD_MS:
        anomalies.append({
            "detected_at": detected_at,
            "endpoint": endpoint,
            "anomaly_type": "high_latency",
            "severity": "medium",
            "detection_method": "rule_based",
            "anomaly_score": float(response_time_ms),
            "score_unit": "response_time_ms",
            "description": (
                f"Response time {response_time_ms}ms >= threshold "
                f"{HIGH_LATENCY_THRESHOLD_MS}ms"
            ),
            "request_id": request_id,
        })

    if status_code >= ERROR_STATUS_THRESHOLD:
        anomalies.append({
            "detected_at": detected_at,
            "endpoint": endpoint,
            "anomaly_type": "error_response",
            "severity": "high",
            "detection_method": "rule_based",
            "anomaly_score": float(status_code),
            "score_unit": "status_code",
            "description": (
                f"Status code {status_code} >= threshold {ERROR_STATUS_THRESHOLD}"
            ),
            "request_id": request_id,
        })

    return anomalies


def evaluate_window_rules(endpoint: str, detected_at, error_rate: float, request_count: int) -> list:
    anomalies = []

    if error_rate >= HIGH_ERROR_RATE_THRESHOLD and request_count >= HIGH_ERROR_RATE_MIN_REQUESTS:
        anomalies.append({
            "detected_at": detected_at,
            "endpoint": endpoint,
            "anomaly_type": "high_error_rate_window",
            "severity": "high",
            "detection_method": "rule_based",
            "anomaly_score": float(error_rate),
            "score_unit": "error_rate",
            "description": (
                f"Error rate {error_rate:.2f} over {request_count} requests >= threshold "
                f"{HIGH_ERROR_RATE_THRESHOLD} (min {HIGH_ERROR_RATE_MIN_REQUESTS} requests)"
            ),
            "request_id": None,
        })

    return anomalies
