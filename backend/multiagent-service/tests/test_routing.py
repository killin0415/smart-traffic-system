"""Pure unit tests for `src/agents/routing.py`.

No real DB. We hand-build small RoadGraph instances and exercise:
  - SearchBox.contains
  - compute_search_bbox padding semantics
  - astar with bbox pruning, signal penalty (mid-path / end-node / start-node)
  - update_weight + get_weight contract
  - find_top_k_routes deduplication + search_box propagation
  - plan_optimal_route response shape + retry-with-wider-bbox
"""

from __future__ import annotations

import math
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agents import routing
from src.agents.routing import (
    DEFAULT_PADDING_RATIO,
    RETRY_PADDING_RATIO,
    SIGNAL_PENALTY_HR,
    GraphEdge,
    GraphNode,
    RoadGraph,
    SearchBox,
    astar,
    compute_search_bbox,
    find_top_k_routes,
    plan_optimal_route,
)


# ---------- helpers ----------


def _build_graph(nodes, edges, weights):
    """Construct a RoadGraph from raw GraphNode / GraphEdge objects.

    Mirrors the structure produced by `RoadGraph.from_db` but bypasses DB.
    Adds bidirectional adjacency (oneway=False assumed unless edge.oneway=True).
    """
    g = RoadGraph()
    for n in nodes:
        g.nodes[n.id] = n
        g.adjacency.setdefault(n.id, [])
    for e in edges:
        g.edges[e.id] = e
        g.adjacency.setdefault(e.source_node_id, []).append(
            (e.target_node_id, e.id, 0.0)
        )
        if not e.oneway:
            g.adjacency.setdefault(e.target_node_id, []).append(
                (e.source_node_id, e.id, 0.0)
            )
    g.max_speed_kmh = 50
    for eid, w in weights.items():
        g.update_weight(eid, w)
    return g


def _edge(eid, src, tgt, length_km, road_name="Rd", src_ll=None, tgt_ll=None):
    return GraphEdge(
        id=eid,
        source_node_id=src,
        target_node_id=tgt,
        road_name=road_name,
        length_km=length_km,
        max_speed_kmh=50,
        oneway=False,
        source_lat_lng=src_ll,
        target_lat_lng=tgt_ll,
    )


# ---------- (a) SearchBox.contains boundary ----------


class TestSearchBoxContains:
    def test_corner_points_are_inside(self):
        box = SearchBox(lat_min=25.01234, lat_max=25.05678,
                        lng_min=121.40001, lng_max=121.50002)
        assert box.contains(25.01234, 121.40001) is True   # bottom-left
        assert box.contains(25.05678, 121.50002) is True   # top-right
        assert box.contains(25.01234, 121.50002) is True
        assert box.contains(25.05678, 121.40001) is True

    def test_just_outside_returns_false(self):
        box = SearchBox(lat_min=25.01234, lat_max=25.05678,
                        lng_min=121.40001, lng_max=121.50002)
        assert box.contains(25.01233, 121.45) is False     # below lat_min
        assert box.contains(25.05679, 121.45) is False     # above lat_max
        assert box.contains(25.04, 121.40000) is False     # below lng_min
        assert box.contains(25.04, 121.50003) is False     # above lng_max

    def test_centre_point_inside(self):
        box = SearchBox(0.0, 1.0, 0.0, 1.0)
        assert box.contains(0.5, 0.5) is True


# ---------- (b) compute_search_bbox ----------


class TestComputeSearchBbox:
    def test_short_distance_uses_min_padding(self):
        # Origin / destination very close together (< 6.7 km).
        # min_padding_km=2.0 should dominate over direct_km * 0.3.
        bbox = compute_search_bbox(
            origin_lat=25.040, origin_lng=121.510,
            dest_lat=25.041, dest_lng=121.511,
        )
        expected_lat_pad = 2.0 / 111.32
        # lat_max - max(o,d).lat == lat_pad   AND   min(o,d).lat - lat_min == lat_pad
        assert math.isclose(bbox.lat_max - 25.041, expected_lat_pad, rel_tol=1e-6)
        assert math.isclose(25.040 - bbox.lat_min, expected_lat_pad, rel_tol=1e-6)

    def test_long_distance_uses_padding_ratio(self):
        # ~50 km separation -> pad_km ≈ 50 * 0.3 = 15 km.  lat_pad = pad_km / 111.32.
        # Two points roughly 50 km apart along the same meridian.  We compare
        # the produced lat-padding against (computed direct_km * 0.3) / 111.32
        # to dodge any meter-level haversine drift.
        from src.agents.routing import haversine_km
        o_lat, o_lng = 25.000, 121.500
        d_lat = o_lat + 50.0 / 111.32
        d_lng = o_lng
        bbox = compute_search_bbox(o_lat, o_lng, d_lat, d_lng)

        direct_km = haversine_km(o_lat, o_lng, d_lat, d_lng)
        expected_pad_km = direct_km * DEFAULT_PADDING_RATIO   # > min_padding_km
        assert expected_pad_km > 2.0  # sanity: padding ratio dominates
        expected_lat_pad = expected_pad_km / 111.32
        assert math.isclose(bbox.lat_max - d_lat, expected_lat_pad, rel_tol=1e-9)
        assert math.isclose(o_lat - bbox.lat_min, expected_lat_pad, rel_tol=1e-9)

    def test_origin_equal_dest_still_has_padding(self):
        bbox = compute_search_bbox(25.040, 121.510, 25.040, 121.510)
        assert bbox.contains(25.040, 121.510) is True
        # Has the min_padding_km extent on each side.
        expected_lat_pad = 2.0 / 111.32
        assert math.isclose(bbox.lat_max - 25.040, expected_lat_pad, rel_tol=1e-6)
        assert math.isclose(25.040 - bbox.lat_min, expected_lat_pad, rel_tol=1e-6)


# ---------- (c) A* with bbox pruning ----------


class TestAstarBboxPruning:
    def test_far_node_excluded_by_search_box(self):
        n1 = GraphNode(id=1, latitude=25.040, longitude=121.510)
        n2 = GraphNode(id=2, latitude=25.040, longitude=121.515)
        n3 = GraphNode(id=3, latitude=25.060, longitude=121.530)  # FAR
        n4 = GraphNode(id=4, latitude=25.040, longitude=121.520)
        n5 = GraphNode(id=5, latitude=25.040, longitude=121.525)
        edges = [
            _edge(101, 1, 2, length_km=1.0),
            _edge(102, 2, 3, length_km=5.0),   # detour to FAR node
            _edge(103, 3, 5, length_km=5.0),
            _edge(104, 2, 4, length_km=0.5),
            _edge(105, 4, 5, length_km=0.5),
        ]
        # Use length_km / 60 km/h (in hours) as weight — proportional & realistic.
        weights = {101: 1.0 / 60, 102: 5.0 / 60, 103: 5.0 / 60,
                   104: 0.5 / 60, 105: 0.5 / 60}
        g = _build_graph([n1, n2, n3, n4, n5], edges, weights)

        # Box that excludes n3 explicitly (lat 25.060 outside).
        box = SearchBox(lat_min=25.039, lat_max=25.041,
                        lng_min=121.509, lng_max=121.526)
        result = astar(g, 1, 5, search_box=box)
        assert result is not None
        path_nodes, path_edges, _cost = result
        assert 3 not in path_nodes

    def test_no_bbox_no_pruning_finds_some_path(self):
        n1 = GraphNode(id=1, latitude=25.040, longitude=121.510)
        n2 = GraphNode(id=2, latitude=25.040, longitude=121.515)
        edges = [_edge(101, 1, 2, length_km=1.0)]
        g = _build_graph([n1, n2], edges, {101: 1.0 / 60})
        result = astar(g, 1, 2)
        assert result is not None
        nodes, _, _ = result
        assert nodes == [1, 2]


# ---------- (d) plan_optimal_route retry with wider bbox ----------


@pytest.mark.asyncio
async def test_plan_optimal_route_retries_with_wider_bbox():
    """First call (default 0.3 padding) returns []; second call (0.6) returns a route."""
    n1 = GraphNode(id=1, latitude=25.040, longitude=121.510)
    n2 = GraphNode(id=2, latitude=25.040, longitude=121.520)
    edges = [_edge(101, 1, 2, length_km=1.0, road_name="Test Rd")]
    g = _build_graph([n1, n2], edges, {101: 1.0 / 60})

    # snap_to_graph will pick whichever node is closest; both are in graph.
    fake_route = ([1, 2], [101], 1.0 / 60)

    call_count = {"n": 0}

    def fake_top_k(graph, start, end, k=3, search_box=None, **kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return []                  # too-tight bbox simulated
        return [fake_route]

    # Mock the async session — only used for cameras + parking queries.
    fake_session = MagicMock()
    cam_result = MagicMock()
    cam_result.scalars.return_value.all.return_value = []   # no cameras
    fake_session.execute = AsyncMock(return_value=cam_result)

    with patch.object(routing, "find_top_k_routes", side_effect=fake_top_k), \
         patch.object(routing, "query_parking_near_destination",
                      new=AsyncMock(return_value=[])):
        out = await plan_optimal_route(
            session=fake_session,
            graph=g,
            weight_provider=None,
            origin_lat=25.040, origin_lng=121.510,
            dest_lat=25.040, dest_lng=121.520,
            k=3,
        )

    assert call_count["n"] == 2
    assert out["error"] is None
    assert len(out["routes"]) == 1
    assert out["routes"][0]["edges"] == [101]


# ---------- (e) plan_optimal_route response shape ----------


@pytest.mark.asyncio
async def test_plan_optimal_route_response_shape_success():
    n1 = GraphNode(id=1, latitude=25.040, longitude=121.510)
    n2 = GraphNode(id=2, latitude=25.040, longitude=121.520)
    edges = [_edge(101, 1, 2, length_km=1.0, road_name="Some Rd")]
    g = _build_graph([n1, n2], edges, {101: 1.0 / 60})

    cam_result = MagicMock()
    cam_result.scalars.return_value.all.return_value = []
    fake_session = MagicMock()
    fake_session.execute = AsyncMock(return_value=cam_result)

    parking_payload = [{"id": 1, "name": "Lot A", "address": "Addr",
                        "latitude": 25.040, "longitude": 121.5205,
                        "available_car": 30, "distance_m": 50.0}]

    with patch.object(routing, "query_parking_near_destination",
                      new=AsyncMock(return_value=parking_payload)):
        out = await plan_optimal_route(
            session=fake_session, graph=g, weight_provider=None,
            origin_lat=25.040, origin_lng=121.510,
            dest_lat=25.040, dest_lng=121.520,
            k=3,
        )

    assert set(out.keys()) == {"routes", "error"}
    assert out["error"] is None
    assert len(out["routes"]) >= 1
    r0 = out["routes"][0]
    for key in ("path", "edges", "road_names",
                "estimated_time_min", "distance_km",
                "speed_cameras", "parking_suggestions"):
        assert key in r0
    assert isinstance(r0["speed_cameras"], list)
    assert isinstance(r0["parking_suggestions"], list)
    # Best route gets parking attached.
    assert r0["parking_suggestions"] == parking_payload


@pytest.mark.asyncio
async def test_plan_optimal_route_unreachable_returns_error():
    """Two disconnected nodes -> no path -> error populated, routes=[]."""
    n1 = GraphNode(id=1, latitude=25.040, longitude=121.510)
    n2 = GraphNode(id=2, latitude=25.040, longitude=121.520)
    g = _build_graph([n1, n2], [], {})  # no edges

    fake_session = MagicMock()
    cam_result = MagicMock()
    cam_result.scalars.return_value.all.return_value = []
    fake_session.execute = AsyncMock(return_value=cam_result)

    with patch.object(routing, "query_parking_near_destination",
                      new=AsyncMock(return_value=[])):
        out = await plan_optimal_route(
            session=fake_session, graph=g, weight_provider=None,
            origin_lat=25.040, origin_lng=121.510,
            dest_lat=25.040, dest_lng=121.520,
            k=3,
        )

    assert out["routes"] == []
    assert out["error"] is not None
    assert "no path" in out["error"].lower()


@pytest.mark.asyncio
async def test_plan_optimal_route_empty_graph_returns_error():
    g = RoadGraph()  # nodes empty
    fake_session = MagicMock()
    out = await plan_optimal_route(
        session=fake_session, graph=g, weight_provider=None,
        origin_lat=25.040, origin_lng=121.510,
        dest_lat=25.040, dest_lng=121.520,
    )
    assert out["routes"] == []
    assert out["error"] == "road network not loaded"


# ---------- (f), (g), (h) Signal penalty ----------


def _build_three_node_chain(end_has_signal=False, mid_has_signal=False,
                            start_has_signal=False):
    """n1 --e101--> n2 --e102--> n3, with weights 1/60 each."""
    n1 = GraphNode(id=1, latitude=25.040, longitude=121.510, has_signal=start_has_signal)
    n2 = GraphNode(id=2, latitude=25.040, longitude=121.515, has_signal=mid_has_signal)
    n3 = GraphNode(id=3, latitude=25.040, longitude=121.520, has_signal=end_has_signal)
    edges = [
        _edge(101, 1, 2, length_km=1.0),
        _edge(102, 2, 3, length_km=1.0),
    ]
    weights = {101: 1.0 / 60, 102: 1.0 / 60}
    return _build_graph([n1, n2, n3], edges, weights)


class TestSignalPenalty:
    def test_signal_at_intermediate_node_adds_exactly_signal_penalty_hr(self):
        g_off = _build_three_node_chain(mid_has_signal=False)
        g_on = _build_three_node_chain(mid_has_signal=True)
        r_off = astar(g_off, 1, 3)
        r_on = astar(g_on, 1, 3)
        assert r_off is not None and r_on is not None
        cost_off = r_off[2]
        cost_on = r_on[2]
        assert math.isclose(cost_on - cost_off, SIGNAL_PENALTY_HR, rel_tol=1e-9)

    def test_signal_at_end_node_does_not_add_penalty(self):
        """Per A* loop: penalty skipped when neighbour == end_id."""
        g_off = _build_three_node_chain(end_has_signal=False)
        g_on = _build_three_node_chain(end_has_signal=True)
        r_off = astar(g_off, 1, 3)
        r_on = astar(g_on, 1, 3)
        assert r_off is not None and r_on is not None
        assert math.isclose(r_off[2], r_on[2], rel_tol=1e-9)

    def test_signal_at_start_node_does_not_add_penalty(self):
        """g_score[start]=0 by construction; nothing 'enters' start so no penalty fires."""
        n1 = GraphNode(id=1, latitude=25.040, longitude=121.510, has_signal=True)
        n2 = GraphNode(id=2, latitude=25.040, longitude=121.515, has_signal=False)
        edges = [_edge(101, 1, 2, length_km=1.0)]
        g = _build_graph([n1, n2], edges, {101: 1.0 / 60})
        result = astar(g, 1, 2)
        assert result is not None
        # Cost equals the single edge weight, no penalty.
        assert math.isclose(result[2], 1.0 / 60, rel_tol=1e-9)


# ---------- (i) update_weight signature ----------


class TestUpdateWeight:
    def test_set_then_read(self):
        n1 = GraphNode(id=1, latitude=0.0, longitude=0.0)
        n2 = GraphNode(id=2, latitude=0.0, longitude=0.001)
        e = _edge(101, 1, 2, length_km=0.1)
        g = _build_graph([n1, n2], [e], {})
        g.update_weight(101, 0.123)
        assert math.isclose(g.get_weight(101), 0.123, rel_tol=1e-9)

    def test_override_replaces_prior_value(self):
        n1 = GraphNode(id=1, latitude=0.0, longitude=0.0)
        n2 = GraphNode(id=2, latitude=0.0, longitude=0.001)
        e = _edge(101, 1, 2, length_km=0.1)
        g = _build_graph([n1, n2], [e], {})
        g.update_weight(101, 0.5)
        g.update_weight(101, 0.25)
        assert math.isclose(g.get_weight(101), 0.25, rel_tol=1e-9)

    def test_unknown_edge_id_is_noop(self):
        n1 = GraphNode(id=1, latitude=0.0, longitude=0.0)
        g = _build_graph([n1], [], {})
        # Should not raise, and get_weight returns inf for unknown edge.
        g.update_weight(9999, 1.0)
        assert g.get_weight(9999) == math.inf


# ---------- (j) find_top_k_routes deduplication ----------


def test_find_top_k_routes_dedupes_when_only_one_path_exists():
    n1 = GraphNode(id=1, latitude=25.040, longitude=121.510)
    n2 = GraphNode(id=2, latitude=25.040, longitude=121.515)
    n3 = GraphNode(id=3, latitude=25.040, longitude=121.520)
    edges = [
        _edge(101, 1, 2, length_km=1.0),
        _edge(102, 2, 3, length_km=1.0),
    ]
    weights = {101: 1.0 / 60, 102: 1.0 / 60}
    g = _build_graph([n1, n2, n3], edges, weights)
    routes = find_top_k_routes(g, 1, 3, k=3)
    assert len(routes) == 1


# ---------- (k) find_top_k_routes propagates search_box to astar ----------


def test_find_top_k_routes_passes_search_box_each_iteration():
    n1 = GraphNode(id=1, latitude=25.040, longitude=121.510)
    n2 = GraphNode(id=2, latitude=25.040, longitude=121.515)
    n3 = GraphNode(id=3, latitude=25.040, longitude=121.520)
    n4 = GraphNode(id=4, latitude=25.040, longitude=121.5175)  # alt mid
    edges = [
        _edge(101, 1, 2, length_km=1.0),
        _edge(102, 2, 3, length_km=1.0),
        _edge(103, 1, 4, length_km=1.1),
        _edge(104, 4, 3, length_km=1.1),
    ]
    weights = {101: 1.0 / 60, 102: 1.0 / 60, 103: 1.1 / 60, 104: 1.1 / 60}
    g = _build_graph([n1, n2, n3, n4], edges, weights)
    box = SearchBox(lat_min=25.039, lat_max=25.041,
                    lng_min=121.509, lng_max=121.521)

    seen_box_args = []
    real_astar = routing.astar

    def spy_astar(graph, start, end, weight_overrides=None, search_box=None):
        seen_box_args.append(search_box)
        return real_astar(graph, start, end,
                          weight_overrides=weight_overrides,
                          search_box=search_box)

    with patch.object(routing, "astar", side_effect=spy_astar):
        routes = routing.find_top_k_routes(g, 1, 3, k=3, search_box=box)

    assert len(seen_box_args) >= 1
    assert all(b is box for b in seen_box_args)
    # Sanity: at least one route was found.
    assert len(routes) >= 1
