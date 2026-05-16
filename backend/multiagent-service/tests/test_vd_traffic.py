"""Pure unit tests for src.agents.vd_traffic — no Docker, no live network, no DB."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.agents import tdx_client, vd_traffic
from src.agents.vd_traffic import (
    VDLaneReading,
    fetch_vd_dynamic,
    insert_vd_readings,
    parse_vd_live_json,
    refresh_vd_cycle,
    run_periodic_vd_refresh,
)


# ---------- Fixtures ----------


SAMPLE_PAYLOAD = {
    "UpdateTime": "2026-05-17T00:06:54+08:00",
    "VDLives": [
        {
            "VDID": "VLRJL00",
            "DataCollectTime": "2026-05-07T13:55:00+08:00",
            "Status": 0,
            "LinkFlows": [
                {
                    "LinkID": "2000200000000A",
                    "Lanes": [
                        {
                            "LaneID": 0,
                            "Speed": 42.5,
                            "Occupancy": 14,
                            "Vehicles": [
                                {"VehicleType": "S", "Volume": 100},
                                {"VehicleType": "M", "Volume": 20},
                            ],
                        },
                        {
                            "LaneID": 1,
                            # -99 / -1 are TDX "no data" sentinels.
                            "Speed": -99,
                            "Occupancy": -1,
                            "Vehicles": [
                                {"VehicleType": "S", "Volume": 0},
                            ],
                        },
                    ],
                }
            ],
        },
        {
            "VDID": "VLRJL01",
            "DataCollectTime": "2026-05-07T13:55:00+08:00",
            "Status": 0,
            "LinkFlows": [
                {
                    "LinkID": "X",
                    "Lanes": [
                        {
                            "LaneID": 0,
                            "Speed": 30.0,
                            "Occupancy": 20.0,
                            "Vehicles": [
                                {"VehicleType": "S", "Volume": 80},
                            ],
                        }
                    ],
                }
            ],
        },
    ],
}


# ---------- Parser tests ----------


class TestParseVDLiveJSON:
    def test_parses_payload_with_multiple_lanes_and_sentinel(self):
        readings = parse_vd_live_json(SAMPLE_PAYLOAD)

        assert len(readings) == 3

        first = readings[0]
        assert isinstance(first, VDLaneReading)
        assert first.vdid == "VLRJL00"
        assert first.lane_no == 0
        assert isinstance(first.lane_no, int)
        assert first.avg_speed == 42.5
        assert first.occupancy == 14.0
        # Volume summed across vehicle types: 100 + 20 = 120.
        assert first.volume == 120
        assert first.ts.tzinfo is not None

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

    def test_empty_dict_returns_empty_list(self):
        assert parse_vd_live_json({}) == []

    def test_none_payload_returns_empty_list(self):
        assert parse_vd_live_json(None) == []  # type: ignore[arg-type]

    def test_row_missing_vdid_or_ts_is_skipped(self):
        payload = {
            "VDLives": [
                {
                    # Missing VDID → skipped.
                    "DataCollectTime": "2026-05-07T13:55:00+08:00",
                    "LinkFlows": [{"Lanes": [{"LaneID": 0, "Speed": 30}]}],
                },
                {
                    "VDID": "OK01",
                    "DataCollectTime": "2026-05-07T13:55:00+08:00",
                    "LinkFlows": [{"Lanes": [{"LaneID": 0, "Speed": 20}]}],
                },
            ]
        }
        readings = parse_vd_live_json(payload)
        assert len(readings) == 1
        assert readings[0].vdid == "OK01"

    def test_falls_back_to_flat_volume_when_vehicles_missing(self):
        payload = {
            "VDLives": [
                {
                    "VDID": "X",
                    "DataCollectTime": "2026-05-07T13:55:00+08:00",
                    "LinkFlows": [
                        {
                            "Lanes": [
                                {"LaneID": 0, "Speed": 60.0, "Occupancy": 5, "Volume": 88}
                            ]
                        }
                    ],
                }
            ]
        }
        readings = parse_vd_live_json(payload)
        assert len(readings) == 1
        assert readings[0].volume == 88


# ---------- HTTP fetch tests ----------


class TestFetchVDDynamic:
    @pytest.fixture(autouse=True)
    def _stub_token(self, monkeypatch):
        """All fetch tests run against a stub access token to skip OAuth."""
        monkeypatch.setattr(
            vd_traffic, "get_access_token", AsyncMock(return_value="fake-token")
        )

    @pytest.mark.asyncio
    async def test_fetch_uses_mock_transport_and_returns_parsed_list(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers.get("Authorization")
            return httpx.Response(200, json=SAMPLE_PAYLOAD)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            readings = await fetch_vd_dynamic(client=client, url="http://test.local/VD")

        assert captured["auth"] == "Bearer fake-token"
        assert len(readings) == 3
        assert readings[0].vdid == "VLRJL00"

    @pytest.mark.asyncio
    async def test_fetch_raises_on_http_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await fetch_vd_dynamic(client=client, url="http://test.local/VD")


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
        call_count = {"n": 0}
        two_cycles_done = asyncio.Event()

        async def _fake_cycle(graph, weight_provider, session_factory):
            call_count["n"] += 1
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
                await asyncio.wait_for(two_cycles_done.wait(), timeout=2.0)
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

            assert call_count["n"] >= 2, f"expected >= 2 cycles, got {call_count['n']}"


# ---------- TDX OAuth client tests ----------


class TestTdxClient:
    @pytest.fixture(autouse=True)
    def _reset_cache(self, monkeypatch):
        tdx_client.reset_token_cache()
        monkeypatch.setenv("TDX_CLIENT_ID", "cid")
        monkeypatch.setenv("TDX_CLIENT_SECRET", "csec")
        yield
        tdx_client.reset_token_cache()

    @pytest.mark.asyncio
    async def test_fetches_token_and_caches(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(
                200, json={"access_token": "tok-123", "expires_in": 3600}
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            t1 = await tdx_client.get_access_token(client)
            t2 = await tdx_client.get_access_token(client)

        assert t1 == "tok-123"
        assert t2 == "tok-123"
        assert calls["n"] == 1, "second call should hit cache, not the network"

    @pytest.mark.asyncio
    async def test_raises_when_credentials_missing(self, monkeypatch):
        monkeypatch.delenv("TDX_CLIENT_ID", raising=False)
        monkeypatch.delenv("TDX_CLIENT_SECRET", raising=False)
        with pytest.raises(RuntimeError, match="TDX_CLIENT_ID"):
            await tdx_client.get_access_token()
