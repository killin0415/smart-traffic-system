"""Pure unit tests for src.agents.weight_provider.

No DB, no network. session_factory is mocked as an async context manager;
session.execute returns a result whose .all() yields fake row objects with
attribute access (MagicMock).
"""

from __future__ import annotations

import math
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.routing import GraphEdge, GraphNode, RoadGraph
from src.agents.weight_provider import (
    _DEFAULT_CALIBRATION,
    _MIN_SPEED_KMH,
    PersonalizedWeightProvider,
    SOURCE_FALLBACK,
    SOURCE_TIER_1,
    SOURCE_TIER_2,
    SOURCE_TIER_3,
    TaipeiWeightProvider,
)


# ---------- Fakes / fixtures ----------


def _row(**kwargs) -> MagicMock:
    """Build a fake row whose attribute access returns the kwargs."""
    m = MagicMock()
    for k, v in kwargs.items():
        setattr(m, k, v)
    return m


def _default_maxspeed_rows() -> list[MagicMock]:
    """Default-maxspeed rows roughly matching default_maxspeed() PL/pgSQL."""
    return [
        _row(highway="motorway", speed_kmh=90),
        _row(highway="trunk", speed_kmh=70),
        _row(highway="primary", speed_kmh=50),
        _row(highway="secondary", speed_kmh=50),
        _row(highway="tertiary", speed_kmh=40),
        _row(highway="residential", speed_kmh=30),
        _row(highway="service", speed_kmh=20),
    ]


def _make_session_factory(default_rows, vd_rows):
    """Build a session_factory that yields a session whose execute() returns
    `default_rows` then `vd_rows` (in call order: default-maxspeed first,
    then recent readings, matching rebuild() order)."""

    session = MagicMock()

    # Each .execute() call returns a fresh result mock with .all() ready.
    call_results = []

    def _make_result(rows):
        r = MagicMock()
        r.all = MagicMock(return_value=list(rows))
        return r

    call_results.append(_make_result(default_rows))
    call_results.append(_make_result(vd_rows))

    call_iter = iter(call_results)

    async def _execute(_query):
        return next(call_iter)

    session.execute = AsyncMock(side_effect=_execute)

    class _Ctx:
        async def __aenter__(self_inner):
            return session

        async def __aexit__(self_inner, exc_type, exc, tb):
            return False

    def factory():
        return _Ctx()

    return factory, session


def _edge(
    *,
    eid: int = 1,
    src_id: int = 10,
    tgt_id: int = 20,
    src_lat: float = 25.0330,
    src_lng: float = 121.5654,
    tgt_lat: float = 25.0340,
    tgt_lng: float = 121.5664,
    length_km: float = 0.5,
    road_class: str | None = None,
    max_speed_kmh: int | None = None,
) -> GraphEdge:
    return GraphEdge(
        id=eid,
        source_node_id=src_id,
        target_node_id=tgt_id,
        road_name="Test Rd",
        length_km=length_km,
        road_class=road_class,
        max_speed_kmh=max_speed_kmh,
        oneway=False,
        source_lat_lng=(src_lat, src_lng),
        target_lat_lng=(tgt_lat, tgt_lng),
    )


# ---------- Tier 1 ----------


@pytest.mark.asyncio
async def test_tier1_inverse_distance_when_vds_within_radius():
    """Two VD readings within ~500 m of edge midpoint → Tier 1."""
    edge = _edge(
        src_lat=25.0330, src_lng=121.5654,
        tgt_lat=25.0340, tgt_lng=121.5664,
    )
    # Midpoint ~ (25.0335, 121.5659).
    vd_rows = [
        _row(
            vdid="VD-A",
            latitude=25.0336,
            longitude=121.5660,
            snapped_road_class="primary",
            mean_speed_kmh=40.0,
        ),
        _row(
            vdid="VD-B",
            latitude=25.0334,
            longitude=121.5658,
            snapped_road_class="primary",
            mean_speed_kmh=60.0,
        ),
    ]
    factory, _ = _make_session_factory(_default_maxspeed_rows(), vd_rows)

    provider = TaipeiWeightProvider()
    await provider.rebuild(factory)

    speed, source = provider.get_speed(edge)
    assert source == SOURCE_TIER_1
    # Inverse-distance weighted average of {40, 60} → bounded by [40, 60].
    assert 40.0 <= speed <= 60.0


# ---------- Tier 2 ----------


@pytest.mark.asyncio
async def test_tier2_class_avg_when_no_nearby_vd_but_class_known():
    """VDs are far away (>1km) but share road_class with edge → Tier 2."""
    edge = _edge(
        src_lat=25.0330, src_lng=121.5654,
        tgt_lat=25.0340, tgt_lng=121.5664,
        road_class="primary",
    )
    # Place VDs ~ 5 km away (well beyond 0.01-deg ≈ 1 km radius).
    vd_rows = [
        _row(
            vdid="VD-FAR-1",
            latitude=25.10,  # ~7 km north
            longitude=121.5659,
            snapped_road_class="primary",
            mean_speed_kmh=30.0,
        ),
        _row(
            vdid="VD-FAR-2",
            latitude=25.11,
            longitude=121.5659,
            snapped_road_class="primary",
            mean_speed_kmh=50.0,
        ),
    ]
    factory, _ = _make_session_factory(_default_maxspeed_rows(), vd_rows)

    provider = TaipeiWeightProvider()
    await provider.rebuild(factory)

    speed, source = provider.get_speed(edge)
    assert source == SOURCE_TIER_2
    assert speed == pytest.approx(40.0)  # mean of 30 and 50


# ---------- Tier 3 ----------


@pytest.mark.asyncio
async def test_tier3_uses_max_speed_times_default_calibration_when_class_unknown():
    """Empty VD readings + edge w/ explicit max_speed → Tier 3 (default calibration)."""
    edge = _edge(road_class="residential", max_speed_kmh=30)

    factory, _ = _make_session_factory(_default_maxspeed_rows(), [])
    provider = TaipeiWeightProvider()
    await provider.rebuild(factory)

    speed, source = provider.get_speed(edge)
    assert source == SOURCE_TIER_3
    assert speed == pytest.approx(30.0 * _DEFAULT_CALIBRATION)


@pytest.mark.asyncio
async def test_tier3_falls_back_to_default_calibration_when_class_has_no_calibration():
    """Edge of road_class='secondary' has no calibration entry (only 'primary'
    has VD data) and is far from all VDs → Tier 3 with _DEFAULT_CALIBRATION."""
    # Tier 1 will fail (VD too far). Tier 2 will fail (no class_avg for 'secondary').
    # Tier 3: max_speed * _DEFAULT_CALIBRATION.
    vd_rows = [
        _row(
            vdid="VD-FAR",
            latitude=25.20,  # very far away
            longitude=121.5659,
            snapped_road_class="primary",
            mean_speed_kmh=25.0,
        ),
    ]
    factory, _ = _make_session_factory(_default_maxspeed_rows(), vd_rows)

    provider = TaipeiWeightProvider()
    await provider.rebuild(factory)

    edge = _edge(
        src_lat=25.0330, src_lng=121.5654,
        tgt_lat=25.0340, tgt_lng=121.5664,
        road_class="secondary",
        max_speed_kmh=50,
    )
    speed, source = provider.get_speed(edge)
    assert source == SOURCE_TIER_3
    assert speed == pytest.approx(50.0 * _DEFAULT_CALIBRATION)


# ---------- Calibration formula ----------


@pytest.mark.asyncio
async def test_calibration_is_class_avg_divided_by_default_maxspeed():
    """rebuild populates _state.calibration['primary'] = class_avg/maxspeed."""
    vd_rows = [
        _row(
            vdid="VD-1",
            latitude=25.05,
            longitude=121.55,
            snapped_road_class="primary",
            mean_speed_kmh=20.0,
        ),
        _row(
            vdid="VD-2",
            latitude=25.06,
            longitude=121.55,
            snapped_road_class="primary",
            mean_speed_kmh=30.0,
        ),
    ]
    factory, _ = _make_session_factory(_default_maxspeed_rows(), vd_rows)
    provider = TaipeiWeightProvider()
    await provider.rebuild(factory)

    # default_maxspeed['primary']=50, class_avg['primary']=25 → calibration=0.5
    assert provider._state.class_avg_kmh["primary"] == pytest.approx(25.0)
    assert provider._state.calibration["primary"] == pytest.approx(0.5)


# ---------- kdtree determinism ----------


@pytest.mark.asyncio
async def test_kdtree_get_speed_deterministic_across_rebuilds():
    """Two rebuilds with identical inputs produce identical Tier-1 results."""
    edge = _edge(
        src_lat=25.0330, src_lng=121.5654,
        tgt_lat=25.0340, tgt_lng=121.5664,
    )
    vd_rows = [
        _row(
            vdid="VD-A",
            latitude=25.0336,
            longitude=121.5660,
            snapped_road_class="primary",
            mean_speed_kmh=42.0,
        ),
        _row(
            vdid="VD-B",
            latitude=25.0334,
            longitude=121.5658,
            snapped_road_class="primary",
            mean_speed_kmh=58.0,
        ),
    ]

    factory_1, _ = _make_session_factory(_default_maxspeed_rows(), vd_rows)
    p1 = TaipeiWeightProvider()
    await p1.rebuild(factory_1)
    s1 = p1.get_speed(edge)

    factory_2, _ = _make_session_factory(_default_maxspeed_rows(), vd_rows)
    p2 = TaipeiWeightProvider()
    await p2.rebuild(factory_2)
    s2 = p2.get_speed(edge)

    assert s1 == s2
    assert s1[1] == SOURCE_TIER_1


# ---------- apply_to_graph ----------


@pytest.mark.asyncio
async def test_apply_to_graph_calls_update_weight_per_edge_with_absolute_hours():
    """apply_to_graph computes weight_hours = length_km / speed (clamped at MIN)."""
    graph = RoadGraph()
    # Build 5 edges with varied lengths.
    for i in range(5):
        graph.nodes[i] = GraphNode(id=i, latitude=25.0 + i * 0.001, longitude=121.5)
        graph.nodes[i + 100] = GraphNode(
            id=i + 100, latitude=25.0 + i * 0.001, longitude=121.501
        )
        graph.edges[i] = GraphEdge(
            id=i,
            source_node_id=i,
            target_node_id=i + 100,
            road_name=f"R{i}",
            length_km=0.1 * (i + 1),
            source_lat_lng=(25.0 + i * 0.001, 121.5),
            target_lat_lng=(25.0 + i * 0.001, 121.501),
        )

    provider = TaipeiWeightProvider()
    # Force get_speed to return a fixed value (no rebuild needed).
    provider.get_speed = MagicMock(return_value=(60.0, SOURCE_TIER_1))  # type: ignore[method-assign]

    calls: list[tuple[int, float]] = []

    def _capture_update(eid, w):
        calls.append((eid, w))

    graph.update_weight = _capture_update  # type: ignore[method-assign]

    provider.apply_to_graph(graph)

    assert len(calls) == 5
    seen_ids = {c[0] for c in calls}
    assert seen_ids == set(range(5))
    for eid, w in calls:
        # speed=60.0, length=0.1*(eid+1) → w = length/60.
        expected = (0.1 * (eid + 1)) / 60.0
        assert w == pytest.approx(expected)


@pytest.mark.asyncio
async def test_apply_to_graph_clamps_min_speed():
    """When get_speed returns < _MIN_SPEED_KMH, weight uses the floor."""
    graph = RoadGraph()
    graph.nodes[1] = GraphNode(id=1, latitude=25.0, longitude=121.5)
    graph.nodes[2] = GraphNode(id=2, latitude=25.001, longitude=121.5)
    graph.edges[42] = GraphEdge(
        id=42,
        source_node_id=1,
        target_node_id=2,
        road_name="X",
        length_km=1.0,
        source_lat_lng=(25.0, 121.5),
        target_lat_lng=(25.001, 121.5),
    )

    provider = TaipeiWeightProvider()
    provider.get_speed = MagicMock(return_value=(1.0, SOURCE_TIER_1))  # type: ignore[method-assign]

    calls: list[tuple[int, float]] = []
    graph.update_weight = lambda eid, w: calls.append((eid, w))  # type: ignore[method-assign]

    provider.apply_to_graph(graph)

    assert len(calls) == 1
    eid, w = calls[0]
    assert eid == 42
    # Clamped: weight = 1.0 / max(1.0, _MIN_SPEED_KMH) = 1/5 = 0.2
    assert w == pytest.approx(1.0 / _MIN_SPEED_KMH)


# ---------- update_weight + get_weight ----------


def test_update_weight_sets_and_overwrites_via_get_weight():
    """Direct test of the new RoadGraph.update_weight signature."""
    graph = RoadGraph()
    graph.nodes[1] = GraphNode(id=1, latitude=25.0, longitude=121.5)
    graph.nodes[2] = GraphNode(id=2, latitude=25.001, longitude=121.5)
    edge = GraphEdge(
        id=7,
        source_node_id=1,
        target_node_id=2,
        road_name="X",
        length_km=0.1,
        source_lat_lng=(25.0, 121.5),
        target_lat_lng=(25.001, 121.5),
    )
    graph.edges[7] = edge
    # Build adjacency manually (mirroring from_db logic).
    graph.adjacency[1] = [(2, 7, 0.0)]
    graph.adjacency[2] = [(1, 7, 0.0)]

    graph.update_weight(7, 0.123)
    assert graph.get_weight(7) == pytest.approx(0.123)

    # Overwrite.
    graph.update_weight(7, 0.5)
    assert graph.get_weight(7) == pytest.approx(0.5)


# ---------- Empty rebuild ----------


@pytest.mark.asyncio
async def test_empty_rebuild_falls_back_to_tier3():
    """Zero VD rows → empty kdtree, empty class_avg, default_maxspeed populated."""
    factory, _ = _make_session_factory(_default_maxspeed_rows(), [])
    provider = TaipeiWeightProvider()
    await provider.rebuild(factory)

    assert provider._state.kdtree is None
    assert provider._state.class_avg_kmh == {}
    assert provider._state.default_maxspeed["primary"] == 50

    # Edge with road_class but no max_speed → uses default_maxspeed * _DEFAULT_CALIBRATION.
    edge = _edge(road_class="primary", max_speed_kmh=None)
    speed, source = provider.get_speed(edge)
    assert source == SOURCE_TIER_3
    assert speed == pytest.approx(50.0 * _DEFAULT_CALIBRATION)


# ---------- PersonalizedWeightProvider pass-through ----------


@pytest.mark.asyncio
async def test_personalized_provider_delegates_each_method_once():
    base = MagicMock()
    base.rebuild = AsyncMock()
    base.get_speed = MagicMock(return_value=(42.0, SOURCE_TIER_1))
    base.apply_to_graph = MagicMock()

    pw = PersonalizedWeightProvider(base, user_id="u1")

    # rebuild
    sentinel_factory = object()
    await pw.rebuild(sentinel_factory)
    base.rebuild.assert_awaited_once_with(sentinel_factory)

    # get_speed
    e = _edge(road_class="primary")
    out = pw.get_speed(e)
    base.get_speed.assert_called_once_with(e)
    assert out == (42.0, SOURCE_TIER_1)

    # apply_to_graph
    g = MagicMock()
    pw.apply_to_graph(g)
    base.apply_to_graph.assert_called_once_with(g)


# ---------- Defensive: nan midpoint -> not Tier 1 ----------


@pytest.mark.asyncio
async def test_get_speed_handles_missing_endpoints_via_tier_chain():
    """Edge missing source_lat_lng → Tier 1 cannot compute midpoint, falls
    through to Tier 2/3."""
    factory, _ = _make_session_factory(
        _default_maxspeed_rows(),
        [
            _row(
                vdid="VD-1",
                latitude=25.0336,
                longitude=121.5660,
                snapped_road_class="primary",
                mean_speed_kmh=40.0,
            ),
        ],
    )
    provider = TaipeiWeightProvider()
    await provider.rebuild(factory)

    edge = GraphEdge(
        id=99,
        source_node_id=1,
        target_node_id=2,
        road_name="?",
        length_km=0.1,
        road_class="primary",
        max_speed_kmh=50,
        source_lat_lng=None,
        target_lat_lng=None,
    )
    # Tier 1 needs midpoint -> skipped. class_avg has 'primary' -> Tier 2.
    speed, source = provider.get_speed(edge)
    assert source == SOURCE_TIER_2
    assert speed == pytest.approx(40.0)


# ---------- Last-resort fallback ----------


@pytest.mark.asyncio
async def test_fallback_when_no_max_speed_no_class_no_calibration():
    """Degenerate edge: no road_class, no max_speed, empty default_maxspeed
    for '' key → still gets a positive Tier-3 speed (uses 30 km/h fallback)."""
    factory, _ = _make_session_factory(_default_maxspeed_rows(), [])
    provider = TaipeiWeightProvider()
    await provider.rebuild(factory)

    edge = GraphEdge(
        id=1,
        source_node_id=1,
        target_node_id=2,
        road_name="",
        length_km=0.1,
        road_class=None,
        max_speed_kmh=None,
        source_lat_lng=(25.0, 121.5),
        target_lat_lng=(25.001, 121.501),
    )
    speed, source = provider.get_speed(edge)
    # No max_speed → defaults to default_maxspeed.get("", 30) == 30; calibration=0.5.
    # Speed = 15.0 > 0 → SOURCE_TIER_3.
    assert source == SOURCE_TIER_3
    assert speed == pytest.approx(30.0 * _DEFAULT_CALIBRATION)
