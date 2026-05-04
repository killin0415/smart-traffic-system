"""VD live data parsing, healthy filter, edge aggregation, and snap-to-edge tests."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from src.agents.traffic import (
    _filter_healthy,
    aggregate_edge_speeds,
    fetch_live_vd_data,
)
from src.db.models import TrafficEdge
from src.db.vd_sensor import ParsedVD, _parse_vd_record, snap_vd_to_edge


# ---------- _filter_healthy ----------


class TestFilterHealthy:
    def test_drops_negative_speed(self):
        lanes = [
            {"Speed": 40.0, "ErrorType": ""},
            {"Speed": -99.0, "ErrorType": ""},
            {"Speed": 0, "ErrorType": ""},
        ]
        assert _filter_healthy(lanes) == [40.0]

    def test_drops_error_type(self):
        lanes = [
            {"Speed": 35.0, "ErrorType": ""},
            {"Speed": 50.0, "ErrorType": "diag202"},
        ]
        assert _filter_healthy(lanes) == [35.0]

    def test_returns_empty_for_no_healthy(self):
        lanes = [
            {"Speed": -99, "ErrorType": ""},
            {"Speed": 30, "ErrorType": "diag203"},
        ]
        assert _filter_healthy(lanes) == []

    def test_handles_missing_keys(self):
        lanes = [{}, {"Speed": 25}]
        assert _filter_healthy(lanes) == [25.0]


# ---------- fetch_live_vd_data parse ----------


VDLIVES_PAYLOAD = {
    "VDLives": [
        {
            "VDID": "VD-1",
            "LinkFlows": [
                {
                    "LinkID": "L1",
                    "Lanes": [
                        {"Speed": 40, "ErrorType": ""},
                        {"Speed": -99, "ErrorType": ""},
                        {"Speed": 50, "ErrorType": "diag202"},
                    ],
                }
            ],
        },
        {
            "VDID": "VD-2",
            "LinkFlows": [
                {"LinkID": "L2", "Lanes": [{"Speed": 60, "ErrorType": ""}]},
                {"LinkID": "L3", "Lanes": [{"Speed": 55, "ErrorType": ""}]},
            ],
        },
        {"VDID": "VD-3", "LinkFlows": []},
    ]
}


class TestFetchLiveVDData:
    @pytest.mark.asyncio
    async def test_parses_vdlives_payload(self):
        async def fake_get_token(*args, **kwargs):
            return "tok"

        mock_response = AsyncMock()
        mock_response.raise_for_status = lambda: None
        mock_response.json = lambda: VDLIVES_PAYLOAD

        with patch("src.agents.traffic.get_access_token", side_effect=fake_get_token):
            with patch("httpx.AsyncClient") as MockClient:
                instance = MockClient.return_value.__aenter__.return_value
                instance.get = AsyncMock(return_value=mock_response)
                result = await fetch_live_vd_data()

        assert result == {"VD-1": [40.0], "VD-2": [60.0, 55.0], "VD-3": []}


# ---------- aggregate_edge_speeds ----------


class TestAggregateEdgeSpeeds:
    def test_single_vd_single_edge(self):
        vd_data = {"VD-A": [40.0, 50.0]}
        edge_map = {"VD-A": (1, "S-1", 60)}
        section, stats = aggregate_edge_speeds(vd_data, edge_map)
        assert len(section) == 1
        assert section[0]["edge_id"] == 1
        assert section[0]["tdx_section_id"] == "S-1"
        assert section[0]["speed_limit_kmh"] == 60
        assert section[0]["travel_speed"] == 45.0
        assert stats == {"vds_total": 1, "vds_healthy": 1, "edges_updated": 1}

    def test_multi_vd_same_edge_avg(self):
        # V1 and V2 both on edge 1.
        vd_data = {"V1": [40.0], "V2": [60.0]}
        edge_map = {"V1": (1, "S-1", 50), "V2": (1, "S-1", 50)}
        section, stats = aggregate_edge_speeds(vd_data, edge_map)
        assert len(section) == 1
        assert section[0]["travel_speed"] == 50.0
        assert stats["vds_healthy"] == 2
        assert stats["edges_updated"] == 1

    def test_all_faulty_edge_skipped(self):
        # VD has no healthy lane readings — edge should not appear.
        vd_data = {"V1": []}
        edge_map = {"V1": (1, "S-1", 50)}
        section, stats = aggregate_edge_speeds(vd_data, edge_map)
        assert section == []
        assert stats["vds_healthy"] == 0
        assert stats["edges_updated"] == 0

    def test_partial_faulty_uses_healthy_only(self):
        # V1 healthy on edge 1 (40), V2 faulty on edge 1 (no readings) ⇒ avg = 40
        vd_data = {"V1": [40.0], "V2": []}
        edge_map = {"V1": (1, "S-1", 50), "V2": (1, "S-1", 50)}
        section, stats = aggregate_edge_speeds(vd_data, edge_map)
        assert len(section) == 1
        assert section[0]["travel_speed"] == 40.0
        assert stats["vds_healthy"] == 1
        assert stats["edges_updated"] == 1

    def test_unknown_vd_ignored(self):
        # V1 is in vd_data but not in edge_map — must be skipped.
        vd_data = {"V1": [40.0], "V2": [50.0]}
        edge_map = {"V2": (1, "S-1", 50)}
        section, stats = aggregate_edge_speeds(vd_data, edge_map)
        assert len(section) == 1
        assert section[0]["edge_id"] == 1
        assert stats["vds_total"] == 2
        assert stats["vds_healthy"] == 1


# ---------- snap_vd_to_edge ----------


class TestSnapVDToEdge:
    def test_picks_nearest_edge(self):
        edges = [
            TrafficEdge(id=10, source_node_id=1, target_node_id=2, road_name="near", length_km=0.5, speed_limit_kmh=50, base_weight=0.01),
            TrafficEdge(id=20, source_node_id=3, target_node_id=4, road_name="far", length_km=0.5, speed_limit_kmh=50, base_weight=0.01),
        ]
        node_coords = {
            1: (22.6200, 120.3000),
            2: (22.6210, 120.3010),
            3: (22.7000, 120.4000),
            4: (22.7010, 120.4010),
        }
        vd = ParsedVD(vdid="VD-X", latitude=22.6205, longitude=120.3005, link_id=None, road_section_id=None)
        assert snap_vd_to_edge(vd, edges, node_coords) == 10

    def test_returns_none_on_empty_edges(self):
        vd = ParsedVD(vdid="VD-X", latitude=22.62, longitude=120.30, link_id=None, road_section_id=None)
        assert snap_vd_to_edge(vd, [], {}) is None


# ---------- _parse_vd_record ----------


class TestParseVDRecord:
    def test_extracts_position_and_link(self):
        raw = {
            "VDID": "VD-9",
            "PositionLat": 22.62,
            "PositionLon": 120.30,
            "DetectionLinks": [{"LinkID": "LINK-9"}],
            "RoadSection": {"Start": "RS-9"},
        }
        vd = _parse_vd_record(raw)
        assert vd is not None
        assert vd.vdid == "VD-9"
        assert vd.latitude == 22.62
        assert vd.link_id == "LINK-9"
        assert vd.road_section_id == "RS-9"

    def test_returns_none_when_position_missing(self):
        assert _parse_vd_record({"VDID": "VD-1"}) is None

    def test_returns_none_when_vdid_missing(self):
        assert _parse_vd_record({"PositionLat": 22.6, "PositionLon": 120.3}) is None
