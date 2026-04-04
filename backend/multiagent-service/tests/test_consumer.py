"""Unit tests for the Kafka consumer handlers."""
from unittest.mock import patch, MagicMock

from src.kafka.consumer import (
    handle_chat_request,
    handle_route_request,
    handle_traffic_metrics,
    TOPIC_HANDLERS,
    TOPICS,
)


class TestTopicConfiguration:
    """Tests for topic and handler configuration."""

    def test_topics_list_contains_expected_topics(self):
        assert "chat.request" in TOPICS
        assert "route.request" in TOPICS
        assert "traffic.metrics" in TOPICS

    def test_topic_handlers_map_all_topics(self):
        for topic in TOPICS:
            assert topic in TOPIC_HANDLERS, f"Missing handler for topic: {topic}"


class TestHandleChatRequest:
    """Tests for the chat request handler."""

    @patch("src.kafka.consumer.publish_message")
    def test_should_publish_reply_to_chat_response_topic(self, mock_publish):
        data = {
            "correlation_id": "corr-1",
            "session_id": "sess-1",
            "content": "你好",
        }

        handle_chat_request("corr-1", data)

        mock_publish.assert_called_once()
        call_kwargs = mock_publish.call_args
        assert call_kwargs.kwargs["topic"] == "chat.response"
        assert call_kwargs.kwargs["key"] == "corr-1"

    @patch("src.kafka.consumer.publish_message")
    def test_reply_should_contain_required_fields(self, mock_publish):
        data = {
            "correlation_id": "c2",
            "session_id": "s2",
            "content": "test message",
        }

        handle_chat_request("c2", data)

        value = mock_publish.call_args.kwargs["value"]
        assert "correlation_id" in value
        assert "reply" in value
        assert "suggested_actions" in value
        assert isinstance(value["suggested_actions"], list)

    @patch("src.kafka.consumer.publish_message")
    def test_uses_key_as_fallback_correlation_id(self, mock_publish):
        data = {"session_id": "s1", "content": "hi"}  # no correlation_id

        handle_chat_request("fallback-key", data)

        value = mock_publish.call_args.kwargs["value"]
        assert value["correlation_id"] == "fallback-key"


class TestHandleRouteRequest:
    """Tests for the route request handler."""

    @patch("src.kafka.consumer.publish_message")
    def test_should_publish_to_route_response_topic(self, mock_publish):
        data = {
            "correlation_id": "r1",
            "origin": "高雄車站",
            "destination": "駁二藝術特區",
        }

        handle_route_request("r1", data)

        mock_publish.assert_called_once()
        assert mock_publish.call_args.kwargs["topic"] == "route.response"

    @patch("src.kafka.consumer.publish_message")
    def test_response_should_contain_route_fields(self, mock_publish):
        data = {
            "correlation_id": "r2",
            "origin": "A",
            "destination": "B",
        }

        handle_route_request("r2", data)

        value = mock_publish.call_args.kwargs["value"]
        assert "correlation_id" in value
        assert "route_id" in value
        assert "path" in value
        assert "estimated_time" in value

    @patch("src.kafka.consumer.publish_message")
    def test_path_should_contain_origin_and_destination(self, mock_publish):
        data = {
            "correlation_id": "r3",
            "origin": "X",
            "destination": "Y",
        }

        handle_route_request("r3", data)

        value = mock_publish.call_args.kwargs["value"]
        assert "X" in value["path"]
        assert "Y" in value["path"]


class TestHandleTrafficMetrics:
    """Tests for the traffic metrics handler."""

    def test_should_handle_metrics_without_error(self):
        data = {"vehicle_count": 42, "speed_avg": 30.5}
        # Should not raise
        handle_traffic_metrics("key-1", data)

    def test_should_handle_empty_data(self):
        handle_traffic_metrics("key-2", {})
