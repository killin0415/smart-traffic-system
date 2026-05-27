"""Pure unit tests for scripts/seed_vd_static.py — no Docker, no live network, no DB.

The script is a PEP-723 self-contained CLI living outside the package
(`<repo>/scripts/seed_vd_static.py`), so we load it via importlib.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


# ---------- Module loader ----------


def _load_seed_vd_static_module():
    """Load scripts/seed_vd_static.py from the repo root as a module."""
    # tests/test_seed_vd_static.py → tests/ → multiagent-service/ → backend/ → repo root
    repo_root = Path(__file__).resolve().parents[3]
    script_path = repo_root / "scripts" / "seed_vd_static.py"
    assert script_path.exists(), f"missing script: {script_path}"

    spec = importlib.util.spec_from_file_location("seed_vd_static_under_test", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["seed_vd_static_under_test"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def seed_mod():
    return _load_seed_vd_static_module()


# ---------- Sample XML fixtures ----------


SAMPLE_XML_TWO_RECORDS = """<?xml version='1.0' encoding='UTF-8'?>
<VDList>
  <VD>
    <VDID>VFULL01</VDID>
    <LinkID>LINK-001</LinkID>
    <RoadName>Civic Blvd</RoadName>
    <RoadClass>2</RoadClass>
    <BiDirectional>1</BiDirectional>
    <RoadDirection>E</RoadDirection>
    <PositionLat>25.0478</PositionLat>
    <PositionLon>121.5170</PositionLon>
  </VD>
  <VD>
    <VDID>VMIN02</VDID>
    <PositionLat>25.0500</PositionLat>
    <PositionLon>121.5200</PositionLon>
  </VD>
</VDList>
"""


SAMPLE_XML_NAMESPACED = """<?xml version='1.0' encoding='UTF-8'?>
<VDList xmlns="http://traffic.transportdata.tw/standard/traffic/schema/">
  <VD>
    <VDID>VNS01</VDID>
    <PositionLat>25.04</PositionLat>
    <PositionLon>121.50</PositionLon>
  </VD>
</VDList>
"""


SAMPLE_XML_MISSING_COORDS = """<?xml version='1.0' encoding='UTF-8'?>
<VDList>
  <VD>
    <VDID>VNOCOORD</VDID>
  </VD>
  <VD>
    <VDID>VOK</VDID>
    <PositionLat>25.04</PositionLat>
    <PositionLon>121.50</PositionLon>
  </VD>
</VDList>
"""


# ---------- parse_vd_static_xml ----------


class TestParseVDStaticXML:
    def test_parses_two_records_with_optional_fields(self, seed_mod):
        records = seed_mod.parse_vd_static_xml(SAMPLE_XML_TWO_RECORDS)

        assert len(records) == 2

        full = records[0]
        assert full.vdid == "VFULL01"
        assert full.link_id == "LINK-001"
        assert full.road_name == "Civic Blvd"
        assert full.road_class == "2"
        assert full.bidirectional is True
        assert full.bearing == "E"
        assert full.latitude == 25.0478
        assert full.longitude == 121.5170

        minimal = records[1]
        assert minimal.vdid == "VMIN02"
        # Optional fields missing → None
        assert minimal.link_id is None
        assert minimal.road_name is None
        assert minimal.road_class is None
        assert minimal.bearing is None
        assert minimal.bidirectional is False
        assert minimal.latitude == 25.0500
        assert minimal.longitude == 121.5200

    def test_empty_string_returns_empty_list(self, seed_mod):
        assert seed_mod.parse_vd_static_xml("") == []

    def test_row_missing_coords_is_skipped(self, seed_mod):
        records = seed_mod.parse_vd_static_xml(SAMPLE_XML_MISSING_COORDS)
        # The coord-less row is dropped, the well-formed one survives.
        assert len(records) == 1
        assert records[0].vdid == "VOK"

    def test_namespaced_xml_is_parsed(self, seed_mod):
        records = seed_mod.parse_vd_static_xml(SAMPLE_XML_NAMESPACED)
        assert len(records) == 1
        r = records[0]
        assert r.vdid == "VNS01"
        assert r.latitude == 25.04
        assert r.longitude == 121.50


# ---------- upsert_vd_records ----------


class TestUpsertVDRecords:
    @pytest.mark.asyncio
    async def test_empty_records_returns_zero_and_does_not_create_engine(self, seed_mod):
        sentinel_engine = MagicMock()
        with patch.object(seed_mod, "create_async_engine", MagicMock(return_value=sentinel_engine)) as mocked:
            n = await seed_mod.upsert_vd_records("postgresql+asyncpg://stub", [])

        assert n == 0
        mocked.assert_not_called()


# ---------- main() ----------


class TestMain:
    def test_main_with_no_records_returns_zero(self, seed_mod, caplog):
        """If fetch yields no records, main() should exit gracefully with 0
        and emit a warning — without trying to construct an engine.

        Sync test (NOT @pytest.mark.asyncio) because main() calls
        asyncio.run() internally and asyncio.run() can't nest in a running loop.
        """

        async def _empty_fetch():
            return []

        # Guard against accidental engine construction.
        engine_factory = MagicMock()

        with patch.object(seed_mod, "fetch_vd_static", _empty_fetch), patch.object(
            seed_mod, "create_async_engine", engine_factory
        ):
            with caplog.at_level("WARNING", logger="seed_vd_static"):
                rc = seed_mod.main([])

        assert rc == 0
        engine_factory.assert_not_called()
        # The script logs "no <VD> records returned — graceful exit".
        assert any("no <VD> records" in rec.message for rec in caplog.records), (
            f"expected warning about empty <VD>; got: {[r.message for r in caplog.records]}"
        )
