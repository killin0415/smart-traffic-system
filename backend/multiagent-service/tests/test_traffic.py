"""Integration test for refresh_traffic_data with mocked TDX VD live payload."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import src.agents.traffic as traffic_mod
from src.agents.routing import GraphEdge, GraphNode, RoadGraph


@pytest.fixture(autouse=True)
def _reset_caches():
    traffic_mod._token_cache["token"] = "fake-token"
    traffic_mod._token_cache["expires_at"] = 9_999_999_999
    traffic_mod._edge_map_cache = None
    yield
    traffic_mod._edge_map_cache = None


def _make_graph() -> RoadGraph:
    g = RoadGraph()
    g.nodes[1] = GraphNode(id=1, latitude=22.6, longitude=120.3)
    g.nodes[2] = GraphNode(id=2, latitude=22.7, longitude=120.4)
    edge = GraphEdge(
        id=10,
        source_node_id=1,
        target_node_id=2,
        road_name="test road",
        length_km=1.0,
        speed_limit_kmh=50,
        base_weight=0.02,
        tdx_section_id="L_SEC1",
    )
    g.edges[10] = edge
    g.adjacency[1] = [(2, 10, edge.base_weight)]
    g.adjacency[2] = [(1, 10, edge.base_weight)]
    g.section_to_edge["L_SEC1"] = 10
    g.max_speed_kmh = 50
    return g


@pytest.mark.asyncio
async def test_refresh_traffic_data_propagates_to_redis_db_and_graph():
    graph = _make_graph()

    # MockTransport returns the VD live payload.
    vd_payload = {
        "VDLives": [
            {
                "VDID": "VD-1",
                "LinkFlows": [
                    {"LinkID": "LL", "Lanes": [{"Speed": 25, "ErrorType": ""}, {"Speed": 35, "ErrorType": ""}]}
                ],
            }
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=vd_payload)

    transport = httpx.MockTransport(handler)

    # Stub session: returns the {VD-1: (10, "L_SEC1", 50)} edge map.
    session = MagicMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()

    redis_pipe = MagicMock()
    redis_pipe.set = MagicMock()
    redis_pipe.execute = AsyncMock()

    with patch.object(traffic_mod, "load_vd_edge_map", new=AsyncMock(return_value={"VD-1": (10, "L_SEC1", 50)})):
        with patch.object(traffic_mod, "update_timescaledb", new=AsyncMock()) as mock_tsdb:
            with patch.object(traffic_mod.redis_client, "pipeline", return_value=redis_pipe):
                _OriginalClient = httpx.AsyncClient
                with patch("src.agents.traffic.httpx.AsyncClient", lambda **kwargs: _OriginalClient(transport=transport, **{k: v for k, v in kwargs.items() if k != "transport"})):
                    result = await traffic_mod.refresh_traffic_data(session, graph)

    assert result["fetched"] == 1
    assert result["healthy"] == 1
    assert result["updated_edges"] == 1

    # Redis pipeline.set was called with key=traffic:section:L_SEC1
    redis_pipe.set.assert_called_once()
    call_args = redis_pipe.set.call_args
    assert call_args.args[0] == "traffic:section:L_SEC1"
    cached = json.loads(call_args.args[1])
    assert cached["travel_speed"] == 30.0  # avg(25, 35)

    # TimescaleDB write happened with the right shape.
    mock_tsdb.assert_awaited_once()
    args, _ = mock_tsdb.await_args
    section_data = args[1]
    assert section_data[0]["edge_id"] == 10
    assert section_data[0]["tdx_section_id"] == "L_SEC1"

    # Graph weight was patched: avg=30, limit=50 ⇒ factor = 50/30 ≈ 1.667
    new_weight = graph.get_weight(10)
    assert new_weight > graph.edges[10].base_weight  # weight increased due to congestion


@pytest.mark.asyncio
async def test_refresh_traffic_data_skips_update_when_fetch_fails():
    graph = _make_graph()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)

    session = MagicMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()

    with patch.object(traffic_mod, "load_vd_edge_map", new=AsyncMock(return_value={})):
        with patch.object(traffic_mod, "update_timescaledb", new=AsyncMock()) as mock_tsdb:
            with patch.object(traffic_mod, "update_redis_cache", new=AsyncMock()) as mock_redis:
                _OriginalClient = httpx.AsyncClient
                with patch("src.agents.traffic.httpx.AsyncClient", lambda **kwargs: _OriginalClient(transport=transport, **{k: v for k, v in kwargs.items() if k != "transport"})):
                    result = await traffic_mod.refresh_traffic_data(session, graph)

    assert "error" in result
    mock_tsdb.assert_not_awaited()
    mock_redis.assert_not_awaited()
    # Graph weight remains at base.
    assert graph.get_weight(10) == graph.edges[10].base_weight
