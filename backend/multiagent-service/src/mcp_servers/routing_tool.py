"""
MCP routing tool — wraps `plan_optimal_route()` for Gemini agent tool-calling.

The tool name is `plan_route`; it takes a `PlanRouteInput` and returns
`RouteResponse` (a `routes` list + optional `error`). The implementation
shares the in-memory `RoadGraph` and async session factory through
`src.kafka.runtime` so the FastMCP tool needs no construction-time wiring.

Both schemas are also exposed as Pydantic models so other call sites (e.g.
the Gemini chat agent's tool callable, unit tests) can validate input or
shape the output without going through the MCP transport.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

from src.agents.routing import plan_optimal_route
from src.kafka import runtime as kafka_runtime

logger = logging.getLogger(__name__)


# ---------- Schemas ----------


class PlanRouteInput(BaseModel):
    """Validated input for the `plan_route` MCP tool."""

    model_config = ConfigDict(extra="forbid")

    origin_lat: float = Field(description="Origin latitude (WGS84)")
    origin_lng: float = Field(description="Origin longitude (WGS84)")
    dest_lat: float = Field(description="Destination latitude (WGS84)")
    dest_lng: float = Field(description="Destination longitude (WGS84)")
    top_k: int = Field(default=3, ge=1, le=10, description="Number of route alternatives to return")


class RouteItem(BaseModel):
    """One route alternative in `RouteResponse.routes`."""

    path: list[int] = Field(description="Ordered list of TrafficNode IDs along the route")
    edges: list[int] = Field(description="Ordered list of TrafficEdge IDs along the route")
    coordinates: list[list[float]] = Field(
        default_factory=list,
        description="Ordered list of [lat, lng] pairs aligned with `path`, for client-side polyline rendering",
    )
    road_names: list[str] = Field(default_factory=list, description="Deduplicated road names")
    estimated_time_min: float = Field(description="Estimated travel time in minutes (live-weighted)")
    distance_km: float = Field(description="Total route distance in km")
    speed_cameras: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Speed camera dicts attached to edges along the route",
    )
    parking_suggestions: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Parking lots within 1 km of destination with >=10 free spaces "
                    "(only attached to the best route).",
    )


class RouteResponse(BaseModel):
    """Output of the `plan_route` MCP tool."""

    routes: list[RouteItem] = Field(default_factory=list)
    error: str | None = Field(default=None, description="Populated when no routes could be returned")


# ---------- Tool implementation ----------


async def plan_route(
    origin_lat: float,
    origin_lng: float,
    dest_lat: float,
    dest_lng: float,
    top_k: int = 3,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Plan top-K routes between two GPS points.

    Looks up the in-memory RoadGraph and async session factory via the shared
    `kafka_runtime`. Returns a dict matching `RouteResponse.model_dump()`.
    """
    payload = PlanRouteInput(
        origin_lat=origin_lat,
        origin_lng=origin_lng,
        dest_lat=dest_lat,
        dest_lng=dest_lng,
        top_k=top_k,
    )

    graph = kafka_runtime.get_graph()
    session_factory = kafka_runtime.get_session_factory()
    weight_provider = kafka_runtime.get_weight_provider()
    if graph is None or session_factory is None:
        return RouteResponse(
            routes=[],
            error="service not ready: graph/runtime uninitialised",
        ).model_dump()

    async with session_factory() as session:
        result = await plan_optimal_route(
            session,
            graph,
            weight_provider,
            payload.origin_lat,
            payload.origin_lng,
            payload.dest_lat,
            payload.dest_lng,
            user_id=user_id,
            k=payload.top_k,
        )
    return result


# ---------- MCP server factory ----------


def build_routing_mcp_server() -> FastMCP:
    """Construct an in-memory FastMCP server that exposes `plan_route`."""
    mcp = FastMCP(name="routing")

    @mcp.tool(name="plan_route", description="Plan top-K driving routes between two GPS points.")
    async def _plan_route(
        origin_lat: float,
        origin_lng: float,
        dest_lat: float,
        dest_lng: float,
        top_k: int = 3,
    ) -> dict[str, Any]:
        return await plan_route(origin_lat, origin_lng, dest_lat, dest_lng, top_k)

    return mcp
