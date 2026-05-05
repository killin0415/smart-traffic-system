"""Unit tests for the Kafka consumer handlers."""
from unittest.mock import patch, MagicMock

from src.kafka.consumer import (
    handle_chat_request,
    handle_route_request,
    TOPIC_HANDLERS,
)


class TestTopicHandlerRegistry:
    """Handler registry should cover the default subscribe list."""

    def test_chat_request_registered(self):
        assert "chat.request" in TOPIC_HANDLERS

    def test_route_request_registered(self):
        assert "route.request" in TOPIC_HANDLERS

    def test_traffic_metrics_not_registered(self):
        # traffic.metrics removed — graph weights now driven solely by TDX live polling.
        assert "traffic.metrics" not in TOPIC_HANDLERS


class TestHandleChatRequest:
    """Chat handler should publish a reply to chat.response.

    Without a chat agent in runtime it falls back to a stub message; the
    full agent flow is exercised in `test_chat_agent.py`.
    """

    @patch("src.kafka.consumer.kafka_runtime.get_chat_agent", return_value=None)
    @patch("src.kafka.consumer.publish_message")
    def test_should_publish_reply_to_chat_response_topic(self, mock_publish, _agent):
        data = {
            "correlation_id": "corr-1",
            "session_id": "sess-1",
            "content": "你好",
        }

        handle_chat_request("corr-1", data)

        mock_publish.assert_called_once()
        call_kwargs = mock_publish.call_args.kwargs
        assert call_kwargs["topic"] == "chat.response"
        assert call_kwargs["key"] == "corr-1"

    @patch("src.kafka.consumer.kafka_runtime.get_chat_agent", return_value=None)
    @patch("src.kafka.consumer.publish_message")
    def test_reply_should_contain_required_fields(self, mock_publish, _agent):
        data = {"correlation_id": "c2", "session_id": "s2", "content": "test message"}

        handle_chat_request("c2", data)

        value = mock_publish.call_args.kwargs["value"]
        assert "correlation_id" in value
        assert "reply" in value
        assert "suggested_actions" in value
        assert isinstance(value["suggested_actions"], list)

    @patch("src.kafka.consumer.kafka_runtime.get_chat_agent", return_value=None)
    @patch("src.kafka.consumer.publish_message")
    def test_uses_key_as_fallback_correlation_id(self, mock_publish, _agent):
        data = {"session_id": "s1", "content": "hi"}  # no correlation_id

        handle_chat_request("fallback-key", data)

        value = mock_publish.call_args.kwargs["value"]
        assert value["correlation_id"] == "fallback-key"

    @patch("src.kafka.consumer.kafka_runtime.get_chat_agent", return_value=None)
    @patch("src.kafka.consumer.publish_message")
    def test_route_payload_omitted_when_no_route_intent(self, mock_publish, _agent):
        # Stub agent (None) → no route_payload in the response value.
        handle_chat_request("c3", {"correlation_id": "c3", "content": "hi"})

        value = mock_publish.call_args.kwargs["value"]
        assert "route_payload" not in value


class TestHandleRouteRequest:
    """Route handler runs A* against the in-memory RoadGraph.

    At unit-test time neither the graph nor the event loop are initialised, so
    we verify the error-path contract; integration testing with a live graph
    is covered elsewhere (A* has its own unit tests).
    """

    @patch("src.kafka.consumer.publish_message")
    def test_should_publish_to_route_response_topic(self, mock_publish):
        data = {
            "correlation_id": "r1",
            "origin_lat": 25.04,
            "origin_lng": 121.51,
            "dest_lat": 25.05,
            "dest_lng": 121.52,
        }

        handle_route_request("r1", data)

        mock_publish.assert_called_once()
        assert mock_publish.call_args.kwargs["topic"] == "route.response"

    @patch("src.kafka.consumer.publish_message")
    def test_response_always_includes_correlation_id_and_routes(self, mock_publish):
        data = {
            "correlation_id": "r2",
            "origin_lat": 25.04,
            "origin_lng": 121.51,
            "dest_lat": 25.05,
            "dest_lng": 121.52,
        }

        handle_route_request("r2", data)

        value = mock_publish.call_args.kwargs["value"]
        assert value["correlation_id"] == "r2"
        assert "routes" in value

    @patch("src.kafka.consumer.publish_message")
    def test_invalid_payload_reports_error(self, mock_publish):
        # Missing origin_lat / etc. should not crash the consumer.
        handle_route_request("r3", {"correlation_id": "r3"})

        value = mock_publish.call_args.kwargs["value"]
        assert value["routes"] == []
        assert "error" in value
