"""Pure unit tests for src.agents.vd_traffic — no Docker, no live network, no DB."""

from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.agents import vd_traffic
from src.agents.vd_traffic import (
    VDLaneReading,
    fetch_vd_dynamic,
    insert_vd_readings,
    parse_vd_dynamic_xml,
    refresh_vd_cycle,
    run_periodic_vd_refresh,
)


# ---------- Fixtures ----------


SAMPLE_XML_PLAIN = """<?xml version='1.0' encoding='UTF-8'?>
<VDLiveList>
  <VDLive>
    <VDID>VLRJL00</VDID>
    <DataCollectTime>2026-05-07T13:55:00+08:00</DataCollectTime>
    <LinkFlows>
      <LinkFlow>
        <Lanes>
          <Lane>
            <LaneID>0</LaneID>
            <Speed>42.5</Speed>
            <Occupancy>14</Occupancy>
            <Volume>120</Volume>
          </Lane>
          <Lane>
            <LaneID>1</LaneID>
            <Speed>-99</Speed>
            <Occupancy>-1</Occupancy>
            <Volume>0</Volume>
          </Lane>
        </Lanes>
      </LinkFlow>
    </LinkFlows>
  </VDLive>
  <VDLive>
    <VDID>VLRJL01</VDID>
    <DataCollectTime>2026-05-07T13:55:00+08:00</DataCollectTime>
    <LinkFlows>
      <LinkFlow>
        <Lanes>
          <Lane>
            <LaneID>0</LaneID>
            <Speed>30.0</Speed>
            <Occupancy>20</Occupancy>
            <Volume>80</Volume>
          </Lane>
        </Lanes>
      </LinkFlow>
    </LinkFlows>
  </VDLive>
</VDLiveList>
"""


SAMPLE_XML_NAMESPACED = """<?xml version='1.0' encoding='UTF-8'?>
<VDLiveList xmlns="http://traffic.transportdata.tw/standard/traffic/schema/">
  <VDLive>
    <VDID>VNS001</VDID>
    <DataCollectTime>2026-05-07T13:55:00+08:00</DataCollectTime>
    <LinkFlows>
      <LinkFlow>
        <Lanes>
          <Lane>
            <LaneID>2</LaneID>
            <Speed>55.0</Speed>
            <Occupancy>10</Occupancy>
            <Volume>200</Volume>
          </Lane>
        </Lanes>
      </LinkFlow>
    </LinkFlows>
  </VDLive>
</VDLiveList>
"""


# ---------- Parser tests ----------


class TestParseVDDynamicXML:
    def test_parses_plain_xml_with_multiple_lanes_and_sentinel(self):
        readings = parse_vd_dynamic_xml(SAMPLE_XML_PLAIN)

        # 2 VDLive rows: one with 2 lanes, one with 1 lane → 3 readings.
        assert len(readings) == 3

        first = readings[0]
        assert isinstance(first, VDLaneReading)
        assert first.vdid == "VLRJL00"
        assert first.lane_no == 0
        assert isinstance(first.lane_no, int)
        assert first.avg_speed == 42.5
        assert first.occupancy == 14.0
        assert first.volume == 120
        # Timezone-aware datetime expected.
        assert first.ts.tzinfo is not None

        # Sentinel -99 / -1 must collapse to None.
        sentinel_lane = readings[1]
        assert sentinel_lane.vdid == "VLRJL00"
        assert sentinel_lane.lane_no == 1
        assert sentinel_lane.avg_speed is None
        assert sentinel_lane.occupancy is None
        # Volume of 0 is valid (not negative); should be 0.
        assert sentinel_lane.volume == 0

        third = readings[2]
        assert third.vdid == "VLRJL01"
        assert third.lane_no == 0
        assert third.avg_speed == 30.0

    def test_empty_string_returns_empty_list(self):
        assert parse_vd_dynamic_xml("") == []

    def test_malformed_xml_raises_parse_error(self):
        with pytest.raises(ET.ParseError):
            parse_vd_dynamic_xml("<not-valid><<<")

    def test_namespaced_xml_is_parsed(self):
        readings = parse_vd_dynamic_xml(SAMPLE_XML_NAMESPACED)
        assert len(readings) == 1
        r = readings[0]
        assert r.vdid == "VNS001"
        assert r.lane_no == 2
        assert r.avg_speed == 55.0
        assert r.volume == 200

    def test_row_missing_vdid_or_ts_is_skipped(self):
        xml_text = """<?xml version='1.0' encoding='UTF-8'?>
<VDLiveList>
  <VDLive>
    <DataCollectTime>2026-05-07T13:55:00+08:00</DataCollectTime>
    <LinkFlows><LinkFlow><Lanes><Lane><LaneID>0</LaneID><Speed>30</Speed></Lane></Lanes></LinkFlow></LinkFlows>
  </VDLive>
  <VDLive>
    <VDID>OK01</VDID>
    <DataCollectTime>2026-05-07T13:55:00+08:00</DataCollectTime>
    <LinkFlows><LinkFlow><Lanes><Lane><LaneID>0</LaneID><Speed>20</Speed></Lane></Lanes></LinkFlow></LinkFlows>
  </VDLive>
</VDLiveList>
"""
        readings = parse_vd_dynamic_xml(xml_text)
        assert len(readings) == 1
        assert readings[0].vdid == "OK01"


# ---------- HTTP fetch tests ----------


class TestFetchVDDynamic:
    @pytest.mark.asyncio
    async def test_fetch_uses_mock_transport_and_returns_parsed_list(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=SAMPLE_XML_PLAIN)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            readings = await fetch_vd_dynamic(client=client, url="http://test.local/VD.xml")

        assert len(readings) == 3
        assert readings[0].vdid == "VLRJL00"

    @pytest.mark.asyncio
    async def test_fetch_raises_on_http_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await fetch_vd_dynamic(client=client, url="http://test.local/VD.xml")

    @pytest.mark.asyncio
    async def test_fetch_creates_and_closes_own_client_when_none_passed(self):
        """When no client is passed, fetch should construct + close one of its own."""
        captured = {}

        class _FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                captured["constructed"] = True
                self.closed = False

            async def get(self, url):
                captured["url"] = url
                # Bind a request to the response so raise_for_status() works.
                req = httpx.Request("GET", url)
                return httpx.Response(200, text=SAMPLE_XML_PLAIN, request=req)

            async def aclose(self):
                self.closed = True
                captured["closed"] = True

        with patch.object(vd_traffic.httpx, "AsyncClient", _FakeAsyncClient):
            readings = await fetch_vd_dynamic(url="http://test.local/VD.xml")

        assert captured.get("constructed") is True
        assert captured.get("closed") is True
        assert captured.get("url") == "http://test.local/VD.xml"
        assert len(readings) == 3


# ---------- DB write tests ----------


class TestInsertVDReadings:
    @pytest.mark.asyncio
    async def test_empty_list_returns_zero_and_does_not_call_session(self):
        session = MagicMock()
        session.execute = AsyncMock()
        session.commit = AsyncMock()

        n = await insert_vd_readings(session, [])

        assert n == 0
        session.execute.assert_not_called()
        session.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_empty_list_executes_and_commits_once(self):
        session = MagicMock()
        session.execute = AsyncMock()
        session.commit = AsyncMock()

        readings = [
            VDLaneReading(
                ts=datetime(2026, 5, 7, 13, 55, tzinfo=timezone.utc),
                vdid="VLRJL00",
                lane_no=0,
                avg_speed=42.5,
                volume=120,
                occupancy=14.0,
            ),
            VDLaneReading(
                ts=datetime(2026, 5, 7, 13, 55, tzinfo=timezone.utc),
                vdid="VLRJL00",
                lane_no=1,
                avg_speed=None,
                volume=0,
                occupancy=None,
            ),
        ]

        n = await insert_vd_readings(session, readings)

        assert n == 2
        session.execute.assert_awaited_once()
        session.commit.assert_awaited_once()

        # Verify it's a pg_insert ON CONFLICT DO NOTHING statement.
        called_stmt = session.execute.await_args.args[0]
        rendered = str(called_stmt)
        assert "INSERT INTO vd_reading" in rendered
        assert "ON CONFLICT" in rendered.upper()
        assert "DO NOTHING" in rendered.upper()


# ---------- refresh_vd_cycle tests ----------


class _FakeSessionCtx:
    """async-context manager that yields a mocked session."""

    def __init__(self):
        self.session = MagicMock()
        self.session.execute = AsyncMock()
        self.session.commit = AsyncMock()

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


class TestRefreshVDCycle:
    @pytest.mark.asyncio
    async def test_fetch_failure_returns_error_dict_and_skips_rebuild_apply(self):
        fake_session_ctx = _FakeSessionCtx()
        session_factory = MagicMock(return_value=fake_session_ctx)

        graph = MagicMock()
        weight_provider = MagicMock()
        weight_provider.rebuild = AsyncMock()
        weight_provider.apply_to_graph = MagicMock()

        with patch.object(
            vd_traffic,
            "fetch_vd_dynamic",
            AsyncMock(side_effect=httpx.HTTPError("network down")),
        ):
            result = await refresh_vd_cycle(graph, weight_provider, session_factory)

        assert "error" in result
        assert result["fetched"] == 0
        weight_provider.rebuild.assert_not_called()
        weight_provider.apply_to_graph.assert_not_called()
        # Session factory must not be invoked when fetch failed.
        session_factory.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_fetch_still_rebuilds_and_applies_weights(self):
        fake_session_ctx = _FakeSessionCtx()
        session_factory = MagicMock(return_value=fake_session_ctx)

        graph = MagicMock()
        weight_provider = MagicMock()
        weight_provider.rebuild = AsyncMock()
        weight_provider.apply_to_graph = MagicMock()

        with patch.object(vd_traffic, "fetch_vd_dynamic", AsyncMock(return_value=[])):
            result = await refresh_vd_cycle(graph, weight_provider, session_factory)

        assert result == {"fetched": 0, "inserted": 0}
        weight_provider.rebuild.assert_awaited_once_with(session_factory)
        weight_provider.apply_to_graph.assert_called_once_with(graph)
        # Empty readings → no insert path → session factory should NOT be opened.
        session_factory.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_empty_fetch_inserts_then_rebuilds_then_applies(self):
        fake_session_ctx = _FakeSessionCtx()
        session_factory = MagicMock(return_value=fake_session_ctx)

        graph = MagicMock()
        weight_provider = MagicMock()
        weight_provider.rebuild = AsyncMock()
        weight_provider.apply_to_graph = MagicMock()

        readings = [
            VDLaneReading(
                ts=datetime(2026, 5, 7, 13, 55, tzinfo=timezone.utc),
                vdid="VLRJL00",
                lane_no=0,
                avg_speed=42.5,
                volume=120,
                occupancy=14.0,
            )
        ]

        with patch.object(vd_traffic, "fetch_vd_dynamic", AsyncMock(return_value=readings)):
            result = await refresh_vd_cycle(graph, weight_provider, session_factory)

        assert result == {"fetched": 1, "inserted": 1}
        session_factory.assert_called_once()
        fake_session_ctx.session.execute.assert_awaited_once()
        fake_session_ctx.session.commit.assert_awaited_once()
        weight_provider.rebuild.assert_awaited_once_with(session_factory)
        weight_provider.apply_to_graph.assert_called_once_with(graph)


# ---------- run_periodic_vd_refresh tests ----------


class TestRunPeriodicVDRefresh:
    @pytest.mark.asyncio
    async def test_loops_multiple_cycles_then_cancels_cleanly(self):
        """Verify the loop runs at least 2 cycles, swallows a single cycle exception,
        and re-raises CancelledError when cancelled."""
        call_count = {"n": 0}
        two_cycles_done = asyncio.Event()

        async def _fake_cycle(graph, weight_provider, session_factory):
            call_count["n"] += 1
            # First cycle raises — the loop must survive and keep going.
            if call_count["n"] == 1:
                raise RuntimeError("boom (this should be swallowed)")
            if call_count["n"] >= 2:
                two_cycles_done.set()
            return {"fetched": 0, "inserted": 0}

        with patch.object(vd_traffic, "refresh_vd_cycle", _fake_cycle):
            task = asyncio.create_task(
                run_periodic_vd_refresh(MagicMock(), MagicMock(), MagicMock(), interval_seconds=0)
            )
            try:
                # interval_seconds=0 → real asyncio.sleep(0) just yields to the loop,
                # which keeps the spin tight.
                await asyncio.wait_for(two_cycles_done.wait(), timeout=2.0)
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

            assert call_count["n"] >= 2, f"expected >= 2 cycles, got {call_count['n']}"
