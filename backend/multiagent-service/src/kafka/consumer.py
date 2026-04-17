
"""
Kafka consumer for multiagent-service.
Listens on chat, route, and traffic topics and dispatches to handlers.
"""
import asyncio
import json
import threading
from confluent_kafka import Consumer, KafkaError

from src.agents.routing import plan_optimal_route
from src.kafka import runtime as kafka_runtime
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
    """Handle a route request by running A* on the in-memory RoadGraph.

    Expected payload fields: origin_lat, origin_lng, dest_lat, dest_lng.
    """
    correlation_id = data.get("correlation_id", key)
    try:
        origin_lat = float(data["origin_lat"])
        origin_lng = float(data["origin_lng"])
        dest_lat = float(data["dest_lat"])
        dest_lng = float(data["dest_lng"])
    except (KeyError, TypeError, ValueError) as e:
        publish_message(
            topic="route.response",
            key=correlation_id,
            value={
                "correlation_id": correlation_id,
                "error": f"invalid payload: {e}",
                "routes": [],
            },
        )
        return

    graph = kafka_runtime.get_graph()
    loop = kafka_runtime.get_loop()
    session_factory = kafka_runtime.get_session_factory()

    if graph is None or loop is None or session_factory is None:
        publish_message(
            topic="route.response",
            key=correlation_id,
            value={
                "correlation_id": correlation_id,
                "error": "service not ready: graph/runtime uninitialised",
                "routes": [],
            },
        )
        return

    async def _run() -> dict:
        async with session_factory() as session:
            return await plan_optimal_route(
                session, graph, origin_lat, origin_lng, dest_lat, dest_lng
            )

    future = asyncio.run_coroutine_threadsafe(_run(), loop)
    try:
        result = future.result(timeout=10.0)
    except Exception as e:
        publish_message(
            topic="route.response",
            key=correlation_id,
            value={
                "correlation_id": correlation_id,
                "error": f"routing failed: {e}",
                "routes": [],
            },
        )
        return

    publish_message(
        topic="route.response",
        key=correlation_id,
        value={"correlation_id": correlation_id, **result},
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
