"""Integration tests for refresh_traffic_data with mocked TDX Live Section payload."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import src.agents.traffic as traffic_mod
from src.agents.routing import GraphEdge, GraphNode, RoadGraph
from src.agents.traffic import _is_sentinel, fetch_live_section_data


@pytest.fixture(autouse=True)
def _reset_token_cache():
    traffic_mod._token_cache["token"] = "fake-token"
    traffic_mod._token_cache["expires_at"] = 9_999_999_999
    yield


def _make_graph() -> RoadGraph:
    g = RoadGraph()
    g.nodes[1] = GraphNode(id=1, latitude=25.04, longitude=121.51)
    g.nodes[2] = GraphNode(id=2, latitude=25.05, longitude=121.52)
    edge = GraphEdge(
        id=10,
        source_node_id=1,
        target_node_id=2,
        road_name="test road",
        length_km=1.0,
        speed_limit_kmh=50,
        base_weight=0.02,
        tdx_section_id="L_TPSEC1",
    )
    g.edges[10] = edge
    g.adjacency[1] = [(2, 10, edge.base_weight)]
    g.adjacency[2] = [(1, 10, edge.base_weight)]
    g.section_to_edge["L_TPSEC1"] = 10
    g.max_speed_kmh = 50
    return g


# ---------- _is_sentinel filter ----------


class TestSentinelFilter:
    def test_drops_negative_speed(self):
        assert _is_sentinel({"TravelSpeed": -99, "TravelTime": 60, "CongestionLevel": "2"})

    def test_drops_negative_travel_time(self):
        assert _is_sentinel({"TravelSpeed": 40, "TravelTime": -99, "CongestionLevel": "2"})

    def test_drops_minus99_congestion(self):
        assert _is_sentinel({"TravelSpeed": 40, "TravelTime": 60, "CongestionLevel": "-99"})

    def test_keeps_healthy_row(self):
        assert not _is_sentinel({"TravelSpeed": 40.5, "TravelTime": 60, "CongestionLevel": "2"})

    def test_drops_zero_speed(self):
        assert _is_sentinel({"TravelSpeed": 0, "TravelTime": 60, "CongestionLevel": "2"})


# ---------- fetch_live_section_data ----------


@pytest.mark.asyncio
async def test_fetch_live_section_filters_sentinels():
    payload = {
        "LiveTraffics": [
            {"SectionID": "L_A", "TravelSpeed": 50.0, "TravelTime": 60.0, "CongestionLevel": "2"},
            {"SectionID": "L_B", "TravelSpeed": -99, "TravelTime": -99, "CongestionLevel": "-99"},
            {"SectionID": "L_C", "TravelSpeed": 25.0, "TravelTime": 120.0, "CongestionLevel": "3"},
            {"SectionID": "L_D", "TravelSpeed": 40, "TravelTime": -99, "CongestionLevel": "2"},
            # Missing SectionID — drop.
            {"TravelSpeed": 30, "TravelTime": 30, "CongestionLevel": "2"},
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)
    _OriginalClient = httpx.AsyncClient
    with patch(
        "src.agents.traffic.httpx.AsyncClient",
        lambda **kwargs: _OriginalClient(transport=transport, **{k: v for k, v in kwargs.items() if k != "transport"}),
    ):
        result = await fetch_live_section_data()

    sids = [r["tdx_section_id"] for r in result]
    assert sids == ["L_A", "L_C"]
    assert result[0]["travel_speed"] == 50.0
    assert result[1]["travel_speed"] == 25.0


# ---------- refresh_traffic_data orchestration ----------


@pytest.mark.asyncio
async def test_refresh_traffic_data_propagates_to_redis_db_and_graph():
    graph = _make_graph()

    payload = {
        "LiveTraffics": [
            {"SectionID": "L_TPSEC1", "TravelSpeed": 25.0, "TravelTime": 144.0, "CongestionLevel": "3"},
            {"SectionID": "L_OTHER", "TravelSpeed": 60.0, "TravelTime": 30.0, "CongestionLevel": "1"},
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)

    session = MagicMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()

    redis_pipe = MagicMock()
    redis_pipe.set = MagicMock()
    redis_pipe.execute = AsyncMock()

    _OriginalClient = httpx.AsyncClient
    with patch.object(traffic_mod.redis_client, "pipeline", return_value=redis_pipe):
        with patch(
            "src.agents.traffic.httpx.AsyncClient",
            lambda **kwargs: _OriginalClient(transport=transport, **{k: v for k, v in kwargs.items() if k != "transport"}),
        ):
            result = await traffic_mod.refresh_traffic_data(session, graph)

    # 2 sections fetched (filtering passed both rows).
    assert result["fetched"] == 2
    # Only L_TPSEC1 maps to a graph edge (10).
    assert result["updated_edges"] == 1

    # Redis cache: both sections written.
    assert redis_pipe.set.call_count == 2
    keys = [call.args[0] for call in redis_pipe.set.call_args_list]
    assert "traffic:section:L_TPSEC1" in keys
    body = json.loads(redis_pipe.set.call_args_list[0].args[1])
    assert "travel_speed" in body and "travel_time" in body and "updated_at" in body

    # TimescaleDB write: at least one execute (insert) and a commit.
    assert session.execute.await_count >= 1
    assert session.commit.await_count >= 1

    # Graph weight increased: speed_limit=50, current=25 ⇒ factor=2.0
    new_weight = graph.get_weight(10)
    assert new_weight > graph.edges[10].base_weight


@pytest.mark.asyncio
async def test_refresh_traffic_data_skips_update_when_fetch_fails():
    graph = _make_graph()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)

    session = MagicMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()

    _OriginalClient = httpx.AsyncClient
    with patch.object(traffic_mod, "update_redis_cache", new=AsyncMock()) as mock_redis:
        with patch.object(traffic_mod, "update_timescaledb", new=AsyncMock()) as mock_tsdb:
            with patch(
                "src.agents.traffic.httpx.AsyncClient",
                lambda **kwargs: _OriginalClient(transport=transport, **{k: v for k, v in kwargs.items() if k != "transport"}),
            ):
                result = await traffic_mod.refresh_traffic_data(session, graph)

    assert "error" in result
    mock_redis.assert_not_awaited()
    mock_tsdb.assert_not_awaited()
    # Graph weight remains at base.
    assert graph.get_weight(10) == graph.edges[10].base_weight


@pytest.mark.asyncio
async def test_refresh_handles_all_sentinel_payload():
    """If all rows are -99 sentinels, Redis/DB write are no-ops, no edges updated."""
    graph = _make_graph()
    payload = {
        "LiveTraffics": [
            {"SectionID": "L_TPSEC1", "TravelSpeed": -99, "TravelTime": -99, "CongestionLevel": "-99"},
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)

    session = MagicMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()

    _OriginalClient = httpx.AsyncClient
    with patch.object(traffic_mod, "update_timescaledb", new=AsyncMock()) as mock_tsdb:
        with patch.object(traffic_mod, "update_redis_cache", new=AsyncMock()) as mock_redis:
            with patch(
                "src.agents.traffic.httpx.AsyncClient",
                lambda **kwargs: _OriginalClient(transport=transport, **{k: v for k, v in kwargs.items() if k != "transport"}),
            ):
                result = await traffic_mod.refresh_traffic_data(session, graph)

    assert result["fetched"] == 0
    assert result["updated_edges"] == 0
    # Both helpers still get called with [] (no-op inside).
    mock_redis.assert_awaited_once_with([])
    mock_tsdb.assert_awaited_once()
    assert graph.get_weight(10) == graph.edges[10].base_weight
