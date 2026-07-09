"""
Replay-based batch-processing simulator - evaluation/research tool only.

Answers RQ3 ("how does stream processing compare to batch processing in terms
of detection latency and responsiveness?") by replaying already-persisted
logs_raw/logs_agg data and running the *exact same* detection functions the
live LogProcessor uses, but only at fixed batch-interval boundaries instead of
immediately per message.

This is NOT a second production pipeline:
  - It never touches RabbitMQ, never runs continuously, never writes to the
    `anomalies` table. It reads logs_raw/logs_agg (already written by the
    real, unchanged streaming LogProcessor) and prints a JSON report of what
    a batch job would have detected and when, to stdout.
  - It imports anomaly_rules.py, dynamic_baseline.py, isolation_forest_detector.py,
    and hybrid_decision.py unchanged - the detection logic is 100% shared with
    the live system. Only *when* those functions are called differs.
  - The live LogProcessor (main.py / rabbitmq_consumer.py / database.py) is
    completely unaffected by this file's existence; nothing here is imported
    by, or imports, the live consume/persist path.

Batch semantics (see CLAUDE.md "Batch simulation (RQ3)" for the full writeup):
  - Time is sliced into fixed-size intervals (--interval-minutes, default 5).
  - A 1-minute logs_agg window is only "closeable" once fully contained in an
    elapsed interval (window_end <= interval cutoff) - exactly mirroring how a
    real batch job could only aggregate data that has actually arrived.
  - Once a window closes, ALL of its detection runs in one pass, using the
    window's *final* aggregate (unlike stream mode, which scores every request
    against whatever partial aggregate existed at that instant).
  - Window-level rules (high_error_rate_window, dynamic_baseline_*) and the
    hybrid decision are evaluated once per (window, endpoint) - not once per
    request - since a batch pass naturally sees the whole window at once
    rather than replaying per-request arrival order. Request-level rules
    (high_latency, error_response, isolation_forest) still run once per
    request, exactly as in stream mode, just deferred until batch time.

Usage (run inside the logprocessor container, which already has psycopg2/
scikit-learn/joblib installed - same convention as train_isolation_forest.py):
    docker compose exec -T logprocessor python batch_simulator.py \\
        --start 2026-07-08T10:00:00+00:00 --end 2026-07-08T10:15:00+00:00 \\
        --interval-minutes 5
"""

import argparse
import contextlib
import json
import sys
from datetime import datetime, timedelta, timezone

import psycopg2

import aggregation
import anomaly_rules
import config
import dynamic_baseline
import hybrid_decision
import isolation_forest_detector

SELECT_RAW_ROWS_SQL = """
    SELECT id, timestamp, endpoint, status_code, response_time_ms
    FROM logs_raw
    WHERE timestamp >= %(start)s AND timestamp < %(end)s
    ORDER BY timestamp
"""

SELECT_WINDOW_STATS_SQL = """
    SELECT request_count, error_rate, avg_latency
    FROM logs_agg
    WHERE window_start = %(window_start)s AND endpoint = %(endpoint)s
"""

SELECT_RECENT_WINDOWS_SQL = """
    SELECT avg_latency, error_rate
    FROM logs_agg
    WHERE endpoint = %(endpoint)s AND window_start < %(window_start)s
    ORDER BY window_start DESC
    LIMIT %(limit)s
"""


def _log(message: str) -> None:
    # Diagnostics go to stderr only - stdout is reserved for the final JSON
    # report so the orchestrating evaluation script can parse it directly.
    print(f"[BatchSimulator] {message}", file=sys.stderr)


def _connect():
    return psycopg2.connect(
        host=config.POSTGRES_HOST,
        port=config.POSTGRES_PORT,
        user=config.POSTGRES_USER,
        password=config.POSTGRES_PASSWORD,
        dbname=config.POSTGRES_DB,
    )


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _compute_cutoffs(start: datetime, end: datetime, interval: timedelta) -> list:
    # Aligned to a fixed clock grid (epoch-based N-minute boundaries), matching
    # how a real scheduled batch job runs (e.g. "every 5 minutes on the clock"),
    # not relative to whenever this particular replay's data happens to start.
    # This matters: without clock alignment, an evaluation window shorter than
    # one interval would only ever produce a single cutoff equal to `end`,
    # collapsing "batch" down to "detect at the end of the observation window"
    # instead of genuinely simulating a fixed N-minute execution cadence.
    interval_seconds = interval.total_seconds()
    first_index = int((start - _EPOCH).total_seconds() // interval_seconds) + 1
    cutoffs = []
    index = first_index
    while True:
        boundary = _EPOCH + index * interval
        cutoffs.append(boundary)
        if boundary >= end:
            break
        index += 1
    return cutoffs


def _assign_batch_cutoff(window_end: datetime, cutoffs: list) -> datetime:
    for cutoff in cutoffs:
        if window_end <= cutoff:
            return cutoff
    return cutoffs[-1]


def run_simulation(start: datetime, end: datetime, interval_minutes: int) -> list:
    interval = timedelta(minutes=interval_minutes)
    cutoffs = _compute_cutoffs(start, end, interval)
    _log(f"Replaying [{start.isoformat()}, {end.isoformat()}) in {len(cutoffs)} batch(es) of {interval_minutes}min")

    connection = _connect()
    detected = []

    try:
        with connection.cursor() as cursor:
            cursor.execute(SELECT_RAW_ROWS_SQL, {"start": start, "end": end})
            raw_rows = cursor.fetchall()
            _log(f"Loaded {len(raw_rows)} raw log rows to replay")

            # Group rows by (window_start, endpoint), assign each window to the
            # first batch cutoff that fully contains it.
            windows = {}
            for row_id, timestamp, endpoint, status_code, response_time_ms in raw_rows:
                ts_iso = timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                detected_at, window_start, window_end = aggregation.compute_window(ts_iso)
                key = (window_start, endpoint)
                windows.setdefault(key, {"window_end": window_end, "rows": []})
                windows[key]["rows"].append({
                    "requestId": f"raw-{row_id}",
                    "statusCode": status_code,
                    "responseTimeMs": response_time_ms,
                    "endpoint": endpoint,
                    "detected_at": detected_at,
                })

            batches = {}
            for (window_start, endpoint), payload in windows.items():
                cutoff = _assign_batch_cutoff(payload["window_end"], cutoffs)
                batches.setdefault(cutoff, []).append((window_start, endpoint, payload))

            for cutoff in sorted(batches.keys()):
                window_group = sorted(batches[cutoff], key=lambda w: (w[0], w[1]))
                _log(f"Batch execution at {cutoff.isoformat()}: {len(window_group)} window(s) closing")

                for window_start, endpoint, payload in window_group:
                    window_end = payload["window_end"]
                    rows = sorted(payload["rows"], key=lambda r: r["detected_at"])

                    cursor.execute(SELECT_WINDOW_STATS_SQL, {"window_start": window_start, "endpoint": endpoint})
                    stats_row = cursor.fetchone()
                    if stats_row is None:
                        # logs_agg wasn't populated for this window (shouldn't happen for
                        # data produced by the live pipeline) - skip, nothing to score against.
                        continue
                    request_count, error_rate, avg_latency = stats_row

                    # detected_at for window-level anomalies: use the last (latest) request's
                    # own observed timestamp in the window, not window_end - window_end is a
                    # fixed 1-minute boundary that can be *later* than every actual event in
                    # the window (and, with a short batch interval, later than the batch cutoff
                    # itself, which would produce a negative latency). This mirrors the live
                    # system too: database.py's window-level detected_at is always the specific
                    # triggering request's own timestamp, never the window boundary.
                    last_event_at = rows[-1]["detected_at"]

                    request_level_fired = []
                    for entry in rows:
                        request_anomalies = anomaly_rules.evaluate_request_rules(entry, entry["detected_at"])
                        ml_anomalies = isolation_forest_detector.evaluate(
                            entry, entry["detected_at"], request_count, error_rate
                        )
                        for anomaly in request_anomalies + ml_anomalies:
                            anomaly["batch_execution_time"] = cutoff.isoformat()
                            detected.append(anomaly)
                        request_level_fired.extend(request_anomalies + ml_anomalies)

                    window_anomalies = anomaly_rules.evaluate_window_rules(
                        endpoint, last_event_at, error_rate, request_count
                    )

                    cursor.execute(
                        SELECT_RECENT_WINDOWS_SQL,
                        {"endpoint": endpoint, "window_start": window_start, "limit": dynamic_baseline.ROLLING_WINDOW_COUNT},
                    )
                    historical_windows = [
                        {"avg_latency": row[0], "error_rate": row[1]} for row in cursor.fetchall()
                    ]
                    baseline_anomalies = dynamic_baseline.evaluate_window_baseline(
                        endpoint, last_event_at, avg_latency, error_rate, historical_windows
                    )

                    for anomaly in window_anomalies + baseline_anomalies:
                        anomaly["batch_execution_time"] = cutoff.isoformat()
                        detected.append(anomaly)

                    hybrid_anomalies = hybrid_decision.evaluate(
                        endpoint, last_event_at,
                        request_level_fired + window_anomalies + baseline_anomalies,
                    )
                    for anomaly in hybrid_anomalies:
                        anomaly["batch_execution_time"] = cutoff.isoformat()
                        detected.append(anomaly)
    finally:
        connection.close()

    for anomaly in detected:
        detected_at = anomaly["detected_at"]
        batch_time = datetime.fromisoformat(anomaly["batch_execution_time"])
        anomaly["detected_at"] = detected_at.astimezone(timezone.utc).isoformat()
        anomaly["latency_seconds"] = (batch_time - detected_at).total_seconds()

    _log(f"Simulation produced {len(detected)} anomalies across {len(batches)} batch execution(s)")
    return detected


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay-based batch-processing simulator for RQ3 evaluation only.")
    parser.add_argument("--start", required=True, help="ISO 8601 start of the replay window (inclusive)")
    parser.add_argument("--end", required=True, help="ISO 8601 end of the replay window (exclusive)")
    parser.add_argument("--interval-minutes", type=int, default=5, help="Batch execution interval in minutes (default: 5)")
    args = parser.parse_args()

    start = datetime.fromisoformat(args.start)
    end = datetime.fromisoformat(args.end)

    # isolation_forest_detector.py (imported unchanged, same as the live
    # pipeline uses it) prints its model-load message with a plain print() -
    # redirect stdout to stderr for the whole simulation so nothing but the
    # final JSON below ever reaches stdout, without touching that module.
    with contextlib.redirect_stdout(sys.stderr):
        detected = run_simulation(start, end, args.interval_minutes)

    # Only line on real stdout - everything else above is diagnostics on stderr.
    print(json.dumps(detected, default=str))


if __name__ == "__main__":
    main()
