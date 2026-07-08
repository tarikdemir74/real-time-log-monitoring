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
    -- detected_at: the underlying event's own timestamp (as logged by DemoApi), not when this
    -- row was written. processed_at: the wall-clock time LogProcessor actually processed and
    -- stored this row (set from datetime.now(UTC) in database.py, not a DB-side default, so it
    -- reflects application processing time specifically).
    detected_at TIMESTAMPTZ NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    window_end TIMESTAMPTZ NOT NULL,
    endpoint TEXT NOT NULL,
    anomaly_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    detection_method TEXT NOT NULL,
    anomaly_score DOUBLE PRECISION,
    -- score_unit: what anomaly_score actually measures for this row (it is not on a comparable
    -- scale across detection methods) - e.g. "response_time_ms", "error_rate",
    -- "isolation_forest_decision_function", "hybrid_tier_rank". Set by whichever detector
    -- produced the anomaly; see each module in src/LogProcessor/ for the exact value used.
    score_unit TEXT,
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
