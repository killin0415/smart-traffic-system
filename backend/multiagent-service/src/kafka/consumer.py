"""
Kafka consumer for multiagent-service.

Subscription list is configurable via the `KAFKA_SUBSCRIBE_TOPICS` env var
(comma-separated). Handlers are registered in the `TOPIC_HANDLERS` dict —
new microservices can plug in by adding an entry without touching the
dispatcher loop. Unknown topics, malformed JSON, and handler exceptions are
logged but never crash the consumer thread.
"""
import asyncio
import json
import logging
import os
import threading
import traceback

from confluent_kafka import Consumer, KafkaError

from src.agents.routing import plan_optimal_route
from src.kafka import runtime as kafka_runtime
from src.kafka.producer import publish_message

logger = logging.getLogger(__name__)


KAFKA_CONFIG = {
    "bootstrap.servers": os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"),
    "group.id": os.getenv("KAFKA_CONSUMER_GROUP", "multiagent-service-group"),
    "auto.offset.reset": "latest",
}

DEFAULT_SUBSCRIBE_TOPICS = "chat.request,route.request"

_stop_event = threading.Event()


def _resolve_topics() -> list[str]:
    """Parse KAFKA_SUBSCRIBE_TOPICS env var into a clean list of topic names."""
    raw = os.getenv("KAFKA_SUBSCRIBE_TOPICS", "")
    if not raw or not raw.strip():
        raw = DEFAULT_SUBSCRIBE_TOPICS
    topics = [t.strip() for t in raw.split(",") if t.strip()]
    if not topics:
        topics = [t.strip() for t in DEFAULT_SUBSCRIBE_TOPICS.split(",") if t.strip()]
    return topics


def _run_async(coro):
    """Run an async coroutine on the FastAPI event loop and wait for the result.

    Used by Kafka handlers (which run in a dedicated thread) to invoke async code
    safely. Raises RuntimeError if the runtime loop has not been wired up.
    """
    loop = kafka_runtime.get_loop()
    if loop is None:
        raise RuntimeError("event loop not initialised on kafka_runtime")
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=30.0)


def handle_chat_request(key: str, data: dict):
    """Forward a chat request to the Gemini ChatAgent and publish the reply."""
    correlation_id = data.get("correlation_id", key)
    session_id = data.get("session_id", "")
    content = data.get("content", "")

    logger.info("[Chat Handler] session=%s, content=%s", session_id, content)

    chat_agent = kafka_runtime.get_chat_agent()
    if chat_agent is None:
        reply = "[Multiagent Service] 收到您的訊息，但 chat agent 尚未啟動。"
        route_payload = None
    else:
        try:
            result = _run_async(chat_agent.agenerate(content))
            reply = result.get("reply", "")
            route_payload = result.get("route_payload")
        except Exception as exc:
            logger.exception("Chat agent invocation failed: %s", exc)
            reply = "目前服務忙線，請稍後再試。"
            route_payload = None

    value: dict = {
        "correlation_id": correlation_id,
        "reply": reply,
        "suggested_actions": ["查看即時路況", "規劃路線", "查詢停車位"],
    }
    if route_payload is not None:
        value["route_payload"] = route_payload

    publish_message(topic="chat.response", key=correlation_id, value=value)


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


# Registry of topic → handler. New microservices plug in by adding an entry here
# (and including the topic in KAFKA_SUBSCRIBE_TOPICS).
TOPIC_HANDLERS = {
    "chat.request": handle_chat_request,
    "route.request": handle_route_request,
}


def _consumer_loop():
    """Blocking consumer loop that runs in a dedicated thread."""
    consumer = Consumer(KAFKA_CONFIG)
    topics = _resolve_topics()
    consumer.subscribe(topics)
    logger.info("[Kafka Consumer] Subscribed to topics: %s", topics)

    try:
        while not _stop_event.is_set():
            msg = consumer.poll(1.0)

            if msg is None:
                continue
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                logger.error("[Kafka Consumer] Kafka error: %s", msg.error())
                continue

            topic = msg.topic()
            raw_key = msg.key()
            key = raw_key.decode("utf-8") if raw_key else ""

            try:
                value = json.loads(msg.value().decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                preview = (msg.value() or b"")[:120]
                logger.error(
                    "[Kafka Consumer] failed to decode message on topic=%s key=%s: %s; raw=%r",
                    topic, key, exc, preview,
                )
                continue

            handler = TOPIC_HANDLERS.get(topic)
            if handler is None:
                logger.warning(
                    "[Kafka Consumer] no handler registered for topic=%s key=%s — skipping",
                    topic, key,
                )
                continue

            try:
                handler(key, value)
            except Exception as exc:  # never let a handler kill the loop
                logger.error(
                    "[Kafka Consumer] handler for topic=%s key=%s raised %s\n%s",
                    topic, key, exc, traceback.format_exc(),
                )
                continue
    finally:
        consumer.close()
        logger.info("[Kafka Consumer] Closed.")


async def start_kafka_consumer():
    """Start Kafka consumer in a dedicated thread (confluent_kafka is not async-safe)."""
    _stop_event.clear()
    thread = threading.Thread(target=_consumer_loop, daemon=True)
    thread.start()
    logger.info("[Kafka Consumer] Thread started.")

    try:
        while thread.is_alive():
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        logger.info("[Kafka Consumer] Shutting down...")
        _stop_event.set()
        thread.join(timeout=5)
