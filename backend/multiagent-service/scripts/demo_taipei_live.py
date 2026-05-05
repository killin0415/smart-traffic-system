"""
Smoke-test the Taipei agent routing pipeline end-to-end (no Kafka, no FastAPI).

Steps:
  1. Load `.env` from repo root.
  2. Build in-memory RoadGraph from DB.
  3. Wire kafka_runtime so the routing tool / chat agent see the graph.
  4. Run one round of TDX live refresh — exercises the Section/-99 filter,
     Redis cache, traffic_history insert, and graph weight update.
  5. Print verification numbers (graph size + refresh result).

Run:
    uv run python scripts/demo_taipei_live.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Make `src.*` importable when run as `uv run python scripts/demo_taipei_live.py`.
_SVC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SVC_ROOT))


def _load_env():
    env_path = Path(__file__).resolve().parents[3] / ".env"
    if not env_path.exists():
        print(f"[WARN] .env not found at {env_path}")
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())
    print(f"[INFO] Loaded env from {env_path}")


_load_env()


async def main():
    from src.agents.routing import RoadGraph
    from src.agents.traffic import refresh_traffic_data
    from src.db import async_session
    from src.kafka import runtime as kafka_runtime

    async with async_session() as s:
        graph = await RoadGraph.from_db(s)
    print(f"[INFO] RoadGraph: {len(graph.nodes)} nodes, {len(graph.edges)} edges")

    kafka_runtime.set_runtime(
        graph=graph,
        loop=asyncio.get_running_loop(),
        session_factory=async_session,
    )

    async with async_session() as s:
        result = await refresh_traffic_data(s, graph)
    print(f"[INFO] refresh_traffic_data result: {result}")


if __name__ == "__main__":
    asyncio.run(main())
