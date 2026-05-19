"""
A* path planning engine.

Loads the road network from PostGIS-backed `traffic_node` / `traffic_edge`
tables once at startup, then exposes:
  - RoadGraph: in-memory forward + reverse adjacency with per-edge dynamic
    weight (set by a WeightProvider; weights are absolute travel-time hours).
    Reverse adjacency mirrors forward and is rebuilt with it; reverse-BFS uses
    it without re-scanning all edges per request.
  - SearchBox + compute_search_bbox: per-request frontier pruning.
  - astar(graph, start, end, search_box, weight_overrides): with bbox check
    and per-node `has_signal` stop-wait penalty.
  - find_top_k_routes: penalty-based top-K (3.0×).
  - plan_optimal_route: snap origin/dest with reachability fallback -> bbox
    -> top-K -> enrich with speed cameras + parking suggestions.
  - query_parking_near_destination: PostGIS LATERAL join for nearby parking.
"""

from __future__ import annotations

import heapq
import logging
import math
import os
from dataclasses import dataclass, field

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import SpeedCamera, TrafficEdge, TrafficNode

logger = logging.getLogger(__name__)

EARTH_RADIUS_KM = 6371.0
DEFAULT_TOP_K = 3
DEFAULT_PENALTY = 3.0

# Signal-stop penalty (hours) per traffic_node with has_signal=TRUE that A*
# routes through. Default 20 s ≈ 60-90 s cycle × ~50% green ratio.
SIGNAL_PENALTY_SECONDS = float(os.getenv("SIGNAL_PENALTY_SECONDS", "20"))
SIGNAL_PENALTY_HR = SIGNAL_PENALTY_SECONDS / 3600.0

# Search-bbox tuning.
DEFAULT_PADDING_RATIO = 0.3
DEFAULT_MIN_PADDING_KM = 2.0
RETRY_PADDING_RATIO = 0.6

# Snap-with-reachability tuning. The floor (100) keeps the post-contraction
# Taipei graph (~30-50k nodes) at a meaningful min-reach threshold; the
# `nodes // 1000` term scales up for larger maps. Tests monkeypatch the
# module-level constant to lower it for tiny synthetic graphs.
REACHABILITY_MIN_NODES_FLOOR = 100
REACHABILITY_MAX_HOPS = 1000
REACHABILITY_MAX_VISITED = 5000
SNAP_FALLBACK_CANDIDATES = 5
SNAP_TOP_K = 15


# ---------- Data structures ----------


@dataclass
class GraphNode:
    id: int
    latitude: float
    longitude: float
    has_signal: bool = False


@dataclass
class GraphEdge:
    id: int
    source_node_id: int
    target_node_id: int
    road_name: str
    length_km: float
    road_class: str | None = None
    max_speed_kmh: int | None = None
    oneway: bool = False
    # Cached endpoint coordinates so WeightProvider can compute midpoint
    # without another graph lookup.
    source_lat_lng: tuple[float, float] | None = None
    target_lat_lng: tuple[float, float] | None = None


@dataclass
class RouteResult:
    path: list[int]
    edges: list[int]
    road_names: list[str]
    estimated_time_min: float
    distance_km: float
    speed_cameras: list[dict] = field(default_factory=list)
    parking_suggestions: list[dict] = field(default_factory=list)


# ---------- Haversine ----------


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in km between two (lat, lng) points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dl = math.radians(lng2 - lng1)
    h = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


# ---------- SearchBox ----------


@dataclass(frozen=True)
class SearchBox:
    lat_min: float
    lat_max: float
    lng_min: float
    lng_max: float

    def contains(self, lat: float, lng: float) -> bool:
        return (
            self.lat_min <= lat <= self.lat_max
            and self.lng_min <= lng <= self.lng_max
        )


def compute_search_bbox(
    origin_lat: float,
    origin_lng: float,
    dest_lat: float,
    dest_lng: float,
    padding_ratio: float = DEFAULT_PADDING_RATIO,
    min_padding_km: float = DEFAULT_MIN_PADDING_KM,
) -> SearchBox:
    """Compute a padded bbox around origin+dest for A* frontier pruning.

    padding_km = max(direct_distance_km × padding_ratio, min_padding_km).
    Lat/lng extents are derived from km via:
      1° lat ≈ 111.32 km
      1° lng ≈ 111.32 × cos(lat) km
    """
    direct_km = haversine_km(origin_lat, origin_lng, dest_lat, dest_lng)
    pad_km = max(direct_km * padding_ratio, min_padding_km)

    lat_pad = pad_km / 111.32
    avg_lat = (origin_lat + dest_lat) / 2.0
    lng_pad = pad_km / (111.32 * max(math.cos(math.radians(avg_lat)), 1e-6))

    lat_min = min(origin_lat, dest_lat) - lat_pad
    lat_max = max(origin_lat, dest_lat) + lat_pad
    lng_min = min(origin_lng, dest_lng) - lng_pad
    lng_max = max(origin_lng, dest_lng) + lng_pad
    return SearchBox(lat_min, lat_max, lng_min, lng_max)


# ---------- RoadGraph ----------


class RoadGraph:
    """In-memory road network graph with dynamic edge weights.

    adjacency[node_id] = [(neighbor_id, edge_id, dynamic_weight_hours), ...]
    reverse_adjacency[node_id] = [(predecessor_id, edge_id, weight_hours), ...]

    Reverse adjacency is built once at load time so per-request incoming
    reachability checks don't have to re-scan every edge. Each non-oneway
    street now lives as TWO separate `traffic_edge` rows (one per direction)
    courtesy of the SQL rebuild, so each edge_id appears exactly once in
    forward adjacency (at its source) and once in reverse (at its target).

    Initial weights are 0 until WeightProvider.apply_to_graph() runs in
    the lifespan; A* before that point is undefined behaviour.
    """

    def __init__(self) -> None:
        self.nodes: dict[int, GraphNode] = {}
        self.edges: dict[int, GraphEdge] = {}
        self.adjacency: dict[int, list[tuple[int, int, float]]] = {}
        self.reverse_adjacency: dict[int, list[tuple[int, int, float]]] = {}
        self.max_speed_kmh: int = 1

    @classmethod
    async def from_db(cls, session: AsyncSession) -> RoadGraph:
        """Load all nodes + edges from DB and build graph."""
        graph = cls()

        node_rows = (await session.execute(select(TrafficNode))).scalars().all()
        for n in node_rows:
            graph.nodes[n.id] = GraphNode(
                id=n.id,
                latitude=n.latitude,
                longitude=n.longitude,
                has_signal=bool(n.has_signal),
            )

        edge_rows = (await session.execute(select(TrafficEdge))).scalars().all()
        for e in edge_rows:
            src = graph.nodes.get(e.source_node_id)
            tgt = graph.nodes.get(e.target_node_id)
            ge = GraphEdge(
                id=e.id,
                source_node_id=e.source_node_id,
                target_node_id=e.target_node_id,
                road_name=e.road_name or "",
                length_km=e.length_km,
                road_class=e.road_class,
                max_speed_kmh=e.max_speed_kmh,
                oneway=bool(e.oneway),
                source_lat_lng=(src.latitude, src.longitude) if src else None,
                target_lat_lng=(tgt.latitude, tgt.longitude) if tgt else None,
            )
            graph.edges[e.id] = ge
            # Forward adjacency only: the SQL rebuild emits a separate
            # traffic_edge row per direction for non-oneway streets, so we
            # MUST NOT mirror the entry here (doing so would double-count).
            graph.adjacency.setdefault(e.source_node_id, []).append(
                (e.target_node_id, e.id, 0.0)
            )
            if e.max_speed_kmh and e.max_speed_kmh > graph.max_speed_kmh:
                graph.max_speed_kmh = e.max_speed_kmh

        # Ensure every node has both adjacency entries (even if isolated).
        for nid in graph.nodes:
            graph.adjacency.setdefault(nid, [])
            graph.reverse_adjacency.setdefault(nid, [])

        # Build reverse adjacency from the just-built forward dict (O(E)).
        for u, neighbors in graph.adjacency.items():
            for v, eid, w in neighbors:
                graph.reverse_adjacency.setdefault(v, []).append((u, eid, w))

        logger.info(
            "RoadGraph loaded: %d nodes (%d signal), %d edges, max_speed=%d km/h",
            len(graph.nodes),
            sum(1 for n in graph.nodes.values() if n.has_signal),
            len(graph.edges),
            graph.max_speed_kmh,
        )
        return graph

    def update_weight(self, edge_id: int, new_weight: float) -> None:
        """Set the dynamic weight (in hours) of a single edge.

        Updates the single forward entry in `adjacency[source]` and the
        single reverse entry in `reverse_adjacency[target]`. With the SQL
        rebuild, each direction of a bidirectional street has its own
        edge_id, so each id appears exactly once in each dict.
        """
        edge = self.edges.get(edge_id)
        if edge is None:
            return
        w = max(float(new_weight), 1e-6)

        forward = self.adjacency.get(edge.source_node_id, [])
        for i, (nb, eid, _w) in enumerate(forward):
            if eid == edge_id and nb == edge.target_node_id:
                forward[i] = (nb, eid, w)
                break

        reverse = self.reverse_adjacency.get(edge.target_node_id, [])
        for i, (nb, eid, _w) in enumerate(reverse):
            if eid == edge_id and nb == edge.source_node_id:
                reverse[i] = (nb, eid, w)
                break

    def get_weight(self, edge_id: int) -> float:
        """Read current dynamic weight (hours) of an edge."""
        edge = self.edges.get(edge_id)
        if edge is None:
            return math.inf
        for nb, eid, w in self.adjacency.get(edge.source_node_id, []):
            if eid == edge_id and nb == edge.target_node_id:
                return w
        return math.inf

    def degree(self, node_id: int) -> int:
        """Undirected degree — distinct neighbours via outgoing OR incoming
        edges. Used by `snap_to_graph` to prefer real intersections; counting
        only outgoing would skip oneway-sink nodes that ARE legitimate
        intersections.
        """
        nbrs: set[int] = set()
        for nb, _eid, _w in self.adjacency.get(node_id, []):
            nbrs.add(nb)
        for nb, _eid, _w in self.reverse_adjacency.get(node_id, []):
            nbrs.add(nb)
        return len(nbrs)


# ---------- Snap to graph ----------


def snap_to_graph(
    lat: float,
    lng: float,
    graph: RoadGraph,
    k: int = SNAP_TOP_K,
    return_top_n: int = 1,
) -> int | list[int] | None:
    """Find the nearest K nodes, ranked by degree-desc then distance-asc.

    Return type is determined by `return_top_n`:
      - return_top_n == 1 (default) -> `int | None` (the best single node)
      - return_top_n >= 2           -> `list[int]` (up to N best nodes,
        truncated when graph has fewer nodes than requested; empty list
        when graph is empty)
    """
    if not graph.nodes:
        return [] if return_top_n >= 2 else None

    distances = [
        (haversine_km(lat, lng, n.latitude, n.longitude), n.id)
        for n in graph.nodes.values()
    ]
    distances.sort(key=lambda x: x[0])
    top_k = distances[: max(k, 1)]

    ranked = sorted(top_k, key=lambda x: (-graph.degree(x[1]), x[0]))

    if return_top_n >= 2:
        return [nid for _, nid in ranked[:return_top_n]]
    return ranked[0][1]


# ---------- Reachability helpers ----------


def _has_outgoing_reach(graph: RoadGraph, node_id: int, min_reach: int) -> bool:
    """Limited forward BFS — is there a path out of `node_id` to >= `min_reach`
    other nodes? Capped at REACHABILITY_MAX_HOPS hops and REACHABILITY_MAX_VISITED
    nodes; returns True as soon as the threshold is crossed.
    """
    if node_id not in graph.adjacency:
        return False
    visited: set[int] = {node_id}
    frontier: list[int] = [node_id]
    hops = 0
    while frontier:
        if hops >= REACHABILITY_MAX_HOPS or len(visited) >= REACHABILITY_MAX_VISITED:
            break
        next_frontier: list[int] = []
        for u in frontier:
            for nb, _eid, _w in graph.adjacency.get(u, []):
                if nb in visited:
                    continue
                visited.add(nb)
                if len(visited) - 1 >= min_reach:
                    return True
                next_frontier.append(nb)
        frontier = next_frontier
        hops += 1
    return len(visited) - 1 >= min_reach


def _has_incoming_reach(graph: RoadGraph, node_id: int, min_reach: int) -> bool:
    """Mirror of `_has_outgoing_reach` over `reverse_adjacency` — is there a
    path INTO `node_id` from >= `min_reach` other nodes?"""
    if node_id not in graph.reverse_adjacency:
        return False
    visited: set[int] = {node_id}
    frontier: list[int] = [node_id]
    hops = 0
    while frontier:
        if hops >= REACHABILITY_MAX_HOPS or len(visited) >= REACHABILITY_MAX_VISITED:
            break
        next_frontier: list[int] = []
        for u in frontier:
            for nb, _eid, _w in graph.reverse_adjacency.get(u, []):
                if nb in visited:
                    continue
                visited.add(nb)
                if len(visited) - 1 >= min_reach:
                    return True
                next_frontier.append(nb)
        frontier = next_frontier
        hops += 1
    return len(visited) - 1 >= min_reach


def _pick_reachable_candidate(
    graph: RoadGraph,
    candidates: list[int],
    min_reach: int,
    direction: str,
    coord: tuple[float, float],
) -> int:
    """Walk candidates in order, return the first one whose reach >= min_reach.

    Falls back to candidates[0] if none pass, logging a warning (degraded
    behaviour — A* still gets to try, BFS is a cheap heuristic that can
    misjudge edge cases).
    """
    probe = _has_outgoing_reach if direction == "outgoing" else _has_incoming_reach
    for c in candidates:
        if probe(graph, c, min_reach):
            return c
    degrees = [(c, graph.degree(c)) for c in candidates]
    logger.warning(
        "snap-with-reachability: all %d candidates failed %s-reach for "
        "(%.5f, %.5f); falling back to first. candidates=%s",
        len(candidates), direction, coord[0], coord[1], degrees,
    )
    return candidates[0]


# ---------- A* ----------


def astar(
    graph: RoadGraph,
    start_id: int,
    end_id: int,
    weight_overrides: dict[int, float] | None = None,
    search_box: SearchBox | None = None,
) -> tuple[list[int], list[int], float] | None:
    """A* shortest path with optional bbox pruning + signal-stop penalty.

    Heuristic: haversine_km(current, end) / max_speed_kmh — admissible
    even with the signal penalty (heuristic still underestimates true cost).

    Returns (node_path, edge_path, total_cost_hours) or None if unreachable.
    `weight_overrides[edge_id]` lets callers apply temporary penalties without
    mutating graph (used by find_top_k_routes).
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

    open_heap: list[tuple[float, int, int]] = []
    counter = 0
    heapq.heappush(open_heap, (h(start_id), counter, start_id))

    came_from: dict[int, tuple[int, int]] = {}
    g_score: dict[int, float] = {start_id: 0.0}

    while open_heap:
        _, _, current = heapq.heappop(open_heap)
        if current == end_id:
            return _reconstruct(came_from, start_id, end_id, g_score[end_id])

        current_g = g_score[current]
        for nb, eid, base_w in graph.adjacency.get(current, []):
            neighbor = graph.nodes.get(nb)
            if neighbor is None:
                continue
            # 9.3 — bbox check first to prune frontier.
            if search_box is not None and not search_box.contains(neighbor.latitude, neighbor.longitude):
                continue
            # 9.3b — signal penalty for has_signal nodes (skip end node).
            edge_weight = weight_overrides.get(eid, base_w) if weight_overrides else base_w
            penalty = SIGNAL_PENALTY_HR if (neighbor.has_signal and nb != end_id) else 0.0
            tentative = current_g + edge_weight + penalty
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
    search_box: SearchBox | None = None,
) -> list[tuple[list[int], list[int], float]]:
    """Penalty-based top-K. After each A* run, multiply used edges' weight by
    `penalty` and rerun. Final cost is recomputed with the live (unmodified)
    graph weights. Duplicate paths are filtered.
    """
    overrides: dict[int, float] = {}
    results: list[tuple[list[int], list[int], float]] = []
    seen_edge_sets: set[tuple[int, ...]] = set()

    for _ in range(max(k, 1)):
        result = astar(graph, start_id, end_id, weight_overrides=overrides, search_box=search_box)
        if result is None:
            break
        nodes, edges, _penalized_cost = result
        edge_key = tuple(edges)
        if edge_key in seen_edge_sets:
            break
        seen_edge_sets.add(edge_key)

        # Real cost from live graph weights (no overrides, no signal penalty).
        real_cost = sum(graph.get_weight(eid) for eid in edges)
        results.append((nodes, edges, real_cost))

        for eid in edges:
            base_w = graph.get_weight(eid)
            overrides[eid] = overrides.get(eid, base_w) * penalty

    results.sort(key=lambda r: r[2])
    return results


# ---------- Parking lookup ----------


_PARKING_NEAR_QUERY = text(
    """
    SELECT
        pl.id,
        pl.name,
        pl.address,
        pl.latitude,
        pl.longitude,
        pa.available_car,
        ST_Distance(pl.geom::geography, ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography) AS distance_m
    FROM parking_lot pl
    CROSS JOIN LATERAL (
        SELECT available_car
        FROM parking_availability
        WHERE lot_id = pl.id
        ORDER BY ts DESC
        LIMIT 1
    ) pa
    WHERE ST_DWithin(
        pl.geom::geography,
        ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography,
        :radius_m
    )
      AND pa.available_car IS NOT NULL
      AND pa.available_car >= :min_avail
    ORDER BY distance_m ASC
    LIMIT :top_n
    """
)


async def query_parking_near_destination(
    session: AsyncSession,
    lat: float,
    lng: float,
    radius_km: float = 1.0,
    top: int = 5,
    min_available: int = 10,
) -> list[dict]:
    """Return the nearest parking lots to (lat, lng) with at least
    `min_available` empty car spaces, ordered by distance ascending.
    """
    rows = (
        await session.execute(
            _PARKING_NEAR_QUERY,
            {
                "lat": lat,
                "lng": lng,
                "radius_m": radius_km * 1000,
                "min_avail": min_available,
                "top_n": top,
            },
        )
    ).all()
    return [
        {
            "id": r.id,
            "name": r.name,
            "address": r.address,
            "latitude": r.latitude,
            "longitude": r.longitude,
            "available_car": int(r.available_car),
            "distance_m": round(float(r.distance_m), 1),
        }
        for r in rows
    ]


# ---------- Entry point ----------


async def plan_optimal_route(
    session: AsyncSession,
    graph: RoadGraph,
    weight_provider,
    origin_lat: float,
    origin_lng: float,
    dest_lat: float,
    dest_lng: float,
    user_id: str | None = None,  # Phase-2 personalization hook
    k: int = DEFAULT_TOP_K,
) -> dict:
    """Plan top-K routes from origin to destination.

    Returns a JSON-serialisable dict matching `RouteResponse` schema:
    `{"routes": [RouteItem, ...], "error": str | None}`.

    `weight_provider` is reserved for future per-request reweighting (e.g.
    avoid-tolls overrides). Phase 1 trusts the latest WeightProvider state
    already applied to the graph.
    `user_id` is reserved for PersonalizedWeightProvider; Phase 1 ignores it.
    """
    from src.mcp_servers.routing_tool import RouteItem, RouteResponse

    _ = weight_provider, user_id  # accepted, unused in phase 1

    if not graph.nodes:
        return RouteResponse(routes=[], error="road network not loaded").model_dump()

    bbox = compute_search_bbox(origin_lat, origin_lng, dest_lat, dest_lng)

    # Snap-with-reachability: pull top 5 candidates per endpoint and pick the
    # first one that actually reaches into / out of the main graph.
    reach_min = max(REACHABILITY_MIN_NODES_FLOOR, len(graph.nodes) // 1000)
    skip_reach = len(graph.nodes) < reach_min

    origin_candidates = snap_to_graph(
        origin_lat, origin_lng, graph, return_top_n=SNAP_FALLBACK_CANDIDATES,
    )
    dest_candidates = snap_to_graph(
        dest_lat, dest_lng, graph, return_top_n=SNAP_FALLBACK_CANDIDATES,
    )
    if not origin_candidates or not dest_candidates:
        return RouteResponse(
            routes=[],
            error="could not snap origin/destination to graph",
        ).model_dump()

    if skip_reach:
        start_id = origin_candidates[0]
        end_id = dest_candidates[0]
    else:
        start_id = _pick_reachable_candidate(
            graph, origin_candidates, reach_min,
            direction="outgoing", coord=(origin_lat, origin_lng),
        )
        end_id = _pick_reachable_candidate(
            graph, dest_candidates, reach_min,
            direction="incoming", coord=(dest_lat, dest_lng),
        )

    raw_routes = find_top_k_routes(graph, start_id, end_id, k=k, search_box=bbox)
    if not raw_routes:
        # Retry once with a wider bbox.
        bbox = compute_search_bbox(
            origin_lat, origin_lng, dest_lat, dest_lng,
            padding_ratio=RETRY_PADDING_RATIO,
        )
        raw_routes = find_top_k_routes(graph, start_id, end_id, k=k, search_box=bbox)

    if not raw_routes:
        return RouteResponse(
            routes=[],
            error="no path found between origin and destination",
        ).model_dump()

    # Aggregate edges -> one DB query for cameras.
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

    # Parking suggestions only attach to the best route.
    try:
        parking_suggestions = await query_parking_near_destination(
            session, dest_lat, dest_lng,
        )
    except Exception as exc:
        logger.warning("query_parking_near_destination failed: %s", exc)
        parking_suggestions = []

    routes_out: list[RouteItem] = []
    for idx, (nodes, edges, cost_hours) in enumerate(raw_routes):
        edge_objs = [graph.edges[eid] for eid in edges]
        road_names = _dedupe_preserve_order([e.road_name for e in edge_objs if e.road_name])
        distance_km = sum(e.length_km for e in edge_objs)
        cameras = [cam for eid in edges for cam in cameras_by_edge.get(eid, [])]
        coordinates: list[list[float]] = []
        for nid in nodes:
            node = graph.nodes.get(nid)
            if node is None:
                logger.warning("path contains node %s missing from graph; skipping in coordinates", nid)
                continue
            coordinates.append([node.latitude, node.longitude])
        routes_out.append(
            RouteItem(
                path=nodes,
                edges=edges,
                coordinates=coordinates,
                road_names=road_names,
                estimated_time_min=round(cost_hours * 60.0, 2),
                distance_km=round(distance_km, 3),
                speed_cameras=cameras,
                parking_suggestions=parking_suggestions if idx == 0 else [],
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
