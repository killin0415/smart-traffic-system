"""Speed camera CSV parsing + snap-to-edge tests."""

from __future__ import annotations

import tempfile
from pathlib import Path

from src.db.models import TrafficEdge
from src.db.speed_camera import ParsedCamera, parse_speed_cameras, snap_camera_to_edge


def _write_csv(rows: list[dict], fieldnames: list[str]) -> Path:
    import csv

    f = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, encoding="utf-8", newline="")
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    f.close()
    return Path(f.name)


class TestParseSpeedCameras:
    def test_parses_kaohsiung_rows(self):
        path = _write_csv(
            rows=[
                {"CityName": "高雄市", "Latitude": "22.625", "Longitude": "120.305", "SpeedLimit": "50", "Address": "中山路100號", "Direction": "北"},
                {"CityName": "高雄市", "Latitude": "22.630", "Longitude": "120.310", "SpeedLimit": "60", "Address": "建國路", "Direction": "南"},
            ],
            fieldnames=["CityName", "Latitude", "Longitude", "SpeedLimit", "Address", "Direction"],
        )
        cameras = parse_speed_cameras(path)
        assert len(cameras) == 2
        assert cameras[0].latitude == 22.625
        assert cameras[0].speed_limit == 50
        assert cameras[0].direction == "北"

    def test_filters_out_non_kaohsiung(self):
        path = _write_csv(
            rows=[
                {"CityName": "高雄市", "Latitude": "22.625", "Longitude": "120.305", "SpeedLimit": "50", "Address": "X", "Direction": ""},
                {"CityName": "台北市", "Latitude": "25.03", "Longitude": "121.56", "SpeedLimit": "40", "Address": "Y", "Direction": ""},
                {"CityName": "新北市", "Latitude": "25.01", "Longitude": "121.45", "SpeedLimit": "40", "Address": "Z", "Direction": ""},
            ],
            fieldnames=["CityName", "Latitude", "Longitude", "SpeedLimit", "Address", "Direction"],
        )
        cameras = parse_speed_cameras(path)
        assert len(cameras) == 1
        assert cameras[0].address == "X"

    def test_skips_rows_with_invalid_coords(self):
        path = _write_csv(
            rows=[
                {"CityName": "高雄市", "Latitude": "", "Longitude": "", "SpeedLimit": "50", "Address": "bad"},
                {"CityName": "高雄市", "Latitude": "22.625", "Longitude": "120.305", "SpeedLimit": "50", "Address": "good"},
            ],
            fieldnames=["CityName", "Latitude", "Longitude", "SpeedLimit", "Address"],
        )
        cameras = parse_speed_cameras(path)
        assert len(cameras) == 1
        assert cameras[0].address == "good"

    def test_missing_csv_returns_empty_list(self):
        assert parse_speed_cameras(Path("/does/not/exist.csv")) == []

    def test_chinese_column_names(self):
        """Government datasets frequently use Chinese headers."""
        path = _write_csv(
            rows=[
                {"縣市": "高雄市", "緯度": "22.625", "經度": "120.305", "速限": "50", "地點": "中山路一號", "方向": "南"},
            ],
            fieldnames=["縣市", "緯度", "經度", "速限", "地點", "方向"],
        )
        cameras = parse_speed_cameras(path)
        assert len(cameras) == 1
        assert cameras[0].address == "中山路一號"
        assert cameras[0].speed_limit == 50

    def test_kaohsiung_open_data_schema(self):
        """Kaohsiung 三民區 科技執法 CSV: 座標緯度/座標經度/測照地點/測照方向/測照型式."""
        path = _write_csv(
            rows=[
                {"Seq": "1", "測照地點": "民族一路268號前", "測照方向": "南向北", "行政區": "三民",
                 "測照型式": "超速", "座標緯度": "22.649855", "座標經度": "120.314951"},
                {"Seq": "2", "測照地點": "某路口", "測照方向": "北向南", "行政區": "三民",
                 "測照型式": "闖紅燈", "座標緯度": "22.64", "座標經度": "120.31"},
                {"Seq": "3", "測照地點": "某路口2", "測照方向": "南向北", "行政區": "三民",
                 "測照型式": "闖紅燈兼超速", "座標緯度": "22.647351", "座標經度": "120.31487"},
                {"Seq": "4", "測照地點": "違左路口", "測照方向": "東向西", "行政區": "三民",
                 "測照型式": "違左", "座標緯度": "22.637", "座標經度": "120.337"},
            ],
            fieldnames=["Seq", "測照地點", "測照方向", "行政區", "測照型式", "座標緯度", "座標經度"],
        )
        cameras = parse_speed_cameras(path)
        # Only 超速 and 闖紅燈兼超速 rows are kept.
        assert len(cameras) == 2
        assert {c.address for c in cameras} == {"民族一路268號前", "某路口2"}
        # No speed-limit column ⇒ all rows fall back to DEFAULT_SPEED_LIMIT_KMH.
        assert all(c.speed_limit == 50 for c in cameras)
        # Direction column is 測照方向.
        assert cameras[0].direction in {"南向北", "南向北"}


class TestSeedCamerasRealCSV:
    """Smoke test: the actual data/speed_cameras.csv file parses to >0 usable rows."""

    def test_real_file_has_speed_enforcement_rows(self):
        from src.db.speed_camera import DEFAULT_CSV_PATH

        if not DEFAULT_CSV_PATH.exists():
            # Skip silently if user has not placed the CSV yet.
            return
        cameras = parse_speed_cameras(DEFAULT_CSV_PATH)
        assert len(cameras) > 0
        # Every kept row must carry a non-zero coordinate and default-or-real speed limit.
        for c in cameras:
            assert 22.0 < c.latitude < 23.5
            assert 120.0 < c.longitude < 121.0
            assert c.speed_limit > 0


class TestSnapCameraToEdge:
    def test_picks_nearest_edge(self):
        """Camera should snap to the edge whose endpoints are closest."""
        # Two edges, one near (1,2), one far (3,4).
        edges = [
            TrafficEdge(id=10, source_node_id=1, target_node_id=2, road_name="near", length_km=0.5, speed_limit_kmh=50, base_weight=0.01),
            TrafficEdge(id=20, source_node_id=3, target_node_id=4, road_name="far", length_km=0.5, speed_limit_kmh=50, base_weight=0.01),
        ]
        node_coords = {
            1: (22.6200, 120.3000),
            2: (22.6210, 120.3010),
            3: (22.7000, 120.4000),   # far
            4: (22.7010, 120.4010),
        }
        cam = ParsedCamera(latitude=22.6205, longitude=120.3005, direction="", speed_limit=50, address="")
        assert snap_camera_to_edge(cam, edges, node_coords) == 10

    def test_returns_none_on_empty_edges(self):
        cam = ParsedCamera(latitude=22.62, longitude=120.30, direction="", speed_limit=50, address="")
        assert snap_camera_to_edge(cam, [], {}) is None
