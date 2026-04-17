"""
Shared runtime state for Kafka consumer handlers.

The consumer runs in a dedicated thread (confluent-kafka is not async-safe),
but route planning / TDX Live integration need the event loop, async session,
and the in-memory RoadGraph. This module is the bridge.
"""

from __future__ import annotations

import asyncio
from typing import Callable

from src.agents.routing import RoadGraph


_graph: RoadGraph | None = None
_loop: asyncio.AbstractEventLoop | None = None
_session_factory: Callable[..., object] | None = None


def set_runtime(
    graph: RoadGraph,
    loop: asyncio.AbstractEventLoop,
    session_factory: Callable[..., object],
) -> None:
    global _graph, _loop, _session_factory
    _graph = graph
    _loop = loop
    _session_factory = session_factory


def get_graph() -> RoadGraph | None:
    return _graph


def get_loop() -> asyncio.AbstractEventLoop | None:
    return _loop


def get_session_factory() -> Callable[..., object] | None:
    return _session_factory
