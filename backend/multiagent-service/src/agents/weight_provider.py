"""
WeightProvider — three-tier dynamic edge speed estimation.

Tier 1 (Spatial VD)        : k-d tree lookup of nearest VDs within ~1 km;
                             inverse-distance weighted average of recent
                             readings.
Tier 2 (Class average)     : if Tier 1 has no neighbour, use the city-wide
                             mean speed for the edge's OSM `road_class`,
                             computed from the same VD readings.
Tier 3 (Calibrated max)    : if even Tier 2 is empty, use
                             `max_speed_kmh × calibration[road_class]` where
                             calibration is the data-driven ratio of average
                             VD speed to default max speed.

Default max speed lives in PL/pgSQL `default_maxspeed(highway)`; we never
duplicate the table in Python.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, Tuple

import numpy as np
from scipy.spatial import cKDTree
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from src.agents.routing import GraphEdge, RoadGraph

logger = logging.getLogger(__name__)


# Lookback window for "current" speed readings used to rebuild the model.
_RECENT_WINDOW_MIN = 10

# Tier 1 query parameters.
_KDTREE_K = 3
# 0.01 degree ≈ 1.11 km lat / 1.01 km lng at Taipei latitude (cos(25°)≈0.906).
# We don't apply cos(lat) correction; the asymmetry is < 11% and irrelevant
# to the inverse-distance weighting (Tier 1 is a coarse spatial smoother).
_KDTREE_RADIUS_DEG = 0.01

# Tier 3 fallback when no calibration exists for a road_class.
_DEFAULT_CALIBRATION = 0.5

# Floor passed to the time computation. Edges with speed < 5 km/h would
# explode the weight and starve A*; the cruise speed is bounded but the
# signal-stop penalty is added independently in A*.
_MIN_SPEED_KMH = 5.0


SOURCE_TIER_1 = "vd_spatial"
SOURCE_TIER_2 = "class_avg"
SOURCE_TIER_3 = "calibrated_max"
SOURCE_FALLBACK = "fallback_default"


@dataclass
class _ProviderState:
    """Snapshot of the model after rebuild()."""

    kdtree: cKDTree | None = None
    vd_speeds: np.ndarray = field(default_factory=lambda: np.zeros(0))
    vd_coords: np.ndarray = field(default_factory=lambda: np.zeros((0, 2)))
    class_avg_kmh: dict[str, float] = field(default_factory=dict)
    calibration: dict[str, float] = field(default_factory=dict)
    default_maxspeed: dict[str, int] = field(default_factory=dict)


# ---------- Protocols ----------


class WeightProvider(Protocol):
    async def rebuild(self, session_factory) -> None: ...
    def get_speed(self, edge: "GraphEdge") -> Tuple[float, str]: ...
    def apply_to_graph(self, graph: "RoadGraph") -> None: ...


# ---------- TaipeiWeightProvider ----------


_DEFAULT_MAXSPEED_QUERY = text(
    """
    SELECT v.highway, default_maxspeed(v.highway) AS speed_kmh
    FROM (VALUES
        ('motorway'),('trunk'),('primary'),('secondary'),('tertiary'),
        ('unclassified'),('residential'),('service'),
        ('motorway_link'),('trunk_link'),('primary_link'),
        ('secondary_link'),('tertiary_link'),('living_street')
    ) AS v(highway)
    """
)

# Recent VD readings joined with snapped road class. We use DISTINCT ON to keep
# the most recent reading per (vdid, lane), then average across lanes per VD.
_RECENT_READINGS_QUERY = text(
    """
    WITH latest AS (
        SELECT DISTINCT ON (vdid, lane_no)
            vdid, lane_no, ts, avg_speed
        FROM vd_reading
        WHERE ts > NOW() - INTERVAL '10 minutes'
          AND avg_speed IS NOT NULL
          AND avg_speed > 0
        ORDER BY vdid, lane_no, ts DESC
    )
    SELECT
        v.vdid,
        v.latitude,
        v.longitude,
        v.snapped_road_class,
        AVG(latest.avg_speed) AS mean_speed_kmh
    FROM vd_static v
    JOIN latest ON latest.vdid = v.vdid
    GROUP BY v.vdid, v.latitude, v.longitude, v.snapped_road_class
    """
)


class TaipeiWeightProvider:
    """Default implementation backed by data.taipei VD readings."""

    def __init__(self) -> None:
        self._state = _ProviderState()

    # ---------- rebuild ----------

    async def rebuild(self, session_factory) -> None:
        async with session_factory() as session:
            default_maxspeed = await self._load_default_maxspeed(session)
            vd_rows = (await session.execute(_RECENT_READINGS_QUERY)).all()

        if not vd_rows:
            logger.warning(
                "TaipeiWeightProvider.rebuild: no VD readings in last %d min — "
                "falling back to defaults only",
                _RECENT_WINDOW_MIN,
            )
            self._state = _ProviderState(default_maxspeed=default_maxspeed)
            return

        coords = np.array([[r.latitude, r.longitude] for r in vd_rows], dtype=float)
        speeds = np.array([float(r.mean_speed_kmh) for r in vd_rows], dtype=float)

        # Aggregate per snapped_road_class (Tier 2).
        class_speeds: dict[str, list[float]] = {}
        for r in vd_rows:
            cls = r.snapped_road_class
            if cls:
                class_speeds.setdefault(cls, []).append(float(r.mean_speed_kmh))
        class_avg = {cls: float(np.mean(values)) for cls, values in class_speeds.items()}

        # Tier 3 calibration: ratio of class average to default max speed.
        calibration: dict[str, float] = {}
        for cls, avg_kmh in class_avg.items():
            base = default_maxspeed.get(cls)
            if base and base > 0:
                calibration[cls] = avg_kmh / base

        kdtree = cKDTree(coords) if len(coords) else None

        self._state = _ProviderState(
            kdtree=kdtree,
            vd_speeds=speeds,
            vd_coords=coords,
            class_avg_kmh=class_avg,
            calibration=calibration,
            default_maxspeed=default_maxspeed,
        )

        logger.info(
            "TaipeiWeightProvider rebuilt: %d VDs, %d road classes, "
            "calibration=%s",
            len(vd_rows),
            len(class_avg),
            {k: round(v, 3) for k, v in calibration.items()},
        )

    async def _load_default_maxspeed(self, session: AsyncSession) -> dict[str, int]:
        rows = (await session.execute(_DEFAULT_MAXSPEED_QUERY)).all()
        return {r.highway: int(r.speed_kmh) for r in rows}

    # ---------- get_speed ----------

    def get_speed(self, edge: "GraphEdge") -> Tuple[float, str]:
        s = self._state

        # Tier 1: VD spatial inverse-distance weighting.
        if s.kdtree is not None and len(s.vd_speeds) > 0:
            mid_lat, mid_lng = _midpoint(edge)
            if mid_lat is not None:
                dists, idxs = s.kdtree.query(
                    [mid_lat, mid_lng],
                    k=min(_KDTREE_K, len(s.vd_speeds)),
                    distance_upper_bound=_KDTREE_RADIUS_DEG,
                )
                # query returns scalars when k=1; normalise to arrays.
                dists = np.atleast_1d(dists)
                idxs = np.atleast_1d(idxs)
                valid = (idxs < len(s.vd_speeds)) & np.isfinite(dists)
                if np.any(valid):
                    d = dists[valid]
                    speeds = s.vd_speeds[idxs[valid]]
                    # Inverse-distance weighting; tiny epsilon keeps zero distance safe.
                    weights = 1.0 / (d + 1e-9)
                    speed = float(np.average(speeds, weights=weights))
                    return speed, SOURCE_TIER_1

        # Tier 2: class average.
        if edge.road_class and edge.road_class in s.class_avg_kmh:
            return float(s.class_avg_kmh[edge.road_class]), SOURCE_TIER_2

        # Tier 3: max_speed × calibration.
        cls = edge.road_class or ""
        max_speed = edge.max_speed_kmh
        if not max_speed:
            max_speed = s.default_maxspeed.get(cls, 30)
        cal = s.calibration.get(cls, _DEFAULT_CALIBRATION)
        speed = float(max_speed) * cal
        if speed > 0:
            return speed, SOURCE_TIER_3

        # Last-resort fallback (degenerate input). 30 km/h × 0.5 = 15 km/h.
        return 30.0 * _DEFAULT_CALIBRATION, SOURCE_FALLBACK

    # ---------- apply_to_graph ----------

    def apply_to_graph(self, graph: "RoadGraph") -> None:
        for edge in graph.edges.values():
            speed, _ = self.get_speed(edge)
            speed = max(speed, _MIN_SPEED_KMH)
            weight_hours = edge.length_km / speed
            graph.update_weight(edge.id, weight_hours)


# ---------- Helpers ----------


def _midpoint(edge: "GraphEdge") -> tuple[float | None, float | None]:
    """Return the midpoint of the edge's two endpoints, if both are present."""
    src = edge.source_lat_lng
    tgt = edge.target_lat_lng
    if src is None or tgt is None:
        return None, None
    if any(math.isnan(c) for c in (*src, *tgt)):
        return None, None
    return (src[0] + tgt[0]) / 2.0, (src[1] + tgt[1]) / 2.0


# ---------- Personalized stub ----------


class PersonalizedWeightProvider:
    """Phase-2 stub: pass-through wrapper.

    Phase 1 always delegates to the base provider. Phase 2 will override
    `get_speed` to apply user-specific edge preference (e.g. avoid hills,
    prefer historically-driven roads).
    """

    def __init__(self, base: WeightProvider, user_id: str) -> None:
        self._base = base
        self.user_id = user_id

    async def rebuild(self, session_factory) -> None:
        await self._base.rebuild(session_factory)

    def get_speed(self, edge: "GraphEdge") -> Tuple[float, str]:
        return self._base.get_speed(edge)

    def apply_to_graph(self, graph: "RoadGraph") -> None:
        self._base.apply_to_graph(graph)
