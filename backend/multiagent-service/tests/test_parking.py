"""Pure unit tests for src.agents.parking + query_parking_near_destination.

No real DB, no real network.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.agents.parking import (
    ParkingReading,
    fetch_parking_availability,
    insert_parking_readings,
    parse_parking_payload,
    run_periodic_parking_refresh,
)
from src.agents.routing import query_parking_near_destination


# ---------- parse_parking_payload ----------


def test_parse_payload_happy_path_data_taipei_shape():
    """Happy-path with a 'FareInfo' dict so the buggy precedence still resolves
    `availablecar`. See test_parse_payload_bug_availablecar_dropped_without_fareinfo
    for the production bug.
    """
    payload = {
        "result": {
            "limit": 1000,
            "offset": 0,
            "count": 2,
            "results": [
                {
                    "id": "1",
                    "name": "Lot One",
                    "totalcar": "200",
                    "totalmot": "10",
                    "availablecar": "47",
                    "availablemot": "3",
                    # Workaround: presence of FareInfo dict satisfies the
                    # `if isinstance(row.get("FareInfo"), dict)` gate that
                    # currently shadows the entire avail_car expression.
                    "FareInfo": {},
                },
                {
                    "id": "2",
                    "name": "Lot Two",
                    "availablecar": "0",
                    "availablemot": "0",
                    "FareInfo": {},
                },
            ],
        }
    }
    out = parse_parking_payload(payload)
    assert len(out) == 2
    assert out[0].lot_id == 1
    assert out[0].available_car == 47
    assert out[0].available_motor == 3
    assert out[1].lot_id == 2
    assert out[1].available_car == 0
    assert out[1].available_motor == 0


def test_parse_payload_availablecar_works_without_fareinfo():
    """Regression guard for the operator-precedence bug previously in
    parse_parking_payload.

    The earlier code parsed as `(a or b or c) if isinstance(...) else None`,
    so when the row had `availablecar` directly (no `FareInfo` wrapper) the
    value was silently dropped. Fix wraps the FareInfo branch in parens so
    the `or` chain short-circuits on `availablecar` first.
    """
    payload = {
        "result": {
            "results": [
                {"id": "1", "availablecar": "47", "availablemot": "3"},
            ]
        }
    }
    out = parse_parking_payload(payload)
    assert len(out) == 1
    assert out[0].available_car == 47
    assert out[0].available_motor == 3


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"result": {}},
        {"results": "not a list"},
        {"result": {"results": "not a list"}},
        {"result": {"results": ["not a dict", 42, None]}},
        "not even a dict",
        None,
    ],
)
def test_parse_payload_bad_shapes_returns_empty(payload):
    assert parse_parking_payload(payload) == []  # type: ignore[arg-type]


def test_parse_payload_skips_rows_with_non_int_id():
    payload = {
        "result": {
            "results": [
                {"id": "abc", "availablecar": "5"},
                {"id": None, "availablecar": "10"},
                {"id": "42", "availablecar": "1"},
            ]
        }
    }
    out = parse_parking_payload(payload)
    assert len(out) == 1
    assert out[0].lot_id == 42


def test_parse_payload_treats_negative_availablecar_as_none():
    payload = {
        "result": {
            "results": [
                {"id": "1", "availablecar": "-1", "availablemot": "-2"},
            ]
        }
    }
    out = parse_parking_payload(payload)
    assert len(out) == 1
    assert out[0].available_car is None
    assert out[0].available_motor is None


def test_parse_payload_uses_provided_timestamp():
    ts = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    payload = {"result": {"results": [{"id": "5", "availablecar": "1"}]}}
    out = parse_parking_payload(payload, ts=ts)
    assert out[0].ts == ts


def test_parse_payload_accepts_top_level_results_key():
    """Fallback shape: payload['results'] (no 'result' wrapper).

    NOTE: Same FareInfo precedence bug as the happy-path test — we add
    `FareInfo: {}` to keep available_car parsing intact. See
    test_parse_payload_bug_availablecar_dropped_without_fareinfo.
    """
    payload = {"results": [{"id": "9", "availablecar": "12", "FareInfo": {}}]}
    out = parse_parking_payload(payload)
    assert len(out) == 1
    assert out[0].lot_id == 9
    assert out[0].available_car == 12


# ---------- fetch_parking_availability ----------


@pytest.mark.asyncio
async def test_fetch_parking_availability_passes_through_parsed_list():
    # FareInfo workaround for the parse_parking_payload precedence bug —
    # see test_parse_payload_bug_availablecar_dropped_without_fareinfo.
    payload = {
        "result": {
            "results": [
                {"id": "1", "availablecar": "5", "availablemot": "1", "FareInfo": {}},
                {"id": "2", "availablecar": "10", "FareInfo": {}},
            ]
        }
    }
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value=payload)

    client = MagicMock()
    client.get = AsyncMock(return_value=response)

    out = await fetch_parking_availability(client=client, url="http://example/test")

    client.get.assert_awaited_once_with("http://example/test")
    response.raise_for_status.assert_called_once()
    assert len(out) == 2
    assert out[0].lot_id == 1
    assert out[0].available_car == 5


@pytest.mark.asyncio
async def test_fetch_parking_availability_propagates_http_errors():
    response = MagicMock()
    response.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("boom", request=MagicMock(), response=MagicMock())
    )
    response.json = MagicMock(return_value={})

    client = MagicMock()
    client.get = AsyncMock(return_value=response)

    with pytest.raises(httpx.HTTPStatusError):
        await fetch_parking_availability(client=client, url="http://x")


# ---------- insert_parking_readings ----------


@pytest.mark.asyncio
async def test_insert_parking_readings_empty_returns_zero_no_execute():
    session = MagicMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()

    n = await insert_parking_readings(session, [])

    assert n == 0
    session.execute.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_insert_parking_readings_executes_and_commits_for_rows():
    session = MagicMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()

    ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
    readings = [
        ParkingReading(ts=ts, lot_id=1, available_car=10, available_motor=2),
        ParkingReading(ts=ts, lot_id=2, available_car=None, available_motor=None),
    ]
    n = await insert_parking_readings(session, readings)

    assert n == 2
    session.execute.assert_awaited_once()
    session.commit.assert_awaited_once()


# ---------- run_periodic_parking_refresh ----------


@pytest.mark.asyncio
async def test_run_periodic_parking_refresh_loops_and_survives_errors():
    """Patch fetch to raise on iteration 1 (HTTPError caught), return data on
    iteration 2 (insert called), and cancel on iteration 3."""
    session = MagicMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()

    class _Ctx:
        async def __aenter__(self_inner):
            return session

        async def __aexit__(self_inner, exc_type, exc, tb):
            return False

    def factory():
        return _Ctx()

    ts = datetime(2025, 1, 1, tzinfo=timezone.utc)

    iteration = {"i": 0}

    async def _fake_fetch():
        iteration["i"] += 1
        if iteration["i"] == 1:
            raise httpx.HTTPError("boom")
        if iteration["i"] == 2:
            return [ParkingReading(ts=ts, lot_id=1, available_car=5, available_motor=0)]
        # Iteration 3+: empty list (still completes loop body, no insert).
        return []

    sleep_calls = {"n": 0}

    async def _fast_sleep(_secs):
        sleep_calls["n"] += 1
        if sleep_calls["n"] >= 3:
            raise asyncio.CancelledError

    with patch("src.agents.parking.fetch_parking_availability", new=AsyncMock(side_effect=_fake_fetch)), \
         patch("src.agents.parking.asyncio.sleep", new=_fast_sleep):
        with pytest.raises(asyncio.CancelledError):
            await run_periodic_parking_refresh(factory, interval_seconds=300)

    # Three iterations executed.
    assert iteration["i"] >= 3
    # Insert only happened in iteration 2.
    session.execute.assert_awaited_once()
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_periodic_parking_refresh_propagates_cancellation():
    """asyncio.CancelledError escapes the except block (re-raised)."""
    factory = MagicMock()  # never used: fetch raises immediately.

    async def _cancelled():
        raise asyncio.CancelledError

    with patch("src.agents.parking.fetch_parking_availability", new=AsyncMock(side_effect=_cancelled)):
        with pytest.raises(asyncio.CancelledError):
            await run_periodic_parking_refresh(factory, interval_seconds=1)


# ---------- query_parking_near_destination ----------


def _parking_row(**kwargs):
    m = MagicMock()
    for k, v in kwargs.items():
        setattr(m, k, v)
    return m


@pytest.mark.asyncio
async def test_query_parking_near_destination_serialises_rows():
    rows = [
        _parking_row(
            id=1,
            name="Lot A",
            address="addr A",
            latitude=25.05,
            longitude=121.55,
            available_car=20,
            distance_m=123.456789,
        ),
        _parking_row(
            id=2,
            name="Lot B",
            address="addr B",
            latitude=25.06,
            longitude=121.56,
            available_car=5,
            distance_m=999.99,
        ),
    ]
    result = MagicMock()
    result.all = MagicMock(return_value=rows)

    session = MagicMock()
    session.execute = AsyncMock(return_value=result)

    out = await query_parking_near_destination(session, lat=25.05, lng=121.55)

    assert isinstance(out, list)
    assert len(out) == 2
    assert out[0] == {
        "id": 1,
        "name": "Lot A",
        "address": "addr A",
        "latitude": 25.05,
        "longitude": 121.55,
        "available_car": 20,
        "distance_m": 123.5,  # rounded to 1 decimal
    }
    assert out[1]["distance_m"] == 1000.0  # round(999.99, 1)
    # Ensure id is int
    assert isinstance(out[0]["id"], int)
    assert isinstance(out[0]["available_car"], int)


@pytest.mark.asyncio
async def test_query_parking_near_destination_empty_returns_empty_list():
    result = MagicMock()
    result.all = MagicMock(return_value=[])

    session = MagicMock()
    session.execute = AsyncMock(return_value=result)

    out = await query_parking_near_destination(session, lat=25.05, lng=121.55)
    assert out == []


@pytest.mark.asyncio
async def test_query_parking_near_destination_default_min_avail_is_10():
    """The :min_avail bound parameter defaults to 10 and is delegated to SQL
    (no Python-side filter on rows)."""
    # Row with available_car well below default 10 — must still be returned
    # (SQL would filter, but the function does not).
    rows = [
        _parking_row(
            id=1,
            name="Tiny",
            address="x",
            latitude=25.0,
            longitude=121.5,
            available_car=2,  # < 10
            distance_m=10.0,
        ),
    ]
    result = MagicMock()
    result.all = MagicMock(return_value=rows)
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)

    out = await query_parking_near_destination(session, lat=25.05, lng=121.55)

    # No Python-side filter: row is returned despite available_car < 10.
    assert len(out) == 1

    # Inspect the bound parameters of the SQL execute call.
    session.execute.assert_awaited_once()
    args, kwargs = session.execute.await_args
    # Second positional arg is the bound-parameter dict.
    params = args[1] if len(args) > 1 else kwargs.get("parameters") or kwargs.get("params") or {}
    assert params.get("min_avail") == 10
    assert params.get("lat") == 25.05
    assert params.get("lng") == 121.55
    # radius_km=1.0 default → radius_m=1000
    assert params.get("radius_m") == pytest.approx(1000.0)
    # top default 5
    assert params.get("top_n") == 5


@pytest.mark.asyncio
async def test_query_parking_near_destination_passes_through_overrides():
    result = MagicMock()
    result.all = MagicMock(return_value=[])
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)

    await query_parking_near_destination(
        session, lat=25.0, lng=121.5, radius_km=2.5, top=7, min_available=3
    )

    args, _ = session.execute.await_args
    params = args[1]
    assert params["min_avail"] == 3
    assert params["radius_m"] == pytest.approx(2500.0)
    assert params["top_n"] == 7
