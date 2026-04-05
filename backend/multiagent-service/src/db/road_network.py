"""
路網解析模組：JSON 讀取、Node 推導與去重、Edge 建立。
"""

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_SPEED_LIMIT = 40  # km/h
SNAP_TOLERANCE_M = 20.0
EARTH_RADIUS_KM = 6371.0

# Relative to repo root: data/kaohsiung_road_sections.json
DEFAULT_JSON_PATH = Path(__file__).resolve().parents[4] / "data" / "kaohsiung_road_sections.json"


@dataclass
class Coord:
    latitude: float
    longitude: float


@dataclass
class ParsedNode:
    id: int
    latitude: float
    longitude: float


@dataclass
class ParsedEdge:
    source_node_id: int
    target_node_id: int
    road_name: str
    length_km: float
    speed_limit_kmh: int
    base_weight: float


@dataclass
class ParsedRoadNetwork:
    nodes: list[ParsedNode] = field(default_factory=list)
    edges: list[ParsedEdge] = field(default_factory=list)


def haversine_m(a: Coord, b: Coord) -> float:
    """計算兩座標間的 Haversine 距離（公尺）。"""
    lat1, lat2 = math.radians(a.latitude), math.radians(b.latitude)
    dlat = lat2 - lat1
    dlng = math.radians(b.longitude - a.longitude)
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h)) * 1000


def load_road_sections(path: Path | None = None) -> list[dict]:
    """從 JSON 檔案讀取 road_sections。"""
    p = path or DEFAULT_JSON_PATH
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    return data["road_sections"]


def _get_endpoints(section: dict) -> tuple[Coord, Coord]:
    """從 RoadSection 的 geometry 取出起點和終點座標。"""
    coords = section.get("geometry", [])
    if not coords or len(coords) < 2:
        # geometry 只有一個點時，起終點相同
        pt = coords[0] if coords else [0, 0]
        c = Coord(latitude=pt[1], longitude=pt[0])
        return c, c
    start = coords[0]
    end = coords[-1]
    return Coord(latitude=start[1], longitude=start[0]), Coord(latitude=end[1], longitude=end[0])


def deduplicate_nodes(candidates: list[Coord], tolerance_m: float = SNAP_TOLERANCE_M) -> list[ParsedNode]:
    """對候選 node 座標去重，tolerance 內的合併為同一 node。"""
    nodes: list[ParsedNode] = []
    for coord in candidates:
        merged = False
        for node in nodes:
            if haversine_m(coord, Coord(node.latitude, node.longitude)) < tolerance_m:
                merged = True
                break
        if not merged:
            nodes.append(ParsedNode(id=len(nodes) + 1, latitude=coord.latitude, longitude=coord.longitude))
    return nodes


def find_node_id(coord: Coord, nodes: list[ParsedNode], tolerance_m: float = SNAP_TOLERANCE_M) -> int:
    """找到與座標最近的 node ID。"""
    best_id = nodes[0].id
    best_dist = float("inf")
    for node in nodes:
        d = haversine_m(coord, Coord(node.latitude, node.longitude))
        if d < best_dist:
            best_dist = d
            best_id = node.id
    return best_id


def compute_base_weight(length_km: float, speed_limit_kmh: int) -> float:
    """計算 edge 的 base_weight = length_km / speed_limit_kmh。"""
    speed = speed_limit_kmh if speed_limit_kmh and speed_limit_kmh > 0 else DEFAULT_SPEED_LIMIT
    return length_km / speed


def parse_road_network(sections: list[dict]) -> ParsedRoadNetwork:
    """從 RoadSection 列表解析出完整路網（nodes + edges）。"""
    # 收集所有候選座標
    candidates: list[Coord] = []
    for section in sections:
        start, end = _get_endpoints(section)
        candidates.append(start)
        candidates.append(end)

    # 去重
    nodes = deduplicate_nodes(candidates)

    # 建立 edges
    edges: list[ParsedEdge] = []
    for section in sections:
        start, end = _get_endpoints(section)
        src_id = find_node_id(start, nodes)
        tgt_id = find_node_id(end, nodes)

        length_km = section.get("RoadLength", 0) / 1000  # TDX 單位為公尺
        speed_limit = section.get("SpeedLimit", 0)
        bw = compute_base_weight(length_km, speed_limit)

        edges.append(ParsedEdge(
            source_node_id=src_id,
            target_node_id=tgt_id,
            road_name=section.get("RoadName", ""),
            length_km=length_km,
            speed_limit_kmh=speed_limit if speed_limit and speed_limit > 0 else DEFAULT_SPEED_LIMIT,
            base_weight=bw,
        ))

    return ParsedRoadNetwork(nodes=nodes, edges=edges)
