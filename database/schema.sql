CREATE TABLE IF NOT EXISTS logs_raw (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    endpoint TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    response_time_ms INTEGER NOT NULL,
    user_id TEXT,
    ip TEXT,
    message TEXT
);

CREATE TABLE IF NOT EXISTS logs_agg (
    id SERIAL PRIMARY KEY,
    window_start TIMESTAMPTZ NOT NULL,
    window_end TIMESTAMPTZ NOT NULL,
    endpoint TEXT NOT NULL,
    request_count INTEGER NOT NULL,
    error_count INTEGER NOT NULL,
    error_rate DOUBLE PRECISION NOT NULL,
    avg_latency DOUBLE PRECISION NOT NULL,
    max_response_time_ms INTEGER NOT NULL,
    p95_latency DOUBLE PRECISION,
    unique_ip_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE (window_start, endpoint)
);

CREATE TABLE IF NOT EXISTS anomalies (
    id SERIAL PRIMARY KEY,
    detected_at TIMESTAMPTZ NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    window_end TIMESTAMPTZ NOT NULL,
    endpoint TEXT NOT NULL,
    anomaly_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    detection_method TEXT NOT NULL,
    anomaly_score DOUBLE PRECISION,
    description TEXT,
    request_id TEXT
);

-- Dedupes per-request anomalies (e.g. high_latency, error_response) on retry/redelivery of the same message.
CREATE UNIQUE INDEX IF NOT EXISTS anomalies_request_dedup_idx
    ON anomalies (request_id, anomaly_type)
    WHERE request_id IS NOT NULL;

-- Dedupes window-level anomalies (e.g. high_error_rate_window) so a window fires at most once per anomaly_type.
CREATE UNIQUE INDEX IF NOT EXISTS anomalies_window_dedup_idx
    ON anomalies (window_start, endpoint, anomaly_type)
    WHERE request_id IS NULL;
