# Design doc (section 6.3) specifies request_count, error_rate, avg_latency, p95_latency,
# unique_ip_count, encoded endpoint as the Isolation Forest feature set. We substitute
# response_time_ms/status_code (per-request, not window-aggregated) for avg_latency, and
# window_request_count/window_error_rate for request_count/error_rate, because:
#   - p95_latency is not computed anywhere yet (logs_agg.p95_latency is always NULL).
#   - unique_ip_count is always 0 - DemoApi's RequestLogEntry has no Ip field, so there is
#     no IP data to count unique values from.
# Once IP capture and p95 computation exist upstream, this feature set should be revisited
# to match the design doc rather than carrying this stopgap indefinitely.
KNOWN_ENDPOINTS = ["/api/cart/add", "/api/login", "/api/payment/checkout", "/api/products"]

FEATURE_NAMES = (
    ["response_time_ms", "status_code"]
    + [f"endpoint_{e}" for e in KNOWN_ENDPOINTS]
    + ["window_request_count", "window_error_rate"]
)


def build_feature_vector(
    status_code,
    response_time_ms,
    endpoint,
    window_request_count,
    window_error_rate,
) -> list:
    endpoint_one_hot = [1.0 if endpoint == known else 0.0 for known in KNOWN_ENDPOINTS]
    return [
        float(response_time_ms),
        float(status_code),
        *endpoint_one_hot,
        float(window_request_count),
        float(window_error_rate),
    ]
