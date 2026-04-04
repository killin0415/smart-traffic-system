
"""
Kafka consumer for multiagent-service.
Listens on chat, route, and traffic topics and dispatches to handlers.
"""
import asyncio
import json
import threading
from confluent_kafka import Consumer, KafkaError

from src.kafka.producer import publish_message


KAFKA_CONFIG = {
    "bootstrap.servers": "localhost:9092",
    "group.id": "multiagent-service-group",
    "auto.offset.reset": "latest",
}

TOPICS = ["chat.request", "route.request", "traffic.metrics"]

_stop_event = threading.Event()


def handle_chat_request(key: str, data: dict):
    """Handle a chat request: generate a stub reply and produce to chat.response."""
    correlation_id = data.get("correlation_id", key)
    session_id = data.get("session_id", "")
    content = data.get("content", "")

    print(f"[Chat Handler] session={session_id}, content={content}")

    # TODO: Route to Chat Manager → MCP Tools → LLM
    reply = f"[Multiagent Service] 收到您的訊息: '{content}'. AI 推論功能開發中..."

    publish_message(
        topic="chat.response",
        key=correlation_id,
        value={
            "correlation_id": correlation_id,
            "reply": reply,
            "suggested_actions": ["查看即時路況", "規劃路線", "查詢停車位"],
        },
    )


def handle_route_request(key: str, data: dict):
    """Handle a route request: generate a stub response and produce to route.response."""
    correlation_id = data.get("correlation_id", key)
    origin = data.get("origin", "")
    destination = data.get("destination", "")

    print(f"[Route Handler] origin={origin}, destination={destination}")

    # TODO: Route Agent with A* algorithm
    publish_message(
        topic="route.response",
        key=correlation_id,
        value={
            "correlation_id": correlation_id,
            "route_id": "stub-route-id",
            "path": f"{origin} -> {destination}",
            "estimated_time": 15,
        },
    )


def handle_traffic_metrics(key: str, data: dict):
    """Handle incoming YOLO traffic metrics."""
    print(f"[Traffic Handler] metrics={data}")
    # TODO: Write to TimescaleDB / update graph weights


TOPIC_HANDLERS = {
    "chat.request": handle_chat_request,
    "route.request": handle_route_request,
    "traffic.metrics": handle_traffic_metrics,
}


def _consumer_loop():
    """Blocking consumer loop that runs in a dedicated thread."""
    consumer = Consumer(KAFKA_CONFIG)
    consumer.subscribe(TOPICS)
    print(f"[Kafka Consumer] Subscribed to topics: {TOPICS}", flush=True)

    try:
        while not _stop_event.is_set():
            msg = consumer.poll(1.0)

            if msg is None:
                continue
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                print(f"[Kafka Consumer] Error: {msg.error()}", flush=True)
                continue

            try:
                value = json.loads(msg.value().decode("utf-8"))
                key = msg.key().decode("utf-8") if msg.key() else ""
                topic = msg.topic()

                print(f"[Kafka Consumer] Received on {topic}: key={key}", flush=True)

                handler = TOPIC_HANDLERS.get(topic)
                if handler:
                    handler(key, value)
                else:
                    print(f"[Kafka Consumer] No handler for topic: {topic}", flush=True)

            except json.JSONDecodeError:
                print(f"[Kafka Consumer] Received non-JSON message: {msg.value()}", flush=True)
    finally:
        consumer.close()
        print("[Kafka Consumer] Closed.", flush=True)


async def start_kafka_consumer():
    """Start Kafka consumer in a dedicated thread (confluent_kafka is not async-safe)."""
    _stop_event.clear()
    thread = threading.Thread(target=_consumer_loop, daemon=True)
    thread.start()
    print("[Kafka Consumer] Thread started.", flush=True)

    try:
        while thread.is_alive():
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        print("[Kafka Consumer] Shutting down...", flush=True)
        _stop_event.set()
        thread.join(timeout=5)
