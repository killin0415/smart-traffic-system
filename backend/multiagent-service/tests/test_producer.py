"""Unit tests for the Kafka producer module."""
import json
from unittest.mock import MagicMock, patch

from src.kafka.producer import publish_message, get_producer, _producer


class TestGetProducer:
    """Tests for the get_producer singleton."""

    @patch("src.kafka.producer.Producer")
    def test_get_producer_creates_instance_on_first_call(self, mock_producer_cls):
        """Should create a Producer instance when none exists."""
        import src.kafka.producer as mod
        mod._producer = None

        producer = get_producer()

        mock_producer_cls.assert_called_once_with({"bootstrap.servers": "localhost:9092"})
        assert producer is mock_producer_cls.return_value

        # Cleanup
        mod._producer = None

    @patch("src.kafka.producer.Producer")
    def test_get_producer_returns_same_instance_on_second_call(self, mock_producer_cls):
        """Should reuse existing Producer instance."""
        import src.kafka.producer as mod
        mod._producer = None

        first = get_producer()
        second = get_producer()

        assert first is second
        mock_producer_cls.assert_called_once()

        # Cleanup
        mod._producer = None


class TestPublishMessage:
    """Tests for the publish_message function."""

    @patch("src.kafka.producer.get_producer")
    def test_publish_message_calls_produce_with_correct_args(self, mock_get_producer):
        """Should call produce() with topic, encoded key, encoded value, and callback."""
        mock_producer = MagicMock()
        mock_get_producer.return_value = mock_producer

        payload = {"correlation_id": "c1", "reply": "hello"}
        publish_message(topic="chat.response", key="c1", value=payload)

        mock_producer.produce.assert_called_once()
        call_kwargs = mock_producer.produce.call_args
        assert call_kwargs.kwargs["topic"] == "chat.response"
        assert call_kwargs.kwargs["key"] == b"c1"
        assert json.loads(call_kwargs.kwargs["value"]) == payload
        assert call_kwargs.kwargs["callback"] is not None

    @patch("src.kafka.producer.get_producer")
    def test_publish_message_calls_flush(self, mock_get_producer):
        """Should call flush() after producing."""
        mock_producer = MagicMock()
        mock_get_producer.return_value = mock_producer

        publish_message(topic="test.topic", key="k1", value={"data": 1})

        mock_producer.flush.assert_called_once()

    @patch("src.kafka.producer.get_producer")
    def test_publish_message_encodes_unicode_key_and_value(self, mock_get_producer):
        """Should properly encode unicode characters in key and value."""
        mock_producer = MagicMock()
        mock_get_producer.return_value = mock_producer

        publish_message(topic="t", key="中文key", value={"msg": "你好"})

        call_kwargs = mock_producer.produce.call_args.kwargs
        assert call_kwargs["key"] == "中文key".encode("utf-8")
        decoded_value = json.loads(call_kwargs["value"])
        assert decoded_value["msg"] == "你好"
