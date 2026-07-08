from datetime import datetime, timezone

import psycopg2

import anomaly_rules
import config
import dynamic_baseline
import hybrid_decision
import isolation_forest_detector

_connection = None

# --- Window-level anomaly short-circuit cache -------------------------------
# Window-level rules (high_error_rate_window, dynamic_baseline_*) are
# non-upgradeable: once inserted for a given (window_start, endpoint,
# anomaly_type), re-running their evaluation logic can never change the
# outcome, since a later ON CONFLICT DO NOTHING would just discard it anyway.
# These two in-process caches let persist_log_entry skip that redundant
# evaluation (and, for dynamic_baseline, an entire extra SQL query) once the
# outcome for a window is already known - a pure performance optimization,
# not a correctness mechanism. The database's own unique indexes (and, for
# hybrid_anomaly, the upgrade-only WHERE clause) remain the sole source of
# truth: if this cache is empty or stale (e.g. right after a restart), the
# exact same result is still reached, just via one extra query/evaluation
# instead of a cache hit. Grows by a handful of entries per (window, endpoint)
# over the process's lifetime; not persisted or bounded, since at this
# project's scale that growth is negligible (see CLAUDE.md for the trade-off).
_resolved_window_anomaly_types = set()
_hybrid_max_score_by_window = {}

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
        detected_at, processed_at, window_start, window_end, endpoint,
        anomaly_type, severity, detection_method, anomaly_score, score_unit, description, request_id
    )
    VALUES (
        %(detected_at)s, %(processed_at)s, %(window_start)s, %(window_end)s, %(endpoint)s,
        %(anomaly_type)s, %(severity)s, %(detection_method)s, %(anomaly_score)s, %(score_unit)s, %(description)s,
        %(request_id)s
    )
    ON CONFLICT (request_id, anomaly_type) WHERE request_id IS NOT NULL DO NOTHING
"""

INSERT_WINDOW_ANOMALY_SQL = """
    INSERT INTO anomalies (
        detected_at, processed_at, window_start, window_end, endpoint,
        anomaly_type, severity, detection_method, anomaly_score, score_unit, description, request_id
    )
    VALUES (
        %(detected_at)s, %(processed_at)s, %(window_start)s, %(window_end)s, %(endpoint)s,
        %(anomaly_type)s, %(severity)s, %(detection_method)s, %(anomaly_score)s, %(score_unit)s, %(description)s,
        %(request_id)s
    )
    ON CONFLICT (window_start, endpoint, anomaly_type) WHERE request_id IS NULL DO NOTHING
"""

# Hybrid anomalies use the same window-level dedup index as INSERT_WINDOW_ANOMALY_SQL, but
# upgrade in place instead of no-op'ing on conflict: if a later message-processing pass in the
# same window produces a strictly higher-confidence tier (higher anomaly_score), the existing
# row is updated to that tier. The WHERE clause on the DO UPDATE guarantees this can only ever
# upgrade, never downgrade - a tie or a lower tier leaves the stored row untouched.
INSERT_HYBRID_ANOMALY_SQL = """
    INSERT INTO anomalies (
        detected_at, processed_at, window_start, window_end, endpoint,
        anomaly_type, severity, detection_method, anomaly_score, score_unit, description, request_id
    )
    VALUES (
        %(detected_at)s, %(processed_at)s, %(window_start)s, %(window_end)s, %(endpoint)s,
        %(anomaly_type)s, %(severity)s, %(detection_method)s, %(anomaly_score)s, %(score_unit)s, %(description)s,
        %(request_id)s
    )
    ON CONFLICT (window_start, endpoint, anomaly_type) WHERE request_id IS NULL DO UPDATE SET
        detected_at = EXCLUDED.detected_at,
        processed_at = EXCLUDED.processed_at,
        severity = EXCLUDED.severity,
        anomaly_score = EXCLUDED.anomaly_score,
        score_unit = EXCLUDED.score_unit,
        description = EXCLUDED.description
    WHERE EXCLUDED.anomaly_score > anomalies.anomaly_score
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

    # Wall-clock time LogProcessor is actually handling this message - distinct from
    # detected_at, which is the event's own timestamp. One value per call so every anomaly
    # produced from this message shares the same processed_at.
    processed_at = datetime.now(timezone.utc)

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

            # --- Window-level short-circuit checks (see cache comment above) ---
            high_error_rate_resolved = (
                (window_start, endpoint, "high_error_rate_window") in _resolved_window_anomaly_types
            )
            dynamic_baseline_resolved = (
                (window_start, endpoint, "dynamic_baseline_latency") in _resolved_window_anomaly_types
                and (window_start, endpoint, "dynamic_baseline_error_rate") in _resolved_window_anomaly_types
            )
            hybrid_resolved = (
                _hybrid_max_score_by_window.get((window_start, endpoint), 0.0)
                >= hybrid_decision.TIER_SCORE["CRITICAL"]
            )

            request_anomalies = anomaly_rules.evaluate_request_rules(entry, detected_at)

            window_anomalies = [] if high_error_rate_resolved else anomaly_rules.evaluate_window_rules(
                endpoint, detected_at, updated_error_rate, updated_request_count
            )

            if dynamic_baseline_resolved:
                pass  # both dynamic_baseline anomaly types already recorded for this window; skip the query entirely
            else:
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
                window_anomalies += dynamic_baseline.evaluate_window_baseline(
                    endpoint, detected_at, updated_avg_latency, updated_error_rate, historical_windows
                )

            ml_anomalies = isolation_forest_detector.evaluate(
                entry, detected_at, updated_request_count, updated_error_rate
            )

            hybrid_anomalies = [] if hybrid_resolved else hybrid_decision.evaluate(
                endpoint, detected_at, request_anomalies + window_anomalies + ml_anomalies
            )

            for anomaly in request_anomalies + ml_anomalies:
                params = {
                    **anomaly, "window_start": window_start, "window_end": window_end,
                    "processed_at": processed_at,
                }
                cursor.execute(INSERT_REQUEST_ANOMALY_SQL, params)
                anomaly["inserted"] = cursor.rowcount > 0
                detected_anomalies.append(anomaly)

            for anomaly in window_anomalies:
                params = {
                    **anomaly, "window_start": window_start, "window_end": window_end,
                    "processed_at": processed_at,
                }
                cursor.execute(INSERT_WINDOW_ANOMALY_SQL, params)
                anomaly["inserted"] = cursor.rowcount > 0
                # Cache regardless of whether this call inserted it or hit an
                # existing row via ON CONFLICT DO NOTHING - either way, this
                # (window, endpoint, type) is now confirmed resolved in the
                # database, since this anomaly_type is never upgradeable.
                _resolved_window_anomaly_types.add((window_start, endpoint, anomaly["anomaly_type"]))
                detected_anomalies.append(anomaly)

            for anomaly in hybrid_anomalies:
                params = {
                    **anomaly, "window_start": window_start, "window_end": window_end,
                    "processed_at": processed_at,
                }
                cursor.execute(INSERT_HYBRID_ANOMALY_SQL, params)
                # True on a fresh insert OR a successful upgrade; false if a conflicting row
                # already existed at an equal-or-higher tier (the WHERE clause skipped the update).
                anomaly["inserted"] = cursor.rowcount > 0
                if anomaly["inserted"]:
                    _hybrid_max_score_by_window[(window_start, endpoint)] = anomaly["anomaly_score"]
                detected_anomalies.append(anomaly)

        connection.commit()
        return detected_anomalies
    except Exception:
        connection.rollback()
        _connection = None
        raise
