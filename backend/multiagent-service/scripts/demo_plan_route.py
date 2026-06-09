
"""
Demo: call the `plan_route` MCP tool directly for Taipei Main Station → Xinyi Eslite.

Run after `demo_taipei_live.py` (which seeds Redis cache with live traffic).

Usage:
    uv run python scripts/demo_plan_route.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

_SVC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SVC_ROOT))


def _load_env():
    env_path = Path(__file__).resolve().parents[3] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


_load_env()


# Origin: Taipei Main Station (台北車站)
# Destination: Shandao Temple (善導寺) — in-bbox neighbour ~700m east.
#
# Note: the original aspirational demo destination (信義誠品, ~25.0418, 121.5654)
# falls outside the 2.2km bbox set by spec D5 / task 2.1, so the road network
# does not include it. Pick an in-bbox destination here; expand the bbox if a
# 信義 demo is required in future.
ORIGIN = (25.0480, 121.5170)
DEST = (25.0448, 121.5233)


async def main():
    from src.agents.routing import RoadGraph
    from src.agents.weight_provider import TaipeiWeightProvider
    from src.db import async_session
    from src.kafka import runtime as kafka_runtime
    from src.mcp_servers.routing_tool import plan_route

    async with async_session() as s:
        graph = await RoadGraph.from_db(s)
    print(f"[INFO] RoadGraph: {len(graph.nodes)} nodes, {len(graph.edges)} edges")

    # Without this, every edge weight is 0 (placeholder set by RoadGraph.from_db)
    # and estimated_time_min collapses to ~0 minutes.
    weight_provider = TaipeiWeightProvider()
    await weight_provider.rebuild(async_session)
    weight_provider.apply_to_graph(graph)

    kafka_runtime.set_runtime(
        graph=graph,
        loop=asyncio.get_running_loop(),
        session_factory=async_session,
    )
    kafka_runtime.set_weight_provider(weight_provider)

    result = await plan_route(
        origin_lat=ORIGIN[0],
        origin_lng=ORIGIN[1],
        dest_lat=DEST[0],
        dest_lng=DEST[1],
        top_k=3,
    )
    routes = result.get("routes") or []
    print(f"[INFO] plan_route returned {len(routes)} route(s); error={result.get('error')!r}")
    for i, r in enumerate(routes):
        print(
            f"  [{i}] dist={r['distance_km']:.2f} km, "
            f"time={r['estimated_time_min']:.1f} min, "
            f"roads={r['road_names']}"
        )
    print("---raw---")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
