import os

RABBITMQ_HOST = os.environ.get("RABBITMQ_HOST", "localhost")
RABBITMQ_PORT = int(os.environ.get("RABBITMQ_PORT", "5672"))
RABBITMQ_USERNAME = os.environ.get("RABBITMQ_USERNAME", "guest")
RABBITMQ_PASSWORD = os.environ.get("RABBITMQ_PASSWORD", "guest")
RABBITMQ_QUEUE = os.environ.get("RABBITMQ_QUEUE", "logs.raw")

RECONNECT_DELAY_SECONDS = int(os.environ.get("RECONNECT_DELAY_SECONDS", "5"))
PROCESSING_RETRY_DELAY_SECONDS = int(os.environ.get("PROCESSING_RETRY_DELAY_SECONDS", "2"))

POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "localhost")
POSTGRES_PORT = int(os.environ.get("POSTGRES_PORT", "5432"))
POSTGRES_USER = os.environ.get("POSTGRES_USER", "logmonitor")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "logmonitor")
POSTGRES_DB = os.environ.get("POSTGRES_DB", "logmonitor")

ISOLATION_FOREST_MODEL_PATH = os.environ.get("ISOLATION_FOREST_MODEL_PATH", "models/isolation_forest.pkl")
