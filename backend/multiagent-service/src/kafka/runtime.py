"""
Shared runtime state for Kafka consumer handlers.

The consumer runs in a dedicated thread (confluent-kafka is not async-safe),
but route planning needs the event loop, async session factory, in-memory
RoadGraph, and current WeightProvider. This module is the bridge — pure
module-level globals + setters/getters, no RuntimeContext class.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Callable

from src.agents.routing import RoadGraph

if TYPE_CHECKING:
    from src.agents.chat_agent import ChatAgent
    from src.agents.weight_provider import WeightProvider


_graph: RoadGraph | None = None
_loop: asyncio.AbstractEventLoop | None = None
_session_factory: Callable[..., object] | None = None
_chat_agent: Any | None = None  # ChatAgent instance, typed Any to avoid import-cycle
_weight_provider: Any | None = None  # WeightProvider, same reason


def set_runtime(
    graph: RoadGraph | None,
    loop: asyncio.AbstractEventLoop | None,
    session_factory: Callable[..., object] | None,
    chat_agent: "ChatAgent | None" = None,
) -> None:
    global _graph, _loop, _session_factory, _chat_agent
    _graph = graph
    _loop = loop
    _session_factory = session_factory
    _chat_agent = chat_agent


def set_weight_provider(wp: "WeightProvider | None") -> None:
    global _weight_provider
    _weight_provider = wp


def get_graph() -> RoadGraph | None:
    return _graph


def get_loop() -> asyncio.AbstractEventLoop | None:
    return _loop


def get_session_factory() -> Callable[..., object] | None:
    return _session_factory


def get_chat_agent() -> "ChatAgent | None":
    return _chat_agent


def get_weight_provider() -> "WeightProvider | None":
    return _weight_provider
