"""
Kafka producer utility for agent-service.
Used to publish processed results (e.g., route recommendations, alerts) back to Kafka.
"""
import json
from confluent_kafka import Producer


KAFKA_CONFIG = {
    "bootstrap.servers": "localhost:9092",
}

_producer: Producer | None = None


def get_producer() -> Producer:
    """Get or create the Kafka producer singleton."""
    global _producer
    if _producer is None:
        _producer = Producer(KAFKA_CONFIG)
        print("[Kafka Producer] Initialized")
    return _producer


def publish_message(topic: str, key: str, value: dict):
    """
    Publish a message to a Kafka topic.

    Args:
        topic: Kafka topic name
        key: Message key (e.g., session_id or node_id)
        value: Message payload as a dictionary
    """
    producer = get_producer()

    def delivery_report(err, msg):
        if err is not None:
            print(f"[Kafka Producer] Delivery failed: {err}")
        else:
            print(f"[Kafka Producer] Delivered to {msg.topic()}[{msg.partition()}]")

    producer.produce(
        topic=topic,
        key=key.encode("utf-8"),
        value=json.dumps(value).encode("utf-8"),
        callback=delivery_report,
    )
    producer.flush()
