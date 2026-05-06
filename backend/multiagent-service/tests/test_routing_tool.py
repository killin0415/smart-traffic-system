"""Schema validation + happy/sad-path tests for the MCP routing tool."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from src.kafka import runtime as kafka_runtime
from src.mcp_servers.routing_tool import (
    PlanRouteInput,
    RouteItem,
    RouteResponse,
    build_routing_mcp_server,
    plan_route,
)


# ---------- PlanRouteInput schema ----------


class TestPlanRouteInputSchema:
    def test_valid_minimal_input(self):
        inp = PlanRouteInput(
            origin_lat=25.0478,
            origin_lng=121.5170,
            dest_lat=25.0418,
            dest_lng=121.5654,
        )
        assert inp.top_k == 3  # default

    def test_explicit_top_k(self):
        inp = PlanRouteInput(
            origin_lat=25.0,
            origin_lng=121.5,
            dest_lat=25.1,
            dest_lng=121.6,
            top_k=5,
        )
        assert inp.top_k == 5

    def test_missing_required_field_raises(self):
        with pytest.raises(ValidationError):
            PlanRouteInput(origin_lat=25.0, origin_lng=121.5, dest_lat=25.1)  # type: ignore[call-arg]

    def test_invalid_type_raises(self):
        with pytest.raises(ValidationError):
            PlanRouteInput(
                origin_lat="not-a-float",  # type: ignore[arg-type]
                origin_lng=121.5,
                dest_lat=25.1,
                dest_lng=121.6,
            )

    def test_top_k_out_of_range_raises(self):
        with pytest.raises(ValidationError):
            PlanRouteInput(
                origin_lat=25.0, origin_lng=121.5, dest_lat=25.1, dest_lng=121.6, top_k=0
            )
        with pytest.raises(ValidationError):
            PlanRouteInput(
                origin_lat=25.0, origin_lng=121.5, dest_lat=25.1, dest_lng=121.6, top_k=11
            )

    def test_extra_field_forbidden(self):
        with pytest.raises(ValidationError):
            PlanRouteInput(
                origin_lat=25.0,
                origin_lng=121.5,
                dest_lat=25.1,
                dest_lng=121.6,
                stranger="hi",  # type: ignore[call-arg]
            )


# ---------- RouteResponse schema alignment ----------


class TestRouteResponseSchema:
    def test_round_trip_serialises_routeitem_fields(self):
        item = RouteItem(
            path=[1, 2, 3],
            edges=[10, 20],
            road_names=["A", "B"],
            estimated_time_min=12.5,
            distance_km=4.321,
            speed_cameras=[{"latitude": 25.0, "longitude": 121.5, "speed_limit": 50}],
        )
        dumped = RouteResponse(routes=[item]).model_dump()
        assert set(dumped.keys()) == {"routes", "error"}
        assert dumped["error"] is None
        route = dumped["routes"][0]
        assert set(route.keys()) >= {
            "path",
            "edges",
            "road_names",
            "estimated_time_min",
            "distance_km",
            "speed_cameras",
        }

    def test_empty_routes_with_error(self):
        dumped = RouteResponse(routes=[], error="no path found").model_dump()
        assert dumped["routes"] == []
        assert dumped["error"] == "no path found"


# ---------- plan_route tool function ----------


@pytest.fixture
def _reset_runtime():
    kafka_runtime.set_runtime(graph=None, loop=None, session_factory=None)  # type: ignore[arg-type]
    yield
    kafka_runtime.set_runtime(graph=None, loop=None, session_factory=None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_plan_route_returns_service_not_ready_when_runtime_uninitialised(_reset_runtime):
    out = await plan_route(25.0478, 121.5170, 25.0418, 121.5654)
    assert out["routes"] == []
    assert out["error"] == "service not ready: graph/runtime uninitialised"


@pytest.mark.asyncio
async def test_plan_route_happy_path_calls_plan_optimal_route(_reset_runtime):
    fake_graph = MagicMock()
    fake_session = MagicMock()

    class _FakeSessionCtx:
        async def __aenter__(self):
            return fake_session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    def _factory():
        return _FakeSessionCtx()

    kafka_runtime.set_runtime(graph=fake_graph, loop=MagicMock(), session_factory=_factory)

    fake_result = {
        "routes": [
            {
                "path": [1, 2, 3],
                "edges": [10, 20],
                "road_names": ["A", "B"],
                "estimated_time_min": 8.0,
                "distance_km": 1.234,
                "speed_cameras": [],
            }
        ],
        "error": None,
    }

    with patch(
        "src.mcp_servers.routing_tool.plan_optimal_route",
        new=AsyncMock(return_value=fake_result),
    ) as mock_plan:
        out = await plan_route(25.0478, 121.5170, 25.0418, 121.5654, top_k=2)

    mock_plan.assert_awaited_once()
    args, kwargs = mock_plan.await_args
    assert args[0] is fake_session
    assert args[1] is fake_graph
    # args[2] is the weight_provider (None here; runtime hasn't set it)
    assert args[2] is None
    assert args[3:7] == (25.0478, 121.5170, 25.0418, 121.5654)
    assert kwargs["k"] == 2
    assert kwargs.get("user_id") is None
    assert out == fake_result


@pytest.mark.asyncio
async def test_plan_route_validates_input_via_pydantic(_reset_runtime):
    """Top-level args go through PlanRouteInput so e.g. top_k=0 raises.

    Runtime is intentionally left unwired — Pydantic validation must fire
    before plan_optimal_route is ever called, regardless of factory state.
    """
    with patch(
        "src.mcp_servers.routing_tool.plan_optimal_route", new=AsyncMock()
    ) as mock_plan:
        with pytest.raises(ValidationError):
            await plan_route(25.0, 121.5, 25.1, 121.6, top_k=0)
        mock_plan.assert_not_awaited()


# ---------- MCP server registration ----------


@pytest.mark.asyncio
async def test_build_routing_mcp_server_registers_plan_route():
    server = build_routing_mcp_server()
    tools = await server.list_tools()
    names = [t.name for t in tools]
    assert "plan_route" in names
