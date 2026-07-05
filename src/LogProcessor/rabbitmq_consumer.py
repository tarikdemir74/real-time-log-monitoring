import json
import time

import pika
from pika.exceptions import AMQPConnectionError, StreamLostError

import config


def _connect():
    credentials = pika.PlainCredentials(config.RABBITMQ_USERNAME, config.RABBITMQ_PASSWORD)
    parameters = pika.ConnectionParameters(
        host=config.RABBITMQ_HOST,
        port=config.RABBITMQ_PORT,
        credentials=credentials,
    )
    connection = pika.BlockingConnection(parameters)
    channel = connection.channel()
    channel.queue_declare(queue=config.RABBITMQ_QUEUE, durable=True)
    channel.basic_qos(prefetch_count=1)
    return connection, channel


def run_consumer(on_message) -> None:
    while True:
        try:
            connection, channel = _connect()
        except AMQPConnectionError as exc:
            print(f"[LogProcessor] Could not connect to RabbitMQ ({exc}); retrying in {config.RECONNECT_DELAY_SECONDS}s")
            time.sleep(config.RECONNECT_DELAY_SECONDS)
            continue

        print(
            f"[LogProcessor] Connected to RabbitMQ at {config.RABBITMQ_HOST}:{config.RABBITMQ_PORT}, "
            f"consuming queue '{config.RABBITMQ_QUEUE}'"
        )

        def _callback(ch, method, _properties, body):
            try:
                entry = json.loads(body)
            except json.JSONDecodeError as exc:
                print(f"[LogProcessor] Failed to decode message, discarding: {exc}")
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
                return

            try:
                on_message(entry)
                ch.basic_ack(delivery_tag=method.delivery_tag)
            except Exception as exc:
                print(
                    f"[LogProcessor] Error processing message, requeueing in "
                    f"{config.PROCESSING_RETRY_DELAY_SECONDS}s: {exc}"
                )
                time.sleep(config.PROCESSING_RETRY_DELAY_SECONDS)
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)

        channel.basic_consume(queue=config.RABBITMQ_QUEUE, on_message_callback=_callback)

        try:
            channel.start_consuming()
        except (AMQPConnectionError, StreamLostError) as exc:
            print(f"[LogProcessor] Lost connection to RabbitMQ ({exc}); reconnecting in {config.RECONNECT_DELAY_SECONDS}s")
            time.sleep(config.RECONNECT_DELAY_SECONDS)
            continue
        except (KeyboardInterrupt, SystemExit):
            print("[LogProcessor] Shutting down consumer...")
            channel.stop_consuming()
            connection.close()
            return
