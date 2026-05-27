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

    Bypasses DB. Each non-oneway test edge here still uses the legacy
    "single edge_id with both directions in forward adjacency" pattern
    (the post-rebuild production model is `two separate edge rows per
    non-oneway street`, which fix-osm-graph-topology added). The helper
    mirrors that bidirectional entry into `reverse_adjacency` too so reach
    BFS tests and the structural invariant `reverse mirrors forward`
    both hold under this synthetic shape.
    """
    g = RoadGraph()
    for n in nodes:
        g.nodes[n.id] = n
        g.adjacency.setdefault(n.id, [])
        g.reverse_adjacency.setdefault(n.id, [])
    for e in edges:
        g.edges[e.id] = e
        g.adjacency.setdefault(e.source_node_id, []).append(
            (e.target_node_id, e.id, 0.0)
        )
        g.reverse_adjacency.setdefault(e.target_node_id, []).append(
            (e.source_node_id, e.id, 0.0)
        )
        if not e.oneway:
            g.adjacency.setdefault(e.target_node_id, []).append(
                (e.source_node_id, e.id, 0.0)
            )
            g.reverse_adjacency.setdefault(e.source_node_id, []).append(
                (e.target_node_id, e.id, 0.0)
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


# ---------- (l) snap_to_graph value-based dispatch ----------


class TestSnapToGraphReturnTopNValueDispatch:
    """fix-osm-graph-topology §6.4 — snap_to_graph return type is determined
    by the *value* of return_top_n, not by whether it was passed."""

    def test_default_returns_int_when_graph_populated(self):
        n1 = GraphNode(id=1, latitude=25.040, longitude=121.510)
        n2 = GraphNode(id=2, latitude=25.040, longitude=121.515)
        g = _build_graph([n1, n2], [_edge(101, 1, 2, length_km=1.0)], {})
        got = routing.snap_to_graph(25.040, 121.510, g)
        assert isinstance(got, int)
        assert got in (1, 2)

    def test_top_n_one_returns_int_when_graph_populated(self):
        n1 = GraphNode(id=1, latitude=25.040, longitude=121.510)
        n2 = GraphNode(id=2, latitude=25.040, longitude=121.515)
        g = _build_graph([n1, n2], [_edge(101, 1, 2, length_km=1.0)], {})
        got = routing.snap_to_graph(25.040, 121.510, g, return_top_n=1)
        assert isinstance(got, int)

    def test_top_n_five_returns_list_with_correct_ordering(self):
        # Four nodes: same distance bucket, varying degrees, plus one farther.
        n1 = GraphNode(id=1, latitude=25.040, longitude=121.510)  # degree 1
        n2 = GraphNode(id=2, latitude=25.0405, longitude=121.510)  # degree 3
        n3 = GraphNode(id=3, latitude=25.041, longitude=121.510)  # degree 2
        n4 = GraphNode(id=4, latitude=25.045, longitude=121.510)  # degree 0, far
        edges = [
            _edge(101, 1, 2, length_km=0.1),  # n1 deg 1
            _edge(102, 2, 3, length_km=0.1),  # n2,n3 share
            _edge(103, 3, 2, length_km=0.1, src_ll=None, tgt_ll=None),  # extra deg @ n2,n3
            _edge(104, 2, 1, length_km=0.1),  # extra deg @ n2
        ]
        for e in edges:
            e.oneway = True
        g = _build_graph([n1, n2, n3, n4], edges, {})
        # snap origin near n1
        ranked = routing.snap_to_graph(
            25.040, 121.510, g, return_top_n=5,
        )
        assert isinstance(ranked, list)
        # n4 (degree 0) must be last among returned candidates.
        # n2 (highest degree) must be first.
        assert ranked[0] == 2
        assert ranked[-1] == 4
        # Length capped at min(graph_node_count, return_top_n).
        assert len(ranked) == 4

    def test_empty_graph_top_n_one_returns_none(self):
        g = RoadGraph()
        assert routing.snap_to_graph(25.0, 121.0, g) is None
        assert routing.snap_to_graph(25.0, 121.0, g, return_top_n=1) is None

    def test_empty_graph_top_n_five_returns_empty_list(self):
        g = RoadGraph()
        got = routing.snap_to_graph(25.0, 121.0, g, return_top_n=5)
        assert got == []

    def test_top_n_larger_than_graph_returns_truncated_list(self):
        n1 = GraphNode(id=1, latitude=25.040, longitude=121.510)
        g = _build_graph([n1], [], {})
        got = routing.snap_to_graph(25.040, 121.510, g, return_top_n=5)
        assert got == [1]


# ---------- (m) RoadGraph.from_db builds reverse adjacency ----------


@pytest.mark.asyncio
async def test_road_graph_builds_reverse_adjacency():
    """fix-osm-graph-topology §7.1 — from_db must build reverse_adjacency
    that mirrors forward exactly, with each edge_id appearing once in each
    dict (no double counting). Simulates the new SQL model where non-oneway
    streets emit TWO separate edge rows (one per direction)."""

    class _TN:
        def __init__(self, id, lat, lng, has_signal=False):
            self.id = id
            self.latitude = lat
            self.longitude = lng
            self.has_signal = has_signal

    class _TE:
        def __init__(self, id, src, tgt, length_km, oneway, road_class="primary",
                     max_speed_kmh=50, road_name="Rd"):
            self.id = id
            self.source_node_id = src
            self.target_node_id = tgt
            self.length_km = length_km
            self.road_class = road_class
            self.max_speed_kmh = max_speed_kmh
            self.oneway = oneway
            self.road_name = road_name

    nodes = [_TN(1, 25.0, 121.500), _TN(2, 25.0, 121.510), _TN(3, 25.0, 121.520)]
    # New-model: non-oneway A↔B is two separate edges (101 fwd, 102 rev).
    # Oneway B→C is a single edge (103).
    edges = [
        _TE(101, 1, 2, 1.0, oneway=False),
        _TE(102, 2, 1, 1.0, oneway=False),
        _TE(103, 2, 3, 1.0, oneway=True),
    ]

    call_count = {"n": 0}

    def make_result(items):
        result = MagicMock()
        result.scalars.return_value.all.return_value = items
        return result

    async def fake_execute(_query):
        call_count["n"] += 1
        return make_result(nodes if call_count["n"] == 1 else edges)

    session = MagicMock()
    session.execute = fake_execute

    graph = await RoadGraph.from_db(session)

    # Forward adjacency: one entry per edge, at its source.
    assert (2, 101, 0.0) in graph.adjacency[1]
    assert (1, 102, 0.0) in graph.adjacency[2]
    assert (3, 103, 0.0) in graph.adjacency[2]

    # Reverse adjacency: same entries indexed by target.
    assert (1, 101, 0.0) in graph.reverse_adjacency[2]
    assert (2, 102, 0.0) in graph.reverse_adjacency[1]
    assert (2, 103, 0.0) in graph.reverse_adjacency[3]

    # No double-counting: each edge appears exactly once in forward and once
    # in reverse.
    assert len(graph.adjacency[1]) == 1
    assert len(graph.adjacency[2]) == 2
    assert len(graph.adjacency[3]) == 0
    assert len(graph.reverse_adjacency[1]) == 1
    assert len(graph.reverse_adjacency[2]) == 1
    assert len(graph.reverse_adjacency[3]) == 1


# ---------- (n) update_weight syncs both forward and reverse ----------


def test_road_graph_update_weight_syncs_reverse():
    """fix-osm-graph-topology §7.2 — update_weight updates the single forward
    entry AND the single reverse entry for an edge_id."""
    n1 = GraphNode(id=1, latitude=0.0, longitude=0.0)
    n2 = GraphNode(id=2, latitude=0.0, longitude=0.001)
    n3 = GraphNode(id=3, latitude=0.0, longitude=0.002)
    # Use oneway edges so we get the clean new-model shape (single entry per
    # direction in each dict).
    e_fwd = _edge(201, 1, 2, length_km=0.1)
    e_fwd.oneway = True
    e_oneway = _edge(202, 2, 3, length_km=0.1)
    e_oneway.oneway = True
    g = _build_graph([n1, n2, n3], [e_fwd, e_oneway], {})

    g.update_weight(201, 0.5)

    # Forward at source(1): entry (2, 201, 0.5).
    fwd = [t for t in g.adjacency[1] if t[1] == 201]
    assert fwd and math.isclose(fwd[0][2], 0.5, rel_tol=1e-9)
    # Reverse at target(2): entry (1, 201, 0.5).
    rev = [t for t in g.reverse_adjacency[2] if t[1] == 201]
    assert rev and math.isclose(rev[0][2], 0.5, rel_tol=1e-9)
    # Other edges untouched.
    other_fwd = [t for t in g.adjacency[2] if t[1] == 202]
    assert other_fwd and math.isclose(other_fwd[0][2], 0.0, rel_tol=1e-9)


# ---------- (o)(p) reachability fallback in plan_optimal_route ----------


def _make_chain_graph(num_extra: int = 8):
    """Build: n1 dead-end (no outgoing), n2 with chain to n3..n(2+num_extra).

    Snap is mocked separately; this just provides graph state for the BFS
    helpers to evaluate.
    """
    nodes = [
        GraphNode(id=1, latitude=25.040, longitude=121.510),
        GraphNode(id=2, latitude=25.040, longitude=121.511),
    ]
    for i in range(num_extra):
        nodes.append(GraphNode(id=3 + i, latitude=25.041 + i * 0.001, longitude=121.511))
    edges = []
    # n1 has no outgoing edges (isolated in the forward direction).
    # n2 -> n3 -> n4 -> ... chain (all oneway forward)
    chain_ids = [n.id for n in nodes if n.id >= 2]
    for i in range(len(chain_ids) - 1):
        e = _edge(500 + i, chain_ids[i], chain_ids[i + 1], length_km=0.1)
        e.oneway = True
        edges.append(e)
    weights = {e.id: 0.1 / 60 for e in edges}
    return _build_graph(nodes, edges, weights), nodes


@pytest.mark.asyncio
async def test_plan_route_falls_back_on_unreachable_origin_snap(monkeypatch):
    """Origin snap candidate[0] has zero outgoing reach; candidate[1] reaches
    a long chain. plan_optimal_route must pick candidate[1] as start_id."""
    g, nodes = _make_chain_graph(num_extra=8)  # 10 nodes total
    last_node_id = nodes[-1].id

    # Force the reach floor below the 8-node chain so candidate[1] passes
    # but the isolated candidate[0] fails.
    monkeypatch.setattr(routing, "REACHABILITY_MIN_NODES_FLOOR", 3)

    # Snap origin → [1 (isolated), 2 (good)]. Snap dest → [last_node_id].
    def fake_snap(lat, lng, graph, k=15, return_top_n=1):
        if return_top_n >= 2:
            if abs(lng - 121.510) < 1e-6:  # origin
                return [1, 2]
            return [last_node_id]
        return 1 if abs(lng - 121.510) < 1e-6 else last_node_id

    captured: dict = {}

    def fake_top_k(graph, start, end, k=3, search_box=None, **_):
        captured["start"] = start
        captured["end"] = end
        return [([start, end], [], 0.01)]

    fake_session = MagicMock()
    cam_result = MagicMock()
    cam_result.scalars.return_value.all.return_value = []
    fake_session.execute = AsyncMock(return_value=cam_result)

    with patch.object(routing, "snap_to_graph", side_effect=fake_snap), \
         patch.object(routing, "find_top_k_routes", side_effect=fake_top_k), \
         patch.object(routing, "query_parking_near_destination",
                      new=AsyncMock(return_value=[])):
        await plan_optimal_route(
            session=fake_session, graph=g, weight_provider=None,
            origin_lat=25.040, origin_lng=121.510,
            dest_lat=25.041, dest_lng=121.511,
            k=3,
        )

    assert captured["start"] == 2, (
        f"expected fallback to candidate[1]=2 (isolated candidate[0]=1 has "
        f"no outgoing reach), got start_id={captured.get('start')}"
    )


@pytest.mark.asyncio
async def test_plan_route_falls_back_on_unreachable_destination_snap(monkeypatch):
    """Destination snap candidate[0] has zero INCOMING reach; candidate[1]
    has long incoming chain. plan_optimal_route must pick candidate[1] as
    end_id (using reverse_adjacency BFS)."""
    g, nodes = _make_chain_graph(num_extra=8)
    last_node_id = nodes[-1].id

    monkeypatch.setattr(routing, "REACHABILITY_MIN_NODES_FLOOR", 3)

    # Origin snap returns just [1] (we don't care about origin in this test
    # — well, 1 has zero out-reach so we have to handle the fallback too;
    # add a working start by including [1, 2]).
    # Dest snap returns [orphan_id (no incoming), last_node_id (good)].
    # We need a node with zero incoming reach. Node 1 in our graph has zero
    # incoming (no edge targets it). Let's use [1, last_node_id] for dest.

    def fake_snap(lat, lng, graph, k=15, return_top_n=1):
        if return_top_n >= 2:
            if abs(lng - 121.510) < 1e-6:  # origin
                return [1, 2]
            # dest: candidate[0] = 1 (no incoming), candidate[1] = last_node_id (good)
            return [1, last_node_id]
        return 1

    captured: dict = {}

    def fake_top_k(graph, start, end, k=3, search_box=None, **_):
        captured["start"] = start
        captured["end"] = end
        return [([start, end], [], 0.01)]

    fake_session = MagicMock()
    cam_result = MagicMock()
    cam_result.scalars.return_value.all.return_value = []
    fake_session.execute = AsyncMock(return_value=cam_result)

    with patch.object(routing, "snap_to_graph", side_effect=fake_snap), \
         patch.object(routing, "find_top_k_routes", side_effect=fake_top_k), \
         patch.object(routing, "query_parking_near_destination",
                      new=AsyncMock(return_value=[])):
        await plan_optimal_route(
            session=fake_session, graph=g, weight_provider=None,
            origin_lat=25.040, origin_lng=121.510,
            dest_lat=25.045, dest_lng=121.520,
            k=3,
        )

    assert captured["end"] == last_node_id, (
        f"expected fallback to dest candidate[1]={last_node_id} (candidate[0]=1 "
        f"has no incoming reach), got end_id={captured.get('end')}"
    )


@pytest.mark.asyncio
async def test_plan_route_skips_reachability_on_tiny_graph(monkeypatch):
    """fix-osm-graph-topology spec scenario `極小圖跳過檢驗`: when
    `len(graph.nodes) < REACHABILITY_MIN_NODES`, plan_optimal_route SHALL
    skip reach helpers entirely and use snap candidate[0] directly."""
    n1 = GraphNode(id=1, latitude=25.040, longitude=121.510)
    n2 = GraphNode(id=2, latitude=25.040, longitude=121.520)
    edges = [_edge(101, 1, 2, length_km=1.0)]
    g = _build_graph([n1, n2], edges, {101: 1.0 / 60})
    # graph has 2 nodes; default floor=100 -> reach_min=100; 2 < 100 -> skip.

    boom_calls = []

    def boom(*args, **kwargs):
        boom_calls.append((args, kwargs))
        raise AssertionError(
            "reach helper called on tiny graph (skip_reach should be True)"
        )

    fake_session = MagicMock()
    cam_result = MagicMock()
    cam_result.scalars.return_value.all.return_value = []
    fake_session.execute = AsyncMock(return_value=cam_result)

    with patch.object(routing, "_has_outgoing_reach", side_effect=boom), \
         patch.object(routing, "_has_incoming_reach", side_effect=boom), \
         patch.object(routing, "query_parking_near_destination",
                      new=AsyncMock(return_value=[])):
        out = await plan_optimal_route(
            session=fake_session, graph=g, weight_provider=None,
            origin_lat=25.040, origin_lng=121.510,
            dest_lat=25.040, dest_lng=121.520,
            k=3,
        )

    assert boom_calls == []
    assert out["error"] is None
    assert len(out["routes"]) >= 1
