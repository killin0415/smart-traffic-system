"""Consumer extensibility: env-var topic subscription + robust dispatcher loop."""

from __future__ import annotations

import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

import src.kafka.consumer as consumer_mod
from src.kafka.consumer import _resolve_topics


@pytest.fixture(autouse=True)
def _reset_consumer_stop_event():
    """Make sure the module-global stop event is clean before/after every test."""
    consumer_mod._stop_event.clear()
    yield
    consumer_mod._stop_event.clear()


# ---------- KAFKA_SUBSCRIBE_TOPICS parsing ----------


class TestSubscribeTopicsEnvVar:
    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("KAFKA_SUBSCRIBE_TOPICS", raising=False)
        assert _resolve_topics() == ["chat.request", "route.request", "geocode.request"]

    def test_default_when_empty_string(self, monkeypatch):
        monkeypatch.setenv("KAFKA_SUBSCRIBE_TOPICS", "")
        assert _resolve_topics() == ["chat.request", "route.request", "geocode.request"]

    def test_default_when_whitespace_only(self, monkeypatch):
        monkeypatch.setenv("KAFKA_SUBSCRIBE_TOPICS", "   ")
        assert _resolve_topics() == ["chat.request", "route.request", "geocode.request"]

    def test_parses_two_topics(self, monkeypatch):
        monkeypatch.setenv("KAFKA_SUBSCRIBE_TOPICS", "foo.bar,baz.qux")
        assert _resolve_topics() == ["foo.bar", "baz.qux"]

    def test_trims_whitespace_around_topics(self, monkeypatch):
        monkeypatch.setenv("KAFKA_SUBSCRIBE_TOPICS", "  foo.bar , baz.qux  ")
        assert _resolve_topics() == ["foo.bar", "baz.qux"]

    def test_ignores_empty_tokens(self, monkeypatch):
        monkeypatch.setenv("KAFKA_SUBSCRIBE_TOPICS", "foo.bar,,baz.qux,")
        assert _resolve_topics() == ["foo.bar", "baz.qux"]


# ---------- _consumer_loop dispatcher robustness ----------


class _FakeMsg:
    """Stand-in for confluent_kafka.Message returned by Consumer.poll()."""

    def __init__(self, *, topic: str, key: bytes | None, value: bytes | None, err=None):
        self._topic = topic
        self._key = key
        self._value = value
        self._err = err

    def error(self):
        return self._err

    def topic(self):
        return self._topic

    def key(self):
        return self._key

    def value(self):
        return self._value


def _run_loop_for_messages(messages: list, *, timeout: float = 1.0):
    """Spin up _consumer_loop in a thread, drive it through `messages`, then stop."""
    consumer_mod._stop_event.clear()

    fake_consumer = MagicMock()
    queue = list(messages) + [None] * 10  # trailing Nones simulate idle polls

    def fake_poll(_timeout):
        if not queue:
            consumer_mod._stop_event.set()
            return None
        return queue.pop(0)

    fake_consumer.poll.side_effect = fake_poll
    fake_consumer.subscribe = MagicMock()
    fake_consumer.close = MagicMock()

    with patch("src.kafka.consumer.Consumer", return_value=fake_consumer):
        thread = threading.Thread(target=consumer_mod._consumer_loop, daemon=True)
        thread.start()
        deadline = time.time() + timeout
        while thread.is_alive() and time.time() < deadline:
            time.sleep(0.01)
        consumer_mod._stop_event.set()
        thread.join(timeout=1)
    return fake_consumer


def test_consumer_subscribes_to_env_var_list(monkeypatch):
    monkeypatch.setenv("KAFKA_SUBSCRIBE_TOPICS", "foo.bar,baz.qux")
    fake_consumer = _run_loop_for_messages([])
    fake_consumer.subscribe.assert_called_once_with(["foo.bar", "baz.qux"])


def test_unknown_topic_logs_warn_does_not_crash(monkeypatch, caplog):
    monkeypatch.setenv("KAFKA_SUBSCRIBE_TOPICS", "unknown.topic,chat.request")
    msg = _FakeMsg(
        topic="unknown.topic",
        key=b"k1",
        value=json.dumps({"hello": "world"}).encode(),
    )
    with caplog.at_level("WARNING", logger="src.kafka.consumer"):
        _run_loop_for_messages([msg])
    assert any(
        "no handler registered for topic=unknown.topic" in rec.message for rec in caplog.records
    ), caplog.text


def test_malformed_json_logs_error_continues(monkeypatch, caplog):
    monkeypatch.setenv("KAFKA_SUBSCRIBE_TOPICS", "chat.request")
    bad_msg = _FakeMsg(topic="chat.request", key=b"k", value=b"{not-json")
    with caplog.at_level("ERROR", logger="src.kafka.consumer"):
        _run_loop_for_messages([bad_msg])
    assert any("failed to decode message" in rec.message for rec in caplog.records), caplog.text


def test_handler_exception_logged_loop_keeps_running(monkeypatch, caplog):
    monkeypatch.setenv("KAFKA_SUBSCRIBE_TOPICS", "chat.request")

    def boom(_key, _data):
        raise RuntimeError("simulated handler crash")

    with patch.dict(consumer_mod.TOPIC_HANDLERS, {"chat.request": boom}, clear=False):
        msg = _FakeMsg(topic="chat.request", key=b"k", value=json.dumps({"x": 1}).encode())
        with caplog.at_level("ERROR", logger="src.kafka.consumer"):
            _run_loop_for_messages([msg])
        assert any("simulated handler crash" in rec.message for rec in caplog.records), caplog.text
