"""路網解析邏輯 unit tests。"""

import json
import tempfile
from pathlib import Path

from src.db.road_network import (
    Coord,
    compute_base_weight,
    deduplicate_nodes,
    haversine_m,
    load_road_sections,
    parse_road_network,
)

# --- Fixtures ---

SAMPLE_SECTIONS = [
    {
        "RoadSectionID": "RS001",
        "RoadName": "中山路",
        "geometry": [[120.300, 22.620], [120.305, 22.625]],
        "RoadLength": 600,  # 公尺
        "SpeedLimit": 50,
    },
    {
        "RoadSectionID": "RS002",
        "RoadName": "建國路",
        "geometry": [[120.305, 22.625], [120.310, 22.630]],
        "RoadLength": 800,
        "SpeedLimit": 60,
    },
]


def _make_json_file(sections: list[dict]) -> Path:
    """建立暫存 JSON 檔案。"""
    data = {
        "metadata": {"source": "test", "count": len(sections)},
        "road_sections": sections,
    }
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(data, f, ensure_ascii=False)
    f.close()
    return Path(f.name)


# --- 6.2 JSON 解析 test ---


class TestLoadRoadSections:
    def test_parse_json_returns_sections(self):
        path = _make_json_file(SAMPLE_SECTIONS)
        sections = load_road_sections(path)
        assert len(sections) == 2
        assert sections[0]["RoadSectionID"] == "RS001"
        assert sections[1]["RoadName"] == "建國路"

    def test_parse_json_preserves_geometry(self):
        path = _make_json_file(SAMPLE_SECTIONS)
        sections = load_road_sections(path)
        assert sections[0]["geometry"] == [[120.300, 22.620], [120.305, 22.625]]


# --- 6.3 Node 去重 test ---


class TestDeduplicateNodes:
    def test_nearby_coords_merged(self):
        """距離 < 20m 的座標應合併為同一 node。"""
        # 兩個幾乎相同的座標（相距約 1m）
        candidates = [
            Coord(22.625000, 120.305000),
            Coord(22.625001, 120.305001),
        ]
        nodes = deduplicate_nodes(candidates, tolerance_m=20.0)
        assert len(nodes) == 1

    def test_distant_coords_kept_separate(self):
        """距離 >= 20m 的座標應保持獨立。"""
        # 兩個明顯不同的座標（相距約 700m）
        candidates = [
            Coord(22.620, 120.300),
            Coord(22.625, 120.305),
        ]
        nodes = deduplicate_nodes(candidates, tolerance_m=20.0)
        assert len(nodes) == 2

    def test_multiple_clusters(self):
        """多個群組各自合併。"""
        candidates = [
            Coord(22.620, 120.300),
            Coord(22.620001, 120.300001),  # 與第一個合併
            Coord(22.630, 120.310),
            Coord(22.630001, 120.310001),  # 與第三個合併
        ]
        nodes = deduplicate_nodes(candidates, tolerance_m=20.0)
        assert len(nodes) == 2

    def test_empty_candidates(self):
        nodes = deduplicate_nodes([], tolerance_m=20.0)
        assert len(nodes) == 0


# --- 6.4 base_weight 計算 test ---


class TestComputeBaseWeight:
    def test_normal_calculation(self):
        """1.0 km / 50 km/h = 0.02 hours。"""
        assert compute_base_weight(1.0, 50) == 1.0 / 50

    def test_different_values(self):
        """2.4 km / 60 km/h = 0.04 hours。"""
        assert compute_base_weight(2.4, 60) == 2.4 / 60

    def test_precision(self):
        weight = compute_base_weight(0.6, 50)
        assert abs(weight - 0.012) < 1e-9


# --- 6.5 速限缺失 test ---


class TestComputeBaseWeightDefaultSpeed:
    def test_zero_speed_uses_default(self):
        """速限為 0 時應使用預設 40 km/h。"""
        assert compute_base_weight(1.0, 0) == 1.0 / 40

    def test_none_speed_uses_default(self):
        """速限為 None 時應使用預設 40 km/h。"""
        assert compute_base_weight(1.0, None) == 1.0 / 40

    def test_negative_speed_uses_default(self):
        """速限為負數時應使用預設 40 km/h。"""
        assert compute_base_weight(1.0, -10) == 1.0 / 40


# --- Integration: parse_road_network ---


class TestParseRoadNetwork:
    def test_end_to_end_parsing(self):
        network = parse_road_network(SAMPLE_SECTIONS)
        # RS001 終點 == RS002 起點，所以應有 3 個獨立 node
        assert len(network.nodes) == 3
        assert len(network.edges) == 2

    def test_edge_weight_values(self):
        network = parse_road_network(SAMPLE_SECTIONS)
        # RS001: 0.6km / 50kmh
        assert abs(network.edges[0].base_weight - 0.6 / 50) < 1e-9
        # RS002: 0.8km / 60kmh
        assert abs(network.edges[1].base_weight - 0.8 / 60) < 1e-9

    def test_haversine_sanity(self):
        """兩點間距離應合理。"""
        a = Coord(22.620, 120.300)
        b = Coord(22.625, 120.305)
        dist = haversine_m(a, b)
        assert 600 < dist < 800  # 約 700m
