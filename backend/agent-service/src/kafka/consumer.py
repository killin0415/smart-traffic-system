"""
Kafka consumer for agent-service.
Listens on traffic-related topics and processes incoming data.
"""
import asyncio
import json
from confluent_kafka import Consumer, KafkaError


KAFKA_CONFIG = {
    "bootstrap.servers": "localhost:9092",
    "group.id": "agent-service-group",
    "auto.offset.reset": "latest",
}

TOPICS = ["traffic-data", "yolo-results"]


async def start_kafka_consumer():
    """Start an async Kafka consumer that listens for traffic data."""
    consumer = Consumer(KAFKA_CONFIG)
    consumer.subscribe(TOPICS)
    print(f"[Kafka Consumer] Subscribed to topics: {TOPICS}")

    loop = asyncio.get_event_loop()

    try:
        while True:
            # Run the blocking poll in a thread to avoid blocking the event loop
            msg = await loop.run_in_executor(None, lambda: consumer.poll(1.0))

            if msg is None:
                continue
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                print(f"[Kafka Consumer] Error: {msg.error()}")
                continue

            try:
                value = json.loads(msg.value().decode("utf-8"))
                print(f"[Kafka Consumer] Topic={msg.topic()}, Data={value}")
                # TODO: Phase 2 - Write to TimescaleDB / Redis
            except json.JSONDecodeError:
                print(f"[Kafka Consumer] Received non-JSON message: {msg.value()}")

    except asyncio.CancelledError:
        print("[Kafka Consumer] Shutting down...")
    finally:
        consumer.close()
