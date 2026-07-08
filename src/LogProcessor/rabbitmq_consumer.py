import json
import time

import pika
from pika.exceptions import AMQPConnectionError, StreamLostError

import config

# Set only by request_shutdown() (called from main.py's SIGTERM handler) - a
# plain module-level flag, never mutated from inside a signal handler in any
# other way, so setting it is always signal-safe (no I/O, no pika calls, no
# re-entrancy risk). It is only ever *acted on* from within _callback, below,
# and only immediately after the current message has been fully acked or
# nacked - never from the signal handler itself and never mid-processing.
_shutdown_requested = False


def request_shutdown() -> None:
    """Requests a graceful stop. Safe to call from a signal handler.

    Does not touch pika/psycopg2 objects directly (calling those from a
    signal handler is not guaranteed re-entrant-safe, since the handler can
    run at an arbitrary point inside an unrelated pika/psycopg2 call). Instead
    it just sets a flag that _callback checks once it has finished handling
    whatever message it was working on - guaranteeing a shutdown request can
    never interrupt an in-flight message, never causes a message to be left
    processed-but-unacked, and never touches RabbitMQ's at-least-once
    semantics: the current message is always fully acked or nacked before
    the consumer stops.
    """
    global _shutdown_requested
    _shutdown_requested = True


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
        if _shutdown_requested:
            print("[LogProcessor] Shutdown already requested; not (re)connecting.")
            return

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
            else:
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

            # Checked only here, after the message above has been fully acked
            # or nacked - see request_shutdown()'s docstring for why.
            if _shutdown_requested:
                print("[LogProcessor] Shutdown requested and current message fully handled; stopping consumer.")
                ch.stop_consuming()

        channel.basic_consume(queue=config.RABBITMQ_QUEUE, on_message_callback=_callback)

        try:
            channel.start_consuming()
        except (AMQPConnectionError, StreamLostError) as exc:
            print(f"[LogProcessor] Lost connection to RabbitMQ ({exc}); reconnecting in {config.RECONNECT_DELAY_SECONDS}s")
            time.sleep(config.RECONNECT_DELAY_SECONDS)
            continue
        except (KeyboardInterrupt, SystemExit):
            # Local interactive Ctrl+C (SIGINT) - Python's default handler
            # raises KeyboardInterrupt asynchronously, the same class of
            # interruption request_shutdown() deliberately avoids for
            # SIGTERM. Kept as a fallback for local dev convenience; not the
            # path Docker's `stop`/`down` take (those send SIGTERM, handled
            # via request_shutdown() and the flag check above instead).
            print("[LogProcessor] Shutting down consumer (interrupt)...")
            channel.stop_consuming()
            connection.close()
            return
        else:
            # start_consuming() only returns without raising after
            # stop_consuming() was called - i.e. exactly the graceful
            # shutdown path from _callback above, never mid-message.
            connection.close()
            if _shutdown_requested:
                print("[LogProcessor] Consumer stopped gracefully (SIGTERM).")
                return
            # Shouldn't normally happen (nothing else calls stop_consuming()),
            # but loop back and reconnect defensively rather than exit silently.
            continue
