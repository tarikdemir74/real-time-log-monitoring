import psycopg2

import anomaly_rules
import config
import dynamic_baseline
import hybrid_decision
import isolation_forest_detector

_connection = None

INSERT_LOG_RAW_SQL = """
    INSERT INTO logs_raw (timestamp, endpoint, status_code, response_time_ms, user_id, ip, message)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
"""

UPSERT_LOG_AGG_SQL = """
    INSERT INTO logs_agg (
        window_start, window_end, endpoint,
        request_count, error_count, error_rate, avg_latency, max_response_time_ms, unique_ip_count
    )
    VALUES (
        %(window_start)s, %(window_end)s, %(endpoint)s,
        1, %(error_increment)s, %(error_rate)s, %(response_time_ms)s, %(response_time_ms)s, 0
    )
    ON CONFLICT (window_start, endpoint) DO UPDATE SET
        window_end = EXCLUDED.window_end,
        request_count = logs_agg.request_count + 1,
        error_count = logs_agg.error_count + EXCLUDED.error_count,
        avg_latency = (logs_agg.avg_latency * logs_agg.request_count + EXCLUDED.avg_latency)
            / (logs_agg.request_count + 1),
        max_response_time_ms = GREATEST(logs_agg.max_response_time_ms, EXCLUDED.max_response_time_ms),
        error_rate = (logs_agg.error_count + EXCLUDED.error_count)::double precision
            / (logs_agg.request_count + 1)
    RETURNING error_rate, request_count, avg_latency
"""

SELECT_RECENT_WINDOWS_SQL = """
    SELECT avg_latency, error_rate
    FROM logs_agg
    WHERE endpoint = %(endpoint)s AND window_start < %(window_start)s
    ORDER BY window_start DESC
    LIMIT %(limit)s
"""

INSERT_REQUEST_ANOMALY_SQL = """
    INSERT INTO anomalies (
        detected_at, window_start, window_end, endpoint,
        anomaly_type, severity, detection_method, anomaly_score, description, request_id
    )
    VALUES (
        %(detected_at)s, %(window_start)s, %(window_end)s, %(endpoint)s,
        %(anomaly_type)s, %(severity)s, %(detection_method)s, %(anomaly_score)s, %(description)s, %(request_id)s
    )
    ON CONFLICT (request_id, anomaly_type) WHERE request_id IS NOT NULL DO NOTHING
"""

INSERT_WINDOW_ANOMALY_SQL = """
    INSERT INTO anomalies (
        detected_at, window_start, window_end, endpoint,
        anomaly_type, severity, detection_method, anomaly_score, description, request_id
    )
    VALUES (
        %(detected_at)s, %(window_start)s, %(window_end)s, %(endpoint)s,
        %(anomaly_type)s, %(severity)s, %(detection_method)s, %(anomaly_score)s, %(description)s, %(request_id)s
    )
    ON CONFLICT (window_start, endpoint, anomaly_type) WHERE request_id IS NULL DO NOTHING
"""


def _get_connection():
    global _connection
    if _connection is None or _connection.closed:
        _connection = psycopg2.connect(
            host=config.POSTGRES_HOST,
            port=config.POSTGRES_PORT,
            user=config.POSTGRES_USER,
            password=config.POSTGRES_PASSWORD,
            dbname=config.POSTGRES_DB,
        )
    return _connection


def persist_log_entry(entry: dict, window_start, window_end, detected_at) -> list:
    global _connection
    connection = _get_connection()

    status_code = entry.get("statusCode") or 0
    response_time_ms = entry.get("responseTimeMs") or 0
    error_increment = 1 if status_code >= 400 else 0
    endpoint = entry.get("endpoint")

    detected_anomalies = []

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                INSERT_LOG_RAW_SQL,
                (
                    entry.get("timestamp"),
                    endpoint,
                    status_code,
                    response_time_ms,
                    entry.get("userId"),
                    entry.get("ip"),
                    entry.get("message"),
                ),
            )

            cursor.execute(
                UPSERT_LOG_AGG_SQL,
                {
                    "window_start": window_start,
                    "window_end": window_end,
                    "endpoint": endpoint,
                    "error_increment": error_increment,
                    "error_rate": float(error_increment),
                    "response_time_ms": response_time_ms,
                },
            )
            updated_error_rate, updated_request_count, updated_avg_latency = cursor.fetchone()

            cursor.execute(
                SELECT_RECENT_WINDOWS_SQL,
                {
                    "endpoint": endpoint,
                    "window_start": window_start,
                    "limit": dynamic_baseline.ROLLING_WINDOW_COUNT,
                },
            )
            historical_windows = [
                {"avg_latency": row[0], "error_rate": row[1]} for row in cursor.fetchall()
            ]

            request_anomalies = anomaly_rules.evaluate_request_rules(entry, detected_at)
            window_anomalies = anomaly_rules.evaluate_window_rules(
                endpoint, detected_at, updated_error_rate, updated_request_count
            )
            window_anomalies += dynamic_baseline.evaluate_window_baseline(
                endpoint, detected_at, updated_avg_latency, updated_error_rate, historical_windows
            )
            ml_anomalies = isolation_forest_detector.evaluate(
                entry, detected_at, updated_request_count, updated_error_rate
            )
            window_anomalies += hybrid_decision.evaluate(
                endpoint, detected_at, request_anomalies + window_anomalies + ml_anomalies
            )

            for anomaly in request_anomalies + ml_anomalies:
                params = {**anomaly, "window_start": window_start, "window_end": window_end}
                cursor.execute(INSERT_REQUEST_ANOMALY_SQL, params)
                anomaly["inserted"] = cursor.rowcount > 0
                detected_anomalies.append(anomaly)

            for anomaly in window_anomalies:
                params = {**anomaly, "window_start": window_start, "window_end": window_end}
                cursor.execute(INSERT_WINDOW_ANOMALY_SQL, params)
                anomaly["inserted"] = cursor.rowcount > 0
                detected_anomalies.append(anomaly)

        connection.commit()
        return detected_anomalies
    except Exception:
        connection.rollback()
        _connection = None
        raise
