# Real-Time Log Monitoring and Anomaly Detection System

This project aims to build a real-time log monitoring and anomaly detection platform.

## Architecture

TrafficSimulator → DemoApi → RabbitMQ → LogProcessor → PostgreSQL → Grafana

## Components

- DemoApi: .NET Web API that simulates application endpoints and produces structured logs.
- TrafficSimulator: Python service that sends fake user requests to DemoApi.
- RabbitMQ: Message broker used for asynchronous log transport.
- LogProcessor: Python background service that consumes logs, aggregates metrics, detects anomalies, and stores results.
- PostgreSQL: Stores raw logs, aggregated metrics, and anomalies.
- Grafana: Visualizes system metrics and anomalies.

## Technologies

- .NET Web API
- Python
- RabbitMQ
- PostgreSQL
- Grafana
- Docker
- scikit-learn