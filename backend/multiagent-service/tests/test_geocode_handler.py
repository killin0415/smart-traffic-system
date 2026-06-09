"""Tests for the Kafka `geocode.request` handler.

The handler delegates to `agents.geocoding.geocode_location` via `_run_async`,
so we patch `_run_async` to (a) immediately close the coroutine it receives
(avoiding "coroutine was never awaited" warnings) and (b) return / raise the
desired result for each path.
"""

from unittest.mock import MagicMock, patch

from src.kafka.consumer import handle_geocode_request, TOPIC_HANDLERS


def _run_async_returning(value):
    """side_effect factory: closes the coroutine, returns `value`."""

    def _side_effect(coro):
        coro.close()
        return value

    return _side_effect


def _run_async_raising(exc):
    def _side_effect(coro):
        coro.close()
        raise exc

    return _side_effect


class TestRegistry:
    def test_geocode_request_registered(self):
        assert "geocode.request" in TOPIC_HANDLERS


class TestHandleGeocodeRequest:
    @patch("src.kafka.consumer._run_async")
    @patch("src.kafka.consumer.publish_message")
    def test_success_publishes_results(self, mock_publish, mock_run_async):
        mock_run_async.side_effect = _run_async_returning(
            [{"latitude": 25.0478, "longitude": 121.5170, "display_name": "台北車站"}]
        )

        handle_geocode_request(
            "corr-1",
            {
                "correlation_id": "corr-1",
                "query": "台北車站",
                "city_hint": "台北",
                "limit": 3,
            },
        )

        mock_publish.assert_called_once()
        kwargs = mock_publish.call_args.kwargs
        assert kwargs["topic"] == "geocode.response"
        assert kwargs["key"] == "corr-1"
        value = kwargs["value"]
        assert value["correlation_id"] == "corr-1"
        assert value["results"] == [
            {"latitude": 25.0478, "longitude": 121.5170, "display_name": "台北車站"},
        ]
        assert "error" not in value

    @patch("src.kafka.consumer._run_async")
    @patch("src.kafka.consumer.publish_message")
    def test_missing_query_publishes_error(self, mock_publish, mock_run_async):
        handle_geocode_request("corr-2", {"correlation_id": "corr-2"})

        mock_run_async.assert_not_called()
        value = mock_publish.call_args.kwargs["value"]
        assert value["correlation_id"] == "corr-2"
        assert value["results"] == []
        assert value["error"] == "query is required"

    @patch("src.kafka.consumer._run_async")
    @patch("src.kafka.consumer.publish_message")
    def test_blank_query_publishes_error(self, mock_publish, mock_run_async):
        handle_geocode_request("corr-3", {"correlation_id": "corr-3", "query": "   "})

        mock_run_async.assert_not_called()
        value = mock_publish.call_args.kwargs["value"]
        assert value["results"] == []
        assert value["error"] == "query is required"

    @patch("src.kafka.consumer._run_async")
    @patch("src.kafka.consumer.publish_message")
    def test_upstream_failure_publishes_error_without_crash(self, mock_publish, mock_run_async):
        mock_run_async.side_effect = _run_async_raising(RuntimeError("nominatim unreachable"))

        handle_geocode_request(
            "corr-4",
            {"correlation_id": "corr-4", "query": "台北車站"},
        )

        value = mock_publish.call_args.kwargs["value"]
        assert value["correlation_id"] == "corr-4"
        assert value["results"] == []
        assert "error" in value
        assert "nominatim unreachable" in value["error"]

    @patch("src.kafka.consumer._run_async")
    @patch("src.kafka.consumer.publish_message")
    def test_falls_back_to_key_when_correlation_id_missing(self, mock_publish, mock_run_async):
        mock_run_async.side_effect = _run_async_returning([])
        handle_geocode_request("fallback-key", {"query": "台北車站"})

        value = mock_publish.call_args.kwargs["value"]
        assert value["correlation_id"] == "fallback-key"

    @patch("src.kafka.consumer.geocode_location")
    @patch("src.kafka.consumer._run_async")
    @patch("src.kafka.consumer.publish_message")
    def test_default_limit_when_field_missing(
        self, mock_publish, mock_run_async, mock_geocode_location,
    ):
        """Missing 'limit' → handler calls geocode_location with default limit=5."""
        # geocode_location returns a real coroutine since the handler does
        # `_run_async(geocode_location(...))` — patching it lets us inspect
        # the call args directly, then close the dummy coroutine cleanly.
        mock_geocode_location.return_value = MagicMock(name="dummy_coroutine")
        mock_run_async.side_effect = lambda _coro: []

        handle_geocode_request("corr-5", {"correlation_id": "corr-5", "query": "台北車站"})

        mock_geocode_location.assert_called_once_with("台北車站", city_hint=None, limit=5)
        value = mock_publish.call_args.kwargs["value"]
        assert value["correlation_id"] == "corr-5"
        assert value["results"] == []

    @patch("src.kafka.consumer.geocode_location")
    @patch("src.kafka.consumer._run_async")
    @patch("src.kafka.consumer.publish_message")
    def test_explicit_limit_and_city_hint_forwarded(
        self, mock_publish, mock_run_async, mock_geocode_location,
    ):
        mock_geocode_location.return_value = MagicMock(name="dummy_coroutine")
        mock_run_async.side_effect = lambda _coro: []

        handle_geocode_request(
            "corr-6",
            {
                "correlation_id": "corr-6",
                "query": "中正紀念堂",
                "city_hint": "台北",
                "limit": 8,
            },
        )

        mock_geocode_location.assert_called_once_with("中正紀念堂", city_hint="台北", limit=8)

    @patch("src.kafka.consumer.geocode_location")
    @patch("src.kafka.consumer._run_async")
    @patch("src.kafka.consumer.publish_message")
    def test_blank_city_hint_passes_none(
        self, mock_publish, mock_run_async, mock_geocode_location,
    ):
        mock_geocode_location.return_value = MagicMock(name="dummy_coroutine")
        mock_run_async.side_effect = lambda _coro: []

        handle_geocode_request(
            "corr-7",
            {"correlation_id": "corr-7", "query": "台北車站", "city_hint": "   "},
        )

        mock_geocode_location.assert_called_once_with("台北車站", city_hint=None, limit=5)
