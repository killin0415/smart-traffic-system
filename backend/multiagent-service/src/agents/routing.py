"""
A* 路徑規劃引擎。

啟動時從 DB 載入路網建構 in-memory adjacency dict，提供：
- RoadGraph: 路網圖結構 + 動態 weight 更新
- snap_to_graph: GPS 座標對應到最近高 degree node
- astar: A* 搜尋（haversine/max_speed heuristic）
- find_top_k_routes: penalty-based top-K
- plan_optimal_route: 入口函數，包含 snap → top-K → 附帶測速照相機
"""

from __future__ import annotations

import heapq
import logging
import math
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import SpeedCamera, TrafficEdge, TrafficNode

logger = logging.getLogger(__name__)

EARTH_RADIUS_KM = 6371.0
DEFAULT_TOP_K = 3
DEFAULT_PENALTY = 3.0
MAX_CONGESTION_FACTOR = 10.0


# ---------- Data structures ----------


@dataclass
class GraphNode:
    id: int
    latitude: float
    longitude: float


@dataclass
class GraphEdge:
    id: int
    source_node_id: int
    target_node_id: int
    road_name: str
    length_km: float
    speed_limit_kmh: int
    base_weight: float
    tdx_section_id: str | None = None


@dataclass
class RouteResult:
    path: list[int]
    edges: list[int]
    road_names: list[str]
    estimated_time_min: float
    distance_km: float
    speed_cameras: list[dict] = field(default_factory=list)


# ---------- Haversine ----------


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in km between two (lat, lng) points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dl = math.radians(lng2 - lng1)
    h = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


# ---------- RoadGraph ----------


class RoadGraph:
    """In-memory road network graph with dynamic edge weights.

    Adjacency dict format:
        adjacency[node_id] = [(neighbor_id, edge_id, dynamic_weight), ...]
    """

    def __init__(self) -> None:
        self.nodes: dict[int, GraphNode] = {}
        self.edges: dict[int, GraphEdge] = {}
        self.adjacency: dict[int, list[tuple[int, int, float]]] = {}
        self.section_to_edge: dict[str, int] = {}
        self.max_speed_kmh: int = 1

    @classmethod
    async def from_db(cls, session: AsyncSession) -> RoadGraph:
        """Load all nodes + edges from DB and build graph."""
        graph = cls()

        node_rows = (await session.execute(select(TrafficNode))).scalars().all()
        for n in node_rows:
            graph.nodes[n.id] = GraphNode(id=n.id, latitude=n.latitude, longitude=n.longitude)

        edge_rows = (await session.execute(select(TrafficEdge))).scalars().all()
        for e in edge_rows:
            ge = GraphEdge(
                id=e.id,
                source_node_id=e.source_node_id,
                target_node_id=e.target_node_id,
                road_name=e.road_name or "",
                length_km=e.length_km,
                speed_limit_kmh=e.speed_limit_kmh,
                base_weight=e.base_weight,
                tdx_section_id=e.tdx_section_id,
            )
            graph.edges[e.id] = ge
            graph.adjacency.setdefault(e.source_node_id, []).append((e.target_node_id, e.id, e.base_weight))
            # Treat edges as bidirectional for routing; TDX Section data lacks direction metadata.
            graph.adjacency.setdefault(e.target_node_id, []).append((e.source_node_id, e.id, e.base_weight))
            if e.tdx_section_id:
                graph.section_to_edge[e.tdx_section_id] = e.id
            if e.speed_limit_kmh and e.speed_limit_kmh > graph.max_speed_kmh:
                graph.max_speed_kmh = e.speed_limit_kmh

        # Ensure every node has an adjacency entry (even if isolated).
        for nid in graph.nodes:
            graph.adjacency.setdefault(nid, [])

        logger.info(
            "RoadGraph loaded: %d nodes, %d edges, max_speed=%d km/h",
            len(graph.nodes),
            len(graph.edges),
            graph.max_speed_kmh,
        )
        return graph

    def update_weight(self, edge_id: int, congestion_factor: float) -> None:
        """Patch dynamic_weight for a single edge on both directions of the adjacency list."""
        edge = self.edges.get(edge_id)
        if edge is None:
            return
        new_weight = edge.base_weight * max(congestion_factor, 1e-6)
        for u, v in ((edge.source_node_id, edge.target_node_id), (edge.target_node_id, edge.source_node_id)):
            neighbors = self.adjacency.get(u, [])
            for i, (nb, eid, _w) in enumerate(neighbors):
                if eid == edge_id and nb == v:
                    neighbors[i] = (nb, eid, new_weight)
                    break

    def get_weight(self, edge_id: int) -> float:
        """Read current dynamic weight of an edge (from adjacency, not base)."""
        edge = self.edges.get(edge_id)
        if edge is None:
            return math.inf
        for nb, eid, w in self.adjacency.get(edge.source_node_id, []):
            if eid == edge_id and nb == edge.target_node_id:
                return w
        return edge.base_weight

    def degree(self, node_id: int) -> int:
        return len(self.adjacency.get(node_id, []))


# ---------- Snap to graph ----------


def snap_to_graph(lat: float, lng: float, graph: RoadGraph, k: int = 3) -> int | None:
    """Find nearest K nodes, return the one with highest degree.

    Ties on degree break by smaller distance.
    """
    if not graph.nodes:
        return None

    # Compute distance to every node (O(N); fine for ~thousand nodes).
    distances = [
        (haversine_km(lat, lng, n.latitude, n.longitude), n.id)
        for n in graph.nodes.values()
    ]
    distances.sort(key=lambda x: x[0])
    top_k = distances[: max(k, 1)]

    # Highest degree wins; ties -> smaller distance.
    best_id = top_k[0][1]
    best_degree = graph.degree(best_id)
    best_dist = top_k[0][0]
    for dist, nid in top_k[1:]:
        d = graph.degree(nid)
        if d > best_degree or (d == best_degree and dist < best_dist):
            best_id, best_degree, best_dist = nid, d, dist
    return best_id


# ---------- A* ----------


def astar(
    graph: RoadGraph,
    start_id: int,
    end_id: int,
    weight_overrides: dict[int, float] | None = None,
) -> tuple[list[int], list[int], float] | None:
    """A* shortest path.

    Heuristic: haversine_km(current, end) / max_speed_kmh  (admissible — underestimates time).

    Returns (node_path, edge_path, total_cost_hours) or None if unreachable.
    `weight_overrides[edge_id]` lets callers apply temporary penalties without mutating graph.
    """
    if start_id == end_id:
        return ([start_id], [], 0.0)
    if start_id not in graph.nodes or end_id not in graph.nodes:
        return None

    end_node = graph.nodes[end_id]
    max_speed = max(graph.max_speed_kmh, 1)

    def h(node_id: int) -> float:
        n = graph.nodes[node_id]
        return haversine_km(n.latitude, n.longitude, end_node.latitude, end_node.longitude) / max_speed

    open_heap: list[tuple[float, int, int]] = []  # (f, counter, node_id)
    counter = 0
    heapq.heappush(open_heap, (h(start_id), counter, start_id))

    came_from: dict[int, tuple[int, int]] = {}  # node -> (prev_node, edge_id)
    g_score: dict[int, float] = {start_id: 0.0}

    while open_heap:
        _, _, current = heapq.heappop(open_heap)
        if current == end_id:
            return _reconstruct(came_from, start_id, end_id, g_score[end_id])

        current_g = g_score[current]
        for nb, eid, base_w in graph.adjacency.get(current, []):
            w = weight_overrides.get(eid, base_w) if weight_overrides else base_w
            tentative = current_g + w
            if tentative < g_score.get(nb, math.inf):
                g_score[nb] = tentative
                came_from[nb] = (current, eid)
                counter += 1
                heapq.heappush(open_heap, (tentative + h(nb), counter, nb))

    return None


def _reconstruct(
    came_from: dict[int, tuple[int, int]],
    start_id: int,
    end_id: int,
    total_cost: float,
) -> tuple[list[int], list[int], float]:
    nodes: list[int] = [end_id]
    edges: list[int] = []
    cur = end_id
    while cur != start_id:
        prev, eid = came_from[cur]
        nodes.append(prev)
        edges.append(eid)
        cur = prev
    nodes.reverse()
    edges.reverse()
    return nodes, edges, total_cost


# ---------- Top-K penalty-based ----------


def find_top_k_routes(
    graph: RoadGraph,
    start_id: int,
    end_id: int,
    k: int = DEFAULT_TOP_K,
    penalty: float = DEFAULT_PENALTY,
) -> list[tuple[list[int], list[int], float]]:
    """Penalty-based top-K: after each A* run, multiply used edges' weight by `penalty` and rerun.

    Final cost for each route is re-computed with the ORIGINAL weights, and results are
    sorted by that real cost ascending. Duplicate paths are filtered.
    """
    overrides: dict[int, float] = {}
    results: list[tuple[list[int], list[int], float]] = []
    seen_edge_sets: set[tuple[int, ...]] = set()

    for _ in range(max(k, 1)):
        result = astar(graph, start_id, end_id, weight_overrides=overrides)
        if result is None:
            break
        nodes, edges, _penalized_cost = result
        edge_key = tuple(edges)
        if edge_key in seen_edge_sets:
            # Penalty didn't produce a different path — no point continuing.
            break
        seen_edge_sets.add(edge_key)

        # Recompute cost with original (or currently live) weights via graph.get_weight.
        real_cost = sum(graph.get_weight(eid) for eid in edges)
        results.append((nodes, edges, real_cost))

        # Penalise all edges used so next iteration picks a different route.
        for eid in edges:
            base_w = graph.edges[eid].base_weight
            overrides[eid] = overrides.get(eid, base_w) * penalty

    results.sort(key=lambda r: r[2])
    return results


# ---------- Entry point ----------


async def plan_optimal_route(
    session: AsyncSession,
    graph: RoadGraph,
    origin_lat: float,
    origin_lng: float,
    dest_lat: float,
    dest_lng: float,
    k: int = DEFAULT_TOP_K,
) -> dict:
    """Plan top-K routes from origin to destination, including speed cameras along the way.

    Returns a JSON-serialisable dict matching `RouteResponse` schema:
    `{"routes": [RouteItem, ...], "error": str | None}`.
    """
    # Local import to avoid a circular dependency (`routing_tool` imports `plan_optimal_route`).
    from src.mcp_servers.routing_tool import RouteItem, RouteResponse

    if not graph.nodes:
        return RouteResponse(routes=[], error="road network not loaded").model_dump()

    start_id = snap_to_graph(origin_lat, origin_lng, graph)
    end_id = snap_to_graph(dest_lat, dest_lng, graph)
    if start_id is None or end_id is None:
        return RouteResponse(
            routes=[],
            error="could not snap origin/destination to graph",
        ).model_dump()

    raw_routes = find_top_k_routes(graph, start_id, end_id, k=k)
    if not raw_routes:
        return RouteResponse(
            routes=[],
            error="no path found between origin and destination",
        ).model_dump()

    # Aggregate all edge ids, then one DB query to fetch associated speed cameras.
    all_edge_ids = {eid for _, edges, _ in raw_routes for eid in edges}
    cameras_by_edge: dict[int, list[dict]] = {}
    if all_edge_ids:
        cam_rows = (
            await session.execute(
                select(SpeedCamera).where(SpeedCamera.nearest_edge_id.in_(all_edge_ids))
            )
        ).scalars().all()
        for c in cam_rows:
            cameras_by_edge.setdefault(c.nearest_edge_id, []).append(
                {
                    "latitude": c.latitude,
                    "longitude": c.longitude,
                    "direction": c.direction,
                    "speed_limit": c.speed_limit,
                    "address": c.address,
                }
            )

    routes_out: list[RouteItem] = []
    for nodes, edges, cost_hours in raw_routes:
        edge_objs = [graph.edges[eid] for eid in edges]
        road_names = _dedupe_preserve_order([e.road_name for e in edge_objs if e.road_name])
        distance_km = sum(e.length_km for e in edge_objs)
        cameras = [cam for eid in edges for cam in cameras_by_edge.get(eid, [])]
        routes_out.append(
            RouteItem(
                path=nodes,
                edges=edges,
                road_names=road_names,
                estimated_time_min=round(cost_hours * 60.0, 2),
                distance_km=round(distance_km, 3),
                speed_cameras=cameras,
            )
        )

    return RouteResponse(routes=routes_out).model_dump()


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out
