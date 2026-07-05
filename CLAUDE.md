# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

This project is being built incrementally, one major component at a time, per `Real_Time Log Monitoring and Anomaly Detection System.txt` in the repo root (the authoritative design doc — read it before implementing any component, since it defines exact responsibilities, data flow, and database columns that code must match). See **Milestones** below for what's implemented vs. still pending.

## Architecture

Event-driven pipeline, all services run locally via Docker Compose (no cloud deployment in scope):

```
TrafficSimulator (Python) --HTTP--> DemoApi (.NET) --structured logs--> RabbitMQ --messages--> LogProcessor (Python) --raw logs/aggregations/anomalies--> PostgreSQL --> Grafana
```

End-to-end, today: an HTTP request hits DemoApi → DemoApi logs structured JSON to stdout AND publishes the same `RequestLogEntry` to RabbitMQ's `logs.raw` queue → LogProcessor consumes it, and in **one Postgres transaction**: inserts into `logs_raw`, upserts the 1-minute aggregation window in `logs_agg`, runs rule-based anomaly checks against both the event and the freshly-updated window, inserts any anomalies into `anomalies`, then commits once and acks the RabbitMQ message. Any failure at any stage rolls back the whole transaction and nacks/requeues the message — nothing is ever partially committed.

Key architectural rules (violating these contradicts the design doc):
- **DemoApi** has no database of its own — it only produces structured logs for endpoints `POST /api/login`, `GET /api/products`, `POST /api/cart/add`, `POST /api/payment/checkout`. It supports a `X-Simulate-Latency: true` header to simulate slow/timeout responses.
- **TrafficSimulator** never writes logs directly — it only calls DemoApi over HTTP to generate normal traffic and controlled anomaly scenarios (`normal`/`latency`/`errors`/`mixed` modes). This keeps logs realistic since they're produced by the real API, not synthesized.
- **RabbitMQ** is transport only, never storage.
- **LogProcessor** is a continuously-running Python background worker, not a web API. It owns: consuming from RabbitMQ, persisting raw logs, window-based aggregation, rule-based anomaly detection, Isolation Forest scoring, and (eventually) periodic model retraining (currently a manual script, not scheduled).

## Docker infrastructure

`docker/docker-compose.yml` is the only Compose file (run all `docker compose` commands from `docker/`, or pass `-f docker/docker-compose.yml`). Services:
- **Core (start with plain `docker compose up`)**: `postgres`, `rabbitmq`, `demoapi`, `logprocessor`.
- **`traffic-simulator`** is under `profiles: [tools]` — it never auto-starts; run on demand with `docker compose run traffic-simulator [args...]`.
- DemoApi and LogProcessor each build from their own `Dockerfile`; Postgres, RabbitMQ use stock images (`postgres:16`, `rabbitmq:3.13-management`).
- Service-to-service hostnames inside the Docker network: `rabbitmq`, `postgres`, `demoapi` (Compose service names, not `localhost`).
- `depends_on` uses `condition: service_healthy` for postgres/rabbitmq so dependents wait for real readiness, not just container start.
- **Whenever `database/schema.sql` changes, the `postgres_data` volume must be recreated** (`docker compose down -v` then `up`) — the init script only runs on a fresh volume. This has happened multiple times already; expect it again for future schema changes (e.g. Isolation Forest features, Grafana data source needs).

## PostgreSQL setup

- `database/schema.sql` is mounted into `/docker-entrypoint-initdb.d/`, auto-applied only on first boot of an empty volume.
- Credentials/db name: `logmonitor` / `logmonitor` / `logmonitor` (Compose `POSTGRES_*` env vars; LogProcessor reads the same names via its own env vars, see `src/LogProcessor/config.py`).
- All three tables are now actively written by LogProcessor (see schema below and Milestones).

## RabbitMQ setup

- `rabbitmq:3.13-management` image — broker on `5672`, management UI on `15672` (guest/guest, `http://localhost:15672` locally).
- Single queue: **`logs.raw`**, durable, default exchange (routing key = queue name). DemoApi publishes, LogProcessor consumes with manual ack and `prefetch_count=1`.
- LogProcessor's consumer (`src/LogProcessor/rabbitmq_consumer.py`) reconnects with a fixed delay (`RECONNECT_DELAY_SECONDS`, default 5s) on connection loss, and on a per-message processing failure nacks with `requeue=True` after a short backoff (`PROCESSING_RETRY_DELAY_SECONDS`, default 2s) to avoid a tight retry loop — discovered this was necessary when a Postgres outage caused ~22,000 retries in 20 seconds before the backoff was added.

## DemoApi implementation status

ASP.NET Core (.NET 10) Web API at `src/DemoApi/`. Implemented:
- **Endpoints** (`Controllers/`): `LoginController`, `ProductsController` (fixed 5-item catalog), `CartController`, `PaymentController`. All deterministic — no randomness, fixed validation rules (`quantity <= 0` → 400, unknown `productId` → 404, `amount <= 0` → 400, missing login fields → 400).
- **Structured JSON logging**: `Middleware/RequestLoggingMiddleware.cs` wraps every request, honors `X-Simulate-Latency: true` (fixed 2000ms delay), builds a `Logging/RequestLogEntry.cs` (`timestamp, endpoint, method, statusCode, userId, responseTimeMs, requestId, simulatedLatencyMs`), camelCase JSON to stdout.
- **RabbitMQ publishing**: same `RequestLogEntry` published to `logs.raw` via `Messaging/RabbitMqRequestLogPublisher.cs`. Settings from `RabbitMqOptions` bound to `"RabbitMq"` config section; `docker-compose.yml` sets `RabbitMq__Host: rabbitmq`. Publish failures are caught/logged, never thrown — RabbitMQ being down doesn't break the HTTP response.

## LogProcessor implementation status

Python background worker at `src/LogProcessor/`, module layout:
- **`rabbitmq_consumer.py`** — connects, declares `logs.raw`, consumes with manual ack; reconnect-on-loss and backoff-before-requeue as described above.
- **`database.py`** — `persist_log_entry(entry, window_start, window_end, detected_at)` is the single entry point that does raw insert + agg upsert + anomaly inserts in one transaction, returns the list of anomalies detected (with an `inserted` flag distinguishing a fresh insert from a deduped skip).
- **`aggregation.py`** — `compute_window(timestamp_str)` parses the (variable-precision, `Z`-suffixed) ISO timestamp and floors it to a 1-minute bucket, returning `(parsed_timestamp, window_start, window_end)`.
- **`anomaly_rules.py`** — pure functions, no I/O: `evaluate_request_rules(entry, detected_at)` and `evaluate_window_rules(endpoint, detected_at, error_rate, request_count)`. See Anomaly detection below for the actual rules.
- **`ml_features.py`** — `build_feature_vector(status_code, response_time_ms, endpoint, window_request_count, window_error_rate)` builds the fixed-order feature vector consumed by both training and inference. Uses `response_time_ms`, `status_code`, one-hot encoded `endpoint`, `window_request_count`, `window_error_rate` — **not** the design doc's `request_count, error_rate, avg_latency, p95_latency, unique_ip_count, encoded endpoint` list, since `p95_latency`/`unique_ip_count` aren't computed yet (see Database schema below). This substitution is a deliberate stopgap, not yet reconciled with the design doc.
- **`isolation_forest_detector.py`** — `evaluate(entry, detected_at, window_request_count, window_error_rate)`, called from inside `database.py`'s transaction. Lazily loads `models/isolation_forest.pkl` (path from `config.ISOLATION_FOREST_MODEL_PATH`) once; if the file is missing or fails to load, logs a message and returns no anomalies (rule-based detection is unaffected). On a load model, runs `predict`/`decision_function` on the feature vector and emits an `isolation_forest_anomaly` (severity `medium`, `detection_method="isolation_forest"`) when flagged, routed through the same per-request dedup path (`request_id, anomaly_type`) as the rule-based anomalies.
- **`train_isolation_forest.py`** — standalone manual script (`python train_isolation_forest.py`, not wired into `main.py` or Compose): loads all `logs_raw` joined with `logs_agg` window stats, excludes rows whose `(endpoint, window)` already has a recorded anomaly, builds feature vectors via `ml_features.py`, splits 80/20 train/holdout (fixed seed), fits `sklearn.ensemble.IsolationForest(n_estimators=100, contamination="auto")` on the train split only, evaluates on both splits, saves `{model, feature_names, trained_rows, rows_loaded, rows_excluded_anomalous, holdout_rows, contamination, holdout_anomaly_rate}` to `models/isolation_forest.pkl` via `joblib`. Prints rows loaded/excluded/trained/held-out, contamination, and holdout anomaly rate. Retraining is still manual — no scheduled/periodic job exists.
- **`dynamic_baseline.py`** — pure function, no I/O: `evaluate_window_baseline(endpoint, detected_at, current_avg_latency, current_error_rate, historical_windows)`. `database.py` queries the last 10 prior `logs_agg` windows for the endpoint (strictly before the current `window_start`) and passes them in. Requires >= 5 prior windows before evaluating (returns `[]` otherwise — no flagging during cold start). See Anomaly detection strategy below for the formula.
- **`hybrid_decision.py`** — pure function, no I/O: `evaluate(endpoint, detected_at, fired_anomalies)`. Called from `database.py` with the union of whatever rule-based/dynamic-baseline/Isolation Forest anomalies fired in the current pass; returns 0 or 1 `hybrid_anomaly` dict. See Anomaly detection strategy below for the tier table.
- **`config.py`** — all connection settings (RabbitMQ + Postgres), retry delays, and `ISOLATION_FOREST_MODEL_PATH` (default `models/isolation_forest.pkl`), all overridable via env vars.
- **`main.py`** — wires it together: prints the consumed entry, calls `persist_log_entry`, prints persistence confirmation and any detected anomalies (`[INSERTED]` or `[DUPLICATE (skipped)]`), across all four detection methods.

## TrafficSimulator implementation status

Python on-demand CLI at `src/TrafficSimulator/main.py`, run via `docker compose run --rm traffic-simulator [--mode normal|latency|errors|mixed] [--count N] [--delay SECONDS]` (never auto-starts). Dockerfile uses `ENTRYPOINT ["python", "main.py"]` + default `CMD` args so `docker compose run` can pass flags directly.
- Deterministic, no real randomness — cycles through a fixed `USERS` list and `VALID_PRODUCT_IDS`/`PRODUCT_PRICES` table (mirrors DemoApi's catalog) by index, so the same `--count` produces the same request sequence every run.
- **`normal`**: full journey (login → products → cart/add → checkout) per iteration, all valid.
- **`latency`**: same journey, rotates `X-Simulate-Latency: true` across one of the 4 endpoints per iteration.
- **`errors`**: rotates through 3 invalid-request generators (missing login password, invalid cart add — unknown product or zero quantity, zero-amount checkout) using an **independent call counter** — not `i % 3` directly, since in `mixed` mode that collided with the mode dispatcher's own `i % 3` and always picked the same error type (found and fixed during implementation).
- **`mixed`**: round-robins `normal`/`latency`/`errors` every 3rd iteration.

## Current folder structure

```
src/
├── DemoApi/                 — .NET Web API (endpoints, structured logging, RabbitMQ publishing)
│   ├── Controllers/, Models/, Logging/, Messaging/, Middleware/
│   ├── Program.cs, appsettings.json, Dockerfile
├── LogProcessor/             — Python worker (consumer, persistence, aggregation, rule-based + dynamic baseline + Isolation Forest + hybrid anomaly detection)
│   ├── main.py, rabbitmq_consumer.py, database.py, aggregation.py, anomaly_rules.py, dynamic_baseline.py, hybrid_decision.py, config.py
│   ├── ml_features.py, isolation_forest_detector.py, train_isolation_forest.py
│   ├── requirements.txt, Dockerfile
├── TrafficSimulator/         — Python on-demand traffic generator (normal/latency/errors/mixed modes)
│   ├── main.py, requirements.txt, Dockerfile
database/
└── schema.sql                — logs_raw, logs_agg, anomalies tables + dedup indexes
docker/
└── docker-compose.yml
ml/
└── models/isolation_forest.pkl — trained artifact (manual `train_isolation_forest.py` run, gitignored); training/, evaluation/ still empty
grafana/                       — empty, not started
```

## Database schema (PostgreSQL)

- **`logs_raw`**: `id, timestamp, endpoint, status_code, response_time_ms, user_id, ip, message` — `ip`/`message` are always `NULL` today since DemoApi doesn't produce them. `RequestLogEntry` (DemoApi) has no `Ip` field at all — IP capture hasn't been started, not just unwired, so `unique_ip_count` can't be computed until that's added upstream.
- **`logs_agg`**: `id, window_start, window_end, endpoint, request_count, error_count, error_rate, avg_latency, max_response_time_ms, p95_latency (nullable, not yet computed), unique_ip_count (always 0, no IP source yet)`. `UNIQUE(window_start, endpoint)` backs the upsert. `error_count`/`avg_latency`/`max_response_time_ms` are maintained via running-aggregate formulas on each upsert (not recomputed from raw rows).
- **`anomalies`**: `id, detected_at, window_start, window_end, endpoint, anomaly_type, severity, detection_method, anomaly_score, description, request_id (nullable)`.
  - **Dedup strategy**: two partial unique indexes — `(request_id, anomaly_type) WHERE request_id IS NOT NULL` for per-request anomalies, `(window_start, endpoint, anomaly_type) WHERE request_id IS NULL` for window-level anomalies. Inserts use `ON CONFLICT ... DO NOTHING`. This means a redelivered RabbitMQ message can't double-insert an anomaly, and a window-level anomaly (e.g. `high_error_rate_window`) fires **at most once per window**, even though the rule re-evaluates on every subsequent qualifying request in that window — verified in testing (4 qualifying requests in one window produced exactly 1 row).

## Anomaly detection strategy

**Implemented (rule-based, `anomaly_rules.py`):**
1. `high_latency` — `responseTimeMs >= 1500` → severity `medium`. Per-request.
2. `error_response` — `statusCode >= 500` → severity `high`. Per-request. (DemoApi currently has no path that returns 5xx, so this rule is implemented but not yet exercised by real traffic — only 400/404 are reachable today.)
3. `high_error_rate_window` — the just-updated aggregation window has `error_rate >= 0.5` and `request_count >= 3` → severity `high`. Window-level, evaluated using the `RETURNING` values from the `logs_agg` upsert (no extra query).

**Implemented (Isolation Forest, `isolation_forest_detector.py` + `ml_features.py` + `train_isolation_forest.py`):** scikit-learn `IsolationForest`, trained via a manual script against historical `logs_raw`/`logs_agg` data, persisted with `joblib` at `models/isolation_forest.pkl`, loaded lazily inside `database.py`'s transaction and scored per-request (`isolation_forest_anomaly`, severity `medium`), deduped through the same per-request unique index as the rule-based anomalies. No external ML service — runs in-process inside LogProcessor, per the design doc. **Hardened:** training now excludes rows whose `(endpoint, window)` already has a recorded anomaly (rule-based or ML) before fitting, and does an 80/20 train/holdout split (fixed seed) reporting rows loaded/excluded/trained/held-out, contamination, and holdout anomaly rate — see `train_isolation_forest.py`. **Remaining known limitations:**
- Feature set still substitutes `window_request_count`/`window_error_rate` for the design doc's `p95_latency`/`unique_ip_count` (those columns aren't computed — see Database schema below; documented in a comment in `ml_features.py`).
- Retraining is a manual one-off script run, not scheduled (design doc recommends daily/weekly).

**Implemented (Dynamic Baseline, `dynamic_baseline.py`):** per-endpoint rolling baseline over the most recent `logs_agg` windows (excluding the current one), computed with the stdlib `statistics` module — deterministic, no extra dependency. Flags `dynamic_baseline_latency` (`avg_latency > rolling_mean + 3*rolling_std`, severity `medium`) and `dynamic_baseline_error_rate` (same formula on `error_rate`, severity `high`) once at least 5 prior windows exist for that endpoint. Window-level, stored with `detection_method = 'dynamic_baseline'`, deduped through the existing `(window_start, endpoint, anomaly_type)` partial unique index — the same path `high_error_rate_window` already uses.

**Implemented (Hybrid Decision, `hybrid_decision.py`):** combines whatever rule-based/Isolation Forest/dynamic-baseline signals fired in the *same processing pass* into one `hybrid_anomaly` row (`detection_method = 'hybrid'`). Groups fired anomalies by `detection_method`, takes each family's max severity, and maps to a confidence tier: 1 family firing `medium` → `LOW`; 1 firing `high`, or 2 firing `medium` → `MEDIUM`; 2 firing with one `high` (not `rule_based`) → `HIGH`; `rule_based` firing `high` alongside another family, or all 3 families firing → `CRITICAL`. Stored with `severity` = tier (lowercased), `anomaly_score` = tier rank (1–4), description lists contributing families/severities. Deduped through the same `(window_start, endpoint, anomaly_type)` partial index as the other window-level anomalies, so it fires once per window based on whichever pass first produces a non-empty combination — it does not retroactively update if a stronger combination appears later in the same window (matches the existing once-per-window pattern, not a new inconsistency).

**Designed, not yet implemented:** an evaluation harness comparing detection methods (precision/recall/F1/false-positive-rate/detection latency).

## Milestones

**Completed:**
- Environment setup (Docker Desktop, .NET 10 SDK, Python 3.11)
- Docker Compose infrastructure (core services + `tools` profile)
- PostgreSQL schema + persistence (LogProcessor writes to `logs_raw`)
- RabbitMQ (broker + management UI, `logs.raw` queue, reconnect/backoff handling)
- DemoApi endpoints, structured JSON logging, RabbitMQ publishing
- LogProcessor RabbitMQ consumer (manual ack, retry/backoff on failure)
- Aggregation pipeline (1-minute windows → `logs_agg`, atomic with raw insert)
- Rule-based anomaly detection (3 rules → `anomalies`, with dedup via partial unique indexes)
- TrafficSimulator on-demand traffic generator (`normal`/`latency`/`errors`/`mixed` modes, `tools` profile)
- Isolation Forest integration (trained model + in-process scoring, wired into the same transaction/dedup path as rule-based anomalies), since hardened (anomalous-window exclusion + train/holdout evaluation — see Anomaly detection strategy)
- Dynamic baseline anomaly detection (rolling mean/std per endpoint over `logs_agg` history, latency + error_rate signals)
- Hybrid decision logic (combines rule/dynamic-baseline/Isolation Forest signals into a `hybrid_anomaly` confidence tier — see Anomaly detection strategy)

**Pending:**
- `p95_latency` / `unique_ip_count` computation (blocked on IP capture, which hasn't been started in DemoApi)
- Evaluation harness (precision/recall/F1/false-positive-rate/detection latency across methods)
- Grafana dashboards
