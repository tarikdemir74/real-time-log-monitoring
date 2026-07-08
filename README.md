# Real-Time Log Monitoring and Anomaly Detection System

A real-time log monitoring and anomaly detection platform built as an event-driven pipeline: a demo API produces structured logs under simulated traffic, and a background worker consumes them, aggregates metrics in 1-minute windows, and detects anomalies using four complementary methods (rule-based, dynamic baseline, Isolation Forest, and a hybrid decision layer), all visualized in Grafana. Everything runs locally via Docker Compose — no cloud dependencies.

This README is the practical, human-facing entry point; `CLAUDE.md` in the repo root has the full implementation-level detail (exact thresholds, formulas, schema, and design decisions behind everything summarized here).

## Architecture

```
TrafficSimulator (Python)  --HTTP-->  DemoApi (.NET)  --structured logs-->  RabbitMQ
                                                                                |
                                                                          logs.raw queue
                                                                                |
                                                                                v
                                                                        LogProcessor (Python)
                                                                     (consume, aggregate, detect)
                                                                                |
                                                    +---------------------------+---------------------------+
                                                    |                                                       |
                                                    v                                                       v
                                              PostgreSQL                                               (in-process)
                                        logs_raw / logs_agg / anomalies                          Isolation Forest model
                                                    |
                                                    v
                                                Grafana
```

An HTTP request hits DemoApi, which logs structured JSON to stdout **and** publishes the same event to RabbitMQ. LogProcessor consumes it and, in **one Postgres transaction**: inserts the raw log, upserts the current 1-minute aggregation window, runs all four detection layers against the event and the freshly-updated window, inserts any anomalies, then commits once and acknowledges the message. Any failure rolls back the whole transaction and requeues the message — nothing is ever partially committed, and no message is ever acknowledged before its processing is fully done.

## Technology stack

| Component | Technology |
|---|---|
| DemoApi | ASP.NET Core (.NET 10) |
| TrafficSimulator | Python 3 (stdlib + `requests`) |
| LogProcessor | Python 3 (stdlib + `pika`, `psycopg2`, `scikit-learn`, `joblib`) |
| Message broker | RabbitMQ 3.13 (management UI included) |
| Database | PostgreSQL 16 |
| Dashboards | Grafana OSS 11.3 |
| Orchestration | Docker Compose |

## Detection methods

Four independent methods run on every event/window, each producing rows in the `anomalies` table:

1. **Rule-based** (`anomaly_rules.py`) — fixed thresholds: `high_latency` (response time ≥ 1500ms), `error_response` (status ≥ 500), `high_error_rate_window` (window error rate ≥ 0.5 with ≥ 3 requests). Highest precision, but blind to anything it wasn't explicitly told to check (e.g. it can't see 4xx errors at all).
2. **Isolation Forest** (`isolation_forest_detector.py` + `ml_features.py` + `train_isolation_forest.py`) — an unsupervised `scikit-learn` model trained offline on historical traffic (excluding any window that already has a recorded anomaly), scoring each request in-process. Highest recall, but currently the noisiest method — see [Known limitations](#known-limitations).
3. **Dynamic baseline** (`dynamic_baseline.py`) — per-endpoint rolling mean + 3×stddev over the last 10 windows (needs ≥ 5 windows of history before it activates). Adapts over time; its recall depends on traffic history/order.
4. **Hybrid decision** (`hybrid_decision.py`) — combines whichever of the above fired in the same pass into a single `LOW`/`MEDIUM`/`HIGH`/`CRITICAL` confidence tier. Persisted anomalies can be **upgraded** in place if a stronger combination fires later in the same window, but never downgraded. Consistently the best-balanced method because it only needs partial agreement, not a well-calibrated model.

See `CLAUDE.md` → "Anomaly detection strategy" for exact thresholds, formulas, and the evidence behind these characterizations.

## Folder structure

```
src/
├── DemoApi/            .NET Web API — endpoints, structured logging, RabbitMQ publishing, /health
├── LogProcessor/        Python worker — consumer, persistence, aggregation, all 4 detection layers, unit tests
└── TrafficSimulator/    Python on-demand traffic generator (normal/latency/errors/mixed modes)
database/
└── schema.sql            logs_raw, logs_agg, anomalies tables + dedup indexes
docker/
├── docker-compose.yml
└── .env.example           copy to .env to override credential defaults
ml/
├── models/                trained Isolation Forest artifact (gitignored, generated by training)
├── training/               reserved, currently unused
└── evaluation/
    ├── run_evaluation.py   precision/recall/F1 comparison harness across all 4 methods
    └── results/            timestamped reports (gitignored, generated by the harness)
grafana/provisioning/       datasource + dashboard, auto-provisioned on startup
```

## Quick start

Prerequisites: Docker Desktop (or compatible Docker Engine + Compose v2).

```bash
cd docker
docker compose up --build
```

This starts `postgres`, `rabbitmq`, `demoapi`, `logprocessor`, and `grafana`. First boot also applies `database/schema.sql` to a fresh Postgres volume. `traffic-simulator` does **not** start automatically (see below).

To override default credentials, copy `docker/.env.example` to `docker/.env` and edit it — the defaults (`logmonitor`/`logmonitor`, `guest`/`guest`, `admin`/`admin`) are used automatically if `.env` doesn't exist.

**If you change `database/schema.sql`**, the running Postgres volume must be recreated for it to take effect (the init script only runs once, against an empty volume):

```bash
docker compose down -v
docker compose up --build
```

## Running TrafficSimulator

TrafficSimulator only runs on demand (it's under the `tools` Compose profile, so plain `docker compose up` never starts it):

```bash
docker compose run --rm traffic-simulator --mode normal --count 50 --delay 0.2
```

- `--mode`: `normal` (full valid user journeys), `latency` (injects `X-Simulate-Latency` on one endpoint per iteration), `errors` (invalid-request generators — bad login, invalid cart add, zero-amount checkout), `mixed` (round-robins the three every 3rd iteration).
- `--count`: number of iterations.
- `--delay`: seconds between individual HTTP calls (smaller = denser aggregation windows).

TrafficSimulator waits for DemoApi's `/health` endpoint to respond before sending traffic (both at the Compose level via `depends_on: condition: service_healthy`, and with its own bounded poll for standalone runs).

## Training the Isolation Forest

Requires the stack to be up (reads from `logs_raw`/`logs_agg`) and at least some accumulated traffic:

```bash
docker compose exec logprocessor python train_isolation_forest.py
```

This excludes any `(endpoint, window)` that already has a recorded anomaly, splits the remaining rows 80/20 (fixed seed), fits `IsolationForest(contamination="auto")`, and saves the model plus its `feature_names` and training stats to `ml/models/isolation_forest.pkl`. **Restart `logprocessor`** afterward to load the new model — it's loaded once per process, not hot-reloaded (see [Known limitations](#known-limitations)):

```bash
docker compose restart logprocessor
```

LogProcessor validates the loaded model's `feature_names` against the current feature set on load; on any mismatch it disables ML-based detection and logs a clear error, without affecting the other three detection methods.

## Running the evaluation harness

Standalone Python script, stdlib + Docker Compose CLI only (no pip installs needed), run from the repo root with the stack already up:

```bash
python3 ml/evaluation/run_evaluation.py
```

It drives four controlled TrafficSimulator scenarios and reports precision/recall/F1 per detection method into `ml/evaluation/results/evaluation_<timestamp>.{md,csv}`. Ground truth is inferred (not measured) from TrafficSimulator's own deterministic dispatch logic — see the generated report for the exact methodology and caveats.

## Opening Grafana

```
http://localhost:3000
```

Default login `admin`/`admin` (or your `.env` override). Everything is auto-provisioned — no manual datasource or dashboard setup. The "Real-Time Log Monitoring" dashboard has 8 panels: request volume, average response time and error rate by endpoint, anomaly count over time, anomalies by detection method, hybrid severity distribution, a recent-anomalies table, and P95 latency by endpoint (computed live from `logs_raw` via `percentile_cont(0.95)`, not pre-aggregated).

## Running unit tests

The four pure-logic detection modules (`anomaly_rules.py`, `dynamic_baseline.py`, `hybrid_decision.py`, `ml_features.py`) have deterministic unit tests with zero external dependencies — no Docker or database needed:

```bash
cd src/LogProcessor
python3 -m unittest discover -s . -p "test_*.py"
```

## Known limitations

- **Isolation Forest generalization**: two full retraining cycles (see `CLAUDE.md` → "Isolation Forest retraining notes") showed that adding significantly more training data did not improve precision — both retrains produced statistically identical results on the evaluation harness. The root cause, confirmed from the data rather than assumed: training windows are dominated by large `request_count` values (60-160 requests/window) while evaluation/production-scale traffic produces much smaller windows (well under 60). **Further "just bigger" retraining is not recommended**; a real fix would need a deliberately different data-generation strategy targeting small-window representation.
- **`logs_agg.p95_latency` / `unique_ip_count`** are structurally present but never computed — no IP capture exists upstream in DemoApi, and P95 is deliberately computed live in Grafana instead of during aggregation (see the Grafana panel above).
- **`ml_features.py`'s feature set** substitutes `window_request_count`/`window_error_rate` and per-request `response_time_ms`/`status_code` for the design doc's `request_count, error_rate, avg_latency, p95_latency, unique_ip_count`, since the latter two aren't computed yet.
- **No TLS, no real authentication** anywhere in the stack — appropriate for this project's local-only scope.
- **No connection pooling** — LogProcessor holds a single psycopg2 connection, reconnecting reactively on failure.
- **No schema migrations** — `schema.sql` is a single init script; every schema change so far has required recreating the Postgres volume.
- **Isolation Forest model is not hot-reloaded** — retraining requires a `logprocessor` restart to take effect.

## Future work

Deliberately deferred, not accidentally missing — none of these are required for the project's core contribution (implementing and evaluating four anomaly detection methods on a real event-driven pipeline):

- Scheduled/periodic Isolation Forest retraining
- Connection pooling
- Schema migrations
- Isolation Forest hot reload
- TLS across the stack
- Real authentication in DemoApi
- IP capture in DemoApi and `unique_ip_count` computation
- Native `p95_latency` computed during aggregation (vs. the current live Grafana panel)
- Multi-window aggregation (e.g. 5-minute windows alongside the current 1-minute)
- Advanced alerting (email/Discord/Telegram/Grafana Alerts) beyond storing rows in `anomalies`

See `CLAUDE.md` → "Future Work" for the same list with rationale for each deferral.
