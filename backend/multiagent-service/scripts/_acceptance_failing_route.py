"""One-shot acceptance check for fix-osm-graph-topology §9.7:
plan the original failing route (25.0478,121.5170 -> 25.0337,121.5645)
and assert &gt;= 1 route is returned.

Run via:
    uv run python scripts/_acceptance_failing_route.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
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

ORIGIN = (25.0478, 121.5170)   # 台北車站
DEST = (25.0337, 121.5645)     # 信義區 (101 area)

# 10 OD pairs for §9.8 benchmark (Taipei common driving destinations
# inside the OSM coverage area; deliberately diverse short / mid / long).
BENCHMARK_PAIRS = [
    ((25.0478, 121.5170), (25.0337, 121.5645)),  # 台北車站 -> 101 (failing case)
    ((25.0478, 121.5170), (25.0448, 121.5233)),  # 台北車站 -> 善導寺
    ((25.0339, 121.5645), (25.0478, 121.5170)),  # 101 -> 台北車站 (reverse)
    ((25.0529, 121.5320), (25.0337, 121.5645)),  # 中山國中 -> 101
    ((25.0660, 121.5230), (25.0420, 121.5520)),  # 圓山 -> 信義
    ((25.0420, 121.5070), (25.0480, 121.5600)),  # 西門町 -> 101
    ((25.0340, 121.5450), (25.0700, 121.5700)),  # 大安 -> 南港
    ((25.0250, 121.5300), (25.0820, 121.5600)),  # 公館 -> 內湖
    ((25.0900, 121.5310), (25.0250, 121.5300)),  # 士林 -> 公館
    ((25.0660, 121.4880), (25.0700, 121.5700)),  # 西湖 -> 南港 (cross-city)
]


async def main():
    from src.agents.routing import RoadGraph, plan_optimal_route
    from src.agents.weight_provider import TaipeiWeightProvider
    from src.db import async_session

    print("[INFO] Loading graph from DB ...")
    t0 = time.perf_counter()
    async with async_session() as s:
        graph = await RoadGraph.from_db(s)
    t_load = time.perf_counter() - t0
    print(f"[INFO] Graph loaded in {t_load:.2f}s: {len(graph.nodes)} nodes, "
          f"{len(graph.edges)} edges")

    weight_provider = TaipeiWeightProvider()
    await weight_provider.rebuild(async_session)
    weight_provider.apply_to_graph(graph)
    print("[INFO] WeightProvider applied")

    # §9.7 — the original failing route must succeed.
    print(f"\n[§9.7] Planning {ORIGIN} -> {DEST} ...")
    async with async_session() as s:
        result = await plan_optimal_route(
            session=s, graph=graph, weight_provider=weight_provider,
            origin_lat=ORIGIN[0], origin_lng=ORIGIN[1],
            dest_lat=DEST[0], dest_lng=DEST[1],
            k=3,
        )
    routes = result.get("routes") or []
    print(f"[§9.7] routes returned: {len(routes)}, error={result.get('error')!r}")
    if not routes:
        print("[§9.7] FAIL: no routes")
        return 1
    for i, r in enumerate(routes):
        print(f"  route[{i}] dist={r['distance_km']:.2f}km time={r['estimated_time_min']:.1f}min")
    print("[§9.7] PASS: &gt;= 1 route returned")

    # §9.8 — benchmark 10 OD pairs.
    print("\n[§9.8] Benchmarking 10 OD pairs ...")
    latencies = []
    async with async_session() as s:
        for i, (o, d) in enumerate(BENCHMARK_PAIRS):
            t = time.perf_counter()
            r = await plan_optimal_route(
                session=s, graph=graph, weight_provider=weight_provider,
                origin_lat=o[0], origin_lng=o[1],
                dest_lat=d[0], dest_lng=d[1],
                k=3,
            )
            dt = (time.perf_counter() - t) * 1000.0
            latencies.append(dt)
            n_routes = len(r.get("routes") or [])
            err = r.get("error")
            print(f"  pair[{i}] {dt:6.1f}ms routes={n_routes} err={err!r}")
    avg = sum(latencies) / len(latencies)
    p95 = sorted(latencies)[int(len(latencies) * 0.95)]
    print(f"[§9.8] avg={avg:.1f}ms p95={p95:.1f}ms over {len(latencies)} pairs")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
