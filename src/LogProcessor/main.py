import signal

import aggregation
import database
import rabbitmq_consumer


def handle_log_entry(entry: dict) -> None:
    print("=" * 60)
    print("[LogProcessor] Consumed RequestLogEntry")
    print(f"  requestId          : {entry.get('requestId')}")
    print(f"  timestamp          : {entry.get('timestamp')}")
    print(f"  endpoint           : {entry.get('endpoint')}")
    print(f"  method             : {entry.get('method')}")
    print(f"  statusCode         : {entry.get('statusCode')}")
    print(f"  userId             : {entry.get('userId')}")
    print(f"  responseTimeMs     : {entry.get('responseTimeMs')}")
    print(f"  simulatedLatencyMs : {entry.get('simulatedLatencyMs')}")
    print("=" * 60)

    detected_at, window_start, window_end = aggregation.compute_window(entry.get("timestamp"))
    anomalies = database.persist_log_entry(entry, window_start, window_end, detected_at)
    print(
        f"[LogProcessor] Persisted to logs_raw and updated logs_agg "
        f"(requestId={entry.get('requestId')}, window={window_start.isoformat()})"
    )

    for anomaly in anomalies:
        status = "INSERTED" if anomaly.get("inserted") else "DUPLICATE (skipped)"
        print(
            f"[LogProcessor] ANOMALY DETECTED [{status}] type={anomaly['anomaly_type']} "
            f"severity={anomaly['severity']} endpoint={anomaly['endpoint']} "
            f"score={anomaly['anomaly_score']} requestId={anomaly.get('request_id')} "
            f"-- {anomaly['description']}"
        )


def _handle_sigterm(_signum, _frame):
    # Only sets a flag (signal-safe) - see rabbitmq_consumer.request_shutdown()
    # for why this deliberately doesn't raise an exception here: doing so
    # could interrupt a message mid-processing, inside psycopg2/pika calls
    # that aren't guaranteed safe to abort asynchronously.
    print("[LogProcessor] SIGTERM received; will stop after the in-flight message (if any) finishes.")
    rabbitmq_consumer.request_shutdown()


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_sigterm)
    print("LogProcessor starting...")
    rabbitmq_consumer.run_consumer(handle_log_entry)


if __name__ == "__main__":
    main()
