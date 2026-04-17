"""A* routing engine unit tests."""

from __future__ import annotations

from src.agents.routing import (
    GraphEdge,
    GraphNode,
    RoadGraph,
    astar,
    find_top_k_routes,
    haversine_km,
    snap_to_graph,
)
from src.agents.traffic import _congestion_factor


def _build_graph(nodes: list[tuple[int, float, float]], edges: list[tuple[int, int, int, float, int]]) -> RoadGraph:
    """Tiny helper to construct a RoadGraph from tuples (no DB).

    nodes: [(id, lat, lng)]
    edges: [(id, src, tgt, length_km, speed_kmh)]
    """
    graph = RoadGraph()
    for nid, lat, lng in nodes:
        graph.nodes[nid] = GraphNode(id=nid, latitude=lat, longitude=lng)
        graph.adjacency[nid] = []

    for eid, src, tgt, length_km, speed in edges:
        base_w = length_km / speed
        graph.edges[eid] = GraphEdge(
            id=eid,
            source_node_id=src,
            target_node_id=tgt,
            road_name=f"E{eid}",
            length_km=length_km,
            speed_limit_kmh=speed,
            base_weight=base_w,
        )
        graph.adjacency[src].append((tgt, eid, base_w))
        graph.adjacency[tgt].append((src, eid, base_w))
        if speed > graph.max_speed_kmh:
            graph.max_speed_kmh = speed
    return graph


# ---------- 6.1 A* correctness ----------


class TestAStarShortestPath:
    def test_direct_edge(self):
        """Two nodes, one edge — A* must return the trivial path."""
        g = _build_graph(
            nodes=[(1, 22.62, 120.30), (2, 22.63, 120.31)],
            edges=[(10, 1, 2, 1.5, 50)],
        )
        result = astar(g, 1, 2)
        assert result is not None
        nodes, edges, cost = result
        assert nodes == [1, 2]
        assert edges == [10]
        assert abs(cost - 1.5 / 50) < 1e-9

    def test_picks_shorter_of_two_paths(self):
        """Triangle with two-hop shortcut and a single long edge — A* must pick shorter."""
        # 1 --long(eid 10)-- 3
        # 1 --short(eid 20)-- 2 --short(eid 21)-- 3
        g = _build_graph(
            nodes=[(1, 22.60, 120.30), (2, 22.60, 120.31), (3, 22.60, 120.32)],
            edges=[
                (10, 1, 3, 5.0, 50),   # cost 0.10
                (20, 1, 2, 1.0, 50),   # cost 0.02
                (21, 2, 3, 1.0, 50),   # cost 0.02
            ],
        )
        nodes, edges, cost = astar(g, 1, 3)
        assert nodes == [1, 2, 3]
        assert edges == [20, 21]
        assert abs(cost - 0.04) < 1e-9

    def test_same_start_and_end(self):
        g = _build_graph(nodes=[(1, 22.6, 120.3)], edges=[])
        nodes, edges, cost = astar(g, 1, 1)
        assert nodes == [1]
        assert edges == []
        assert cost == 0.0

    def test_unreachable_returns_none(self):
        """Disconnected components — A* must return None."""
        g = _build_graph(
            nodes=[(1, 22.60, 120.30), (2, 22.61, 120.31), (3, 22.70, 120.40), (4, 22.71, 120.41)],
            edges=[(10, 1, 2, 1.0, 50), (20, 3, 4, 1.0, 50)],
        )
        assert astar(g, 1, 3) is None


# ---------- 6.2 snap_to_graph ----------


class TestSnapToGraph:
    def test_prefers_high_degree_node(self):
        """Among nearby nodes, highest degree wins (intersection over dead end)."""
        # Dead-end node (1) is *closer* to query point than the intersection (2),
        # but node 2 has degree 3, so it must win.
        g = _build_graph(
            nodes=[
                (1, 22.6200, 120.3000),   # dead end
                (2, 22.6202, 120.3002),   # intersection (3 neighbours)
                (3, 22.6210, 120.3010),
                (4, 22.6210, 120.2990),
                (5, 22.6190, 120.3010),
            ],
            edges=[
                (10, 1, 2, 0.5, 50),   # 1 has degree 1
                (20, 2, 3, 0.5, 50),   # 2 gets +1
                (21, 2, 4, 0.5, 50),   # 2 gets +1
                (22, 2, 5, 0.5, 50),   # 2 gets +1 => degree 4
            ],
        )
        picked = snap_to_graph(22.6200, 120.3000, g, k=3)
        assert picked == 2

    def test_empty_graph_returns_none(self):
        g = RoadGraph()
        assert snap_to_graph(22.62, 120.30, g) is None


# ---------- 6.3 Top-K ----------


class TestTopKRoutes:
    def test_returns_multiple_distinct_paths(self):
        # 1 -> 2 -> 4 (direct, cost 0.04)
        # 1 -> 3 -> 4 (alternate, cost 0.06)
        g = _build_graph(
            nodes=[
                (1, 22.60, 120.30),
                (2, 22.60, 120.31),
                (3, 22.61, 120.30),
                (4, 22.60, 120.32),
            ],
            edges=[
                (10, 1, 2, 1.0, 50),   # 0.02
                (11, 2, 4, 1.0, 50),   # 0.02   ⇒ path A total 0.04
                (20, 1, 3, 1.5, 50),   # 0.03
                (21, 3, 4, 1.5, 50),   # 0.03   ⇒ path B total 0.06
            ],
        )
        routes = find_top_k_routes(g, 1, 4, k=3)
        assert len(routes) >= 2
        # Costs must be ascending with real (un-penalised) weights.
        costs = [r[2] for r in routes]
        assert costs == sorted(costs)
        assert abs(costs[0] - 0.04) < 1e-9
        assert abs(costs[1] - 0.06) < 1e-9
        # Edge sets must be distinct.
        edge_sets = [tuple(r[1]) for r in routes]
        assert len(set(edge_sets)) == len(edge_sets)

    def test_k_larger_than_available_paths(self):
        """Only one path exists — we must return 1, not pad."""
        g = _build_graph(
            nodes=[(1, 22.60, 120.30), (2, 22.60, 120.31)],
            edges=[(10, 1, 2, 1.0, 50)],
        )
        routes = find_top_k_routes(g, 1, 2, k=5)
        assert len(routes) == 1


# ---------- 6.4 Congestion factor ----------


class TestCongestionFactor:
    def test_normal_case(self):
        # speed_limit 50, actual 25 -> factor 2.0
        assert abs(_congestion_factor(50, 25) - 2.0) < 1e-9

    def test_free_flow_returns_one(self):
        # actual == speed_limit -> factor 1.0
        assert abs(_congestion_factor(50, 50) - 1.0) < 1e-9

    def test_zero_speed_returns_max(self):
        assert _congestion_factor(50, 0) == 10.0

    def test_negative_speed_returns_max(self):
        assert _congestion_factor(50, -5) == 10.0

    def test_no_data_returns_one(self):
        # None = no live data -> free flow fallback
        assert _congestion_factor(50, None) == 1.0

    def test_factor_capped_at_max(self):
        # 50 / 1 = 50, must be capped to 10.0
        assert _congestion_factor(50, 1) == 10.0


# ---------- Haversine sanity ----------


def test_haversine_sanity():
    # Two points ~1.1 km apart
    d = haversine_km(22.620, 120.300, 22.625, 120.305)
    assert 0.6 < d < 1.0
