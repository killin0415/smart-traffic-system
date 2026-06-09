"""Pure unit tests for `src/db/speed_camera.py`.

No real DB. SQLAlchemy `AsyncSession` is mocked: `session.execute(...)` returns
a `MagicMock` with chained `.scalar_one()` / `.first()` / `.all()` methods,
and `session.execute` itself is an `AsyncMock`.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.db import speed_camera as sc_mod
from src.db.speed_camera import (
    DEFAULT_SPEED_LIMIT_KMH,
    ParsedCamera,
    parse_speed_cameras,
    seed_speed_cameras,
    snap_camera_to_edge,
)


# ---------- parse_speed_cameras ----------


_HEADER = "緯度,經度,速限,拍攝方向,設置地點"


def _write_csv(tmp_path: Path, body: str) -> Path:
    """Write a UTF-8-with-BOM CSV (the parser opens with utf-8-sig)."""
    p = tmp_path / "cams.csv"
    p.write_text(body, encoding="utf-8-sig")
    return p


class TestParseSpeedCameras:
    def test_parses_lat_lng_and_speed_limit(self, tmp_path):
        body = (
            f"{_HEADER}\n"
            "25.04781,121.51699,40,北向,中山北路一段100號\n"
            "25.03285,121.56542,60,東向,信義路五段7號\n"
        )
        path = _write_csv(tmp_path, body)
        cams = parse_speed_cameras(path)
        assert len(cams) == 2
        assert cams[0].latitude == pytest.approx(25.04781)
        assert cams[0].longitude == pytest.approx(121.51699)
        assert cams[0].speed_limit == 40
        assert cams[0].direction == "北向"
        assert cams[0].address == "中山北路一段100號"
        assert cams[1].speed_limit == 60

    def test_blank_speed_limit_uses_default(self, tmp_path):
        body = (
            f"{_HEADER}\n"
            "25.04781,121.51699,,北向,中山北路一段100號\n"
        )
        path = _write_csv(tmp_path, body)
        cams = parse_speed_cameras(path)
        assert len(cams) == 1
        assert cams[0].speed_limit == DEFAULT_SPEED_LIMIT_KMH

    def test_zero_or_negative_speed_limit_uses_default(self, tmp_path):
        body = (
            f"{_HEADER}\n"
            "25.04781,121.51699,0,北向,Loc A\n"
        )
        path = _write_csv(tmp_path, body)
        cams = parse_speed_cameras(path)
        assert cams[0].speed_limit == DEFAULT_SPEED_LIMIT_KMH

    def test_missing_file_returns_empty(self, tmp_path):
        missing = tmp_path / "does_not_exist.csv"
        assert parse_speed_cameras(missing) == []

    def test_unparseable_lat_lng_rows_skipped(self, tmp_path):
        body = (
            f"{_HEADER}\n"
            "abc,xyz,40,北向,bad row\n"
            "25.04,,40,北向,bad row 2\n"  # missing lng
            "25.04781,121.51699,40,北向,good row\n"
        )
        path = _write_csv(tmp_path, body)
        cams = parse_speed_cameras(path)
        assert len(cams) == 1
        assert cams[0].address == "good row"

    def test_no_kaohsiung_filter_taipei_address_retained(self, tmp_path):
        """The new parser has no city filter — Taipei-mentioning addresses pass through."""
        body = (
            f"{_HEADER}\n"
            "25.04781,121.51699,50,北向,台北市中山區中山北路一段100號\n"
        )
        path = _write_csv(tmp_path, body)
        cams = parse_speed_cameras(path)
        assert len(cams) == 1
        assert "台北" in cams[0].address


# ---------- snap_camera_to_edge ----------


@pytest.mark.asyncio
async def test_snap_camera_to_edge_returns_id_when_found():
    cam = ParsedCamera(latitude=25.0, longitude=121.5,
                       direction="北向", speed_limit=50, address="x")
    fake_row = MagicMock()
    fake_row.id = 42
    exec_result = MagicMock()
    exec_result.first.return_value = fake_row

    fake_session = MagicMock()
    fake_session.execute = AsyncMock(return_value=exec_result)

    out = await snap_camera_to_edge(fake_session, cam)
    assert out == 42
    fake_session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_snap_camera_to_edge_returns_none_when_no_row():
    cam = ParsedCamera(latitude=25.0, longitude=121.5,
                       direction="北向", speed_limit=50, address="x")
    exec_result = MagicMock()
    exec_result.first.return_value = None

    fake_session = MagicMock()
    fake_session.execute = AsyncMock(return_value=exec_result)

    out = await snap_camera_to_edge(fake_session, cam)
    assert out is None


# ---------- seed_speed_cameras ----------


def _make_session_with_counts(speed_camera_count, traffic_edge_count=None):
    """Build a mocked AsyncSession whose successive `execute` awaits return:
       1) speed_camera count via scalar_one()
       2) traffic_edge count via scalar_one()      [only if provided]
       3+) snap_camera_to_edge .first() results    [supplied by caller-side patch]

    Returns (session, execute_mock) so the caller can assert call sequence.
    """
    sc_count_result = MagicMock()
    sc_count_result.scalar_one.return_value = speed_camera_count

    side = [sc_count_result]
    if traffic_edge_count is not None:
        edge_count_result = MagicMock()
        edge_count_result.scalar_one.return_value = traffic_edge_count
        side.append(edge_count_result)

    session = MagicMock()
    session.execute = AsyncMock(side_effect=side)
    session.add_all = MagicMock()
    session.commit = AsyncMock()
    return session


@pytest.mark.asyncio
async def test_seed_skips_when_table_already_populated(tmp_path):
    session = _make_session_with_counts(speed_camera_count=5)
    # parse_speed_cameras shouldn't even be called — but patch it just in case.
    with patch.object(sc_mod, "parse_speed_cameras") as mock_parse:
        await seed_speed_cameras(session, tmp_path / "cams.csv")
    mock_parse.assert_not_called()
    session.add_all.assert_not_called()
    session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_seed_no_op_when_csv_returns_empty(tmp_path):
    """Empty CSV (or missing file) -> no DB writes."""
    session = _make_session_with_counts(speed_camera_count=0)
    with patch.object(sc_mod, "parse_speed_cameras", return_value=[]):
        await seed_speed_cameras(session, tmp_path / "cams.csv")
    session.add_all.assert_not_called()
    session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_seed_skips_when_traffic_edge_empty(tmp_path):
    """Logs a warning and exits without inserting when traffic_edge is empty."""
    session = _make_session_with_counts(
        speed_camera_count=0, traffic_edge_count=0,
    )
    fake_cams = [
        ParsedCamera(latitude=25.04, longitude=121.51, direction="北",
                     speed_limit=50, address="A"),
    ]
    with patch.object(sc_mod, "parse_speed_cameras", return_value=fake_cams):
        await seed_speed_cameras(session, tmp_path / "cams.csv")
    session.add_all.assert_not_called()
    session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_seed_happy_path_inserts_two_rows_and_commits(tmp_path):
    """parse returns 2 rows, snap returns fixed edge_id, expect add_all + commit."""
    # Build a session with: count=0, edge_count=10, then 2 snap rows.
    sc_count_result = MagicMock()
    sc_count_result.scalar_one.return_value = 0
    edge_count_result = MagicMock()
    edge_count_result.scalar_one.return_value = 10

    snap_row1 = MagicMock()
    snap_row1.first.return_value = MagicMock(id=111)
    snap_row2 = MagicMock()
    snap_row2.first.return_value = MagicMock(id=222)

    session = MagicMock()
    session.execute = AsyncMock(
        side_effect=[sc_count_result, edge_count_result, snap_row1, snap_row2]
    )
    session.add_all = MagicMock()
    session.commit = AsyncMock()

    fake_cams = [
        ParsedCamera(latitude=25.04, longitude=121.51, direction="北",
                     speed_limit=50, address="A"),
        ParsedCamera(latitude=25.05, longitude=121.52, direction="南",
                     speed_limit=60, address="B"),
    ]

    with patch.object(sc_mod, "parse_speed_cameras", return_value=fake_cams):
        await seed_speed_cameras(session, tmp_path / "cams.csv")

    # add_all called exactly once with two SpeedCamera ORM objects
    session.add_all.assert_called_once()
    objs = session.add_all.call_args.args[0]
    assert len(objs) == 2
    # Each object should carry the snapped edge id from our mocked .first()
    edge_ids = sorted(int(o.nearest_edge_id) for o in objs)
    assert edge_ids == [111, 222]
    # Latitude/longitude/speed_limit faithfully copied
    lats = sorted(float(o.latitude) for o in objs)
    assert lats == [25.04, 25.05]
    speeds = sorted(int(o.speed_limit) for o in objs)
    assert speeds == [50, 60]

    session.commit.assert_awaited_once()
