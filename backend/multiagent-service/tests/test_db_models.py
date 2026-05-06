"""Pure unit tests asserting that SQLAlchemy ORM classes in `src/db/models.py`
are coherent with the SQL schema files under `infra/init-db/*.sql`.

No DB / no Docker / no PostGIS -- everything is plain text parsing + ORM
introspection through `Table.columns`.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

import pytest
from geoalchemy2.types import Geometry
from sqlalchemy import Boolean, DateTime, Double, Integer, String

from src.db.models import (
    ParkingAvailability,
    ParkingLot,
    SpeedCamera,
    TrafficEdge,
    TrafficNode,
    VDReading,
    VDStatic,
)


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------

# tests/ -> multiagent-service/ -> backend/ -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]
_INIT_DB_DIR = _REPO_ROOT / "infra" / "init-db"

_ROAD_SQL = _INIT_DB_DIR / "02-road-network-tables.sql"
_VD_SQL = _INIT_DB_DIR / "03-vd-tables.sql"
_PARKING_SQL = _INIT_DB_DIR / "04-parking-tables.sql"
_MAXSPEED_SQL = _INIT_DB_DIR / "06-default-maxspeed-fn.sql"


# ---------------------------------------------------------------------------
# Minimal SQL parser: extract column name -> raw type string from CREATE TABLE.
# Skips CREATE INDEX, SELECT create_hypertable(...), CREATE EXTENSION, comments,
# PRIMARY KEY (...) constraint clauses, and REFERENCES inline FK suffixes.
# ---------------------------------------------------------------------------

_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\((.*?)\)\s*;",
    re.IGNORECASE | re.DOTALL,
)


def _strip_sql_comments(text: str) -> str:
    """Remove line comments (-- ...) so they don't pollute regex matches."""
    return re.sub(r"--[^\n]*", "", text)


def _parse_create_tables(sql_text: str) -> dict[str, dict[str, str]]:
    """Return {table_name: {col_name: raw_type_string}} for every CREATE TABLE.

    Raw type string is everything between the column name and the next comma /
    end-of-block, with NOT NULL / DEFAULT / REFERENCES suffixes stripped, so
    that downstream type checks see e.g. "VARCHAR(255)" or "DOUBLE PRECISION".
    """
    cleaned = _strip_sql_comments(sql_text)
    out: dict[str, dict[str, str]] = {}

    for match in _CREATE_TABLE_RE.finditer(cleaned):
        table = match.group(1).lower()
        body = match.group(2)
        cols: dict[str, str] = {}

        # Split on commas that are NOT inside parentheses (so geometry(Point, 4326)
        # and PRIMARY KEY (a, b) survive as single tokens).
        parts: list[str] = []
        depth = 0
        buf: list[str] = []
        for ch in body:
            if ch == "(":
                depth += 1
                buf.append(ch)
            elif ch == ")":
                depth -= 1
                buf.append(ch)
            elif ch == "," and depth == 0:
                parts.append("".join(buf).strip())
                buf = []
            else:
                buf.append(ch)
        if buf:
            parts.append("".join(buf).strip())

        for raw in parts:
            line = raw.strip()
            if not line:
                continue
            upper = line.upper()
            # Skip table-level constraints
            if upper.startswith(("PRIMARY KEY", "FOREIGN KEY", "UNIQUE", "CHECK", "CONSTRAINT")):
                continue
            # First token = column name, rest = type spec
            head, _, rest = line.partition(" ")
            col_name = head.strip().lower()
            type_str = rest.strip()
            # Strip trailing constraints from the column type string
            for kw in (" NOT NULL", " PRIMARY KEY", " DEFAULT ", " REFERENCES "):
                idx = type_str.upper().find(kw.strip())
                # Match as a whole-word boundary by requiring a preceding space
                m = re.search(r"\s+" + re.escape(kw.strip()) + r"\b", " " + type_str, re.IGNORECASE)
                if m:
                    type_str = (" " + type_str)[: m.start()].strip()
            cols[col_name] = type_str.strip()

        out[table] = cols

    return out


# Pre-parse the three SQL files once.
_ROAD_TABLES = _parse_create_tables(_ROAD_SQL.read_text(encoding="utf-8"))
_VD_TABLES = _parse_create_tables(_VD_SQL.read_text(encoding="utf-8"))
_PARKING_TABLES = _parse_create_tables(_PARKING_SQL.read_text(encoding="utf-8"))

_ALL_SQL_TABLES: dict[str, dict[str, str]] = {
    **_ROAD_TABLES,
    **_VD_TABLES,
    **_PARKING_TABLES,
}


# ---------------------------------------------------------------------------
# Type-mapping check: SQL raw type string -> ORM column type.
# ---------------------------------------------------------------------------


def _check_type_mapping(sql_type: str, orm_col) -> tuple[bool, str]:
    """Return (ok, reason). reason is empty when ok."""
    sql = sql_type.strip().upper()
    orm_type = orm_col.type

    # SERIAL or INTEGER -> Integer
    if sql == "INTEGER" or sql == "SERIAL":
        if isinstance(orm_type, Integer):
            return True, ""
        return False, f"SQL {sql_type!r} should map to Integer, got {type(orm_type).__name__}"

    # DOUBLE PRECISION -> Double
    if sql == "DOUBLE PRECISION":
        if isinstance(orm_type, Double):
            return True, ""
        return False, f"SQL {sql_type!r} should map to Double, got {type(orm_type).__name__}"

    # BOOLEAN -> Boolean
    if sql == "BOOLEAN":
        if isinstance(orm_type, Boolean):
            return True, ""
        return False, f"SQL {sql_type!r} should map to Boolean, got {type(orm_type).__name__}"

    # TIMESTAMPTZ -> DateTime(timezone=True)
    if sql == "TIMESTAMPTZ":
        if isinstance(orm_type, DateTime) and getattr(orm_type, "timezone", False):
            return True, ""
        return False, (
            f"SQL TIMESTAMPTZ should map to DateTime(timezone=True), got "
            f"{type(orm_type).__name__}(timezone={getattr(orm_type, 'timezone', None)!r})"
        )

    # VARCHAR(n) -> String(n)
    m = re.match(r"VARCHAR\s*\(\s*(\d+)\s*\)$", sql)
    if m:
        expected_len = int(m.group(1))
        if isinstance(orm_type, String) and orm_type.length == expected_len:
            return True, ""
        return False, (
            f"SQL VARCHAR({expected_len}) should map to String({expected_len}), "
            f"got {type(orm_type).__name__}(length="
            f"{getattr(orm_type, 'length', None)!r})"
        )

    # geometry(...) -> geoalchemy2 Geometry
    if sql.startswith("GEOMETRY"):
        if isinstance(orm_type, Geometry):
            return True, ""
        return False, f"SQL {sql_type!r} should map to Geometry, got {type(orm_type).__name__}"

    # Unknown SQL type -> skip silently (we only assert known mappings).
    return True, ""


# ---------------------------------------------------------------------------
# ORM <-> SQL pairs under test.
# ---------------------------------------------------------------------------

_PAIRS: list[tuple[type, str]] = [
    (TrafficNode, "traffic_node"),
    (TrafficEdge, "traffic_edge"),
    (SpeedCamera, "speed_camera"),
    (VDStatic, "vd_static"),
    (VDReading, "vd_reading"),
    (ParkingLot, "parking_lot"),
    (ParkingAvailability, "parking_availability"),
]


def _orm_column_names(orm_cls: type) -> set[str]:
    return set(orm_cls.__table__.columns.keys())


def _sql_column_names(sql_table_name: str) -> set[str]:
    return set(_ALL_SQL_TABLES[sql_table_name].keys())


# ---------------------------------------------------------------------------
# Sanity test: parser actually found every expected table.
# ---------------------------------------------------------------------------


def test_sql_parser_found_all_expected_tables():
    """Sanity check: every SQL table named in _PAIRS was parsed out."""
    expected: Iterable[str] = (name for _, name in _PAIRS)
    for table_name in expected:
        assert table_name in _ALL_SQL_TABLES, (
            f"Parser failed to extract CREATE TABLE for {table_name!r}. "
            f"Found tables: {sorted(_ALL_SQL_TABLES.keys())}"
        )


# ---------------------------------------------------------------------------
# Per-pair tests, parametrised so each pair shows up as its own test ID.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("orm_cls", "sql_table"), _PAIRS, ids=[p[1] for p in _PAIRS])
def test_every_sql_column_exists_on_orm(orm_cls: type, sql_table: str):
    """Every column in the SQL CREATE TABLE must exist on the ORM class."""
    sql_cols = _sql_column_names(sql_table)
    orm_cols = _orm_column_names(orm_cls)
    missing = sql_cols - orm_cols
    assert not missing, (
        f"{orm_cls.__name__} is missing columns present in SQL "
        f"`{sql_table}`: {sorted(missing)}"
    )


@pytest.mark.parametrize(("orm_cls", "sql_table"), _PAIRS, ids=[p[1] for p in _PAIRS])
def test_every_orm_column_exists_in_sql(orm_cls: type, sql_table: str):
    """Every ORM column must exist in the SQL schema (no ghost ORM fields)."""
    sql_cols = _sql_column_names(sql_table)
    orm_cols = _orm_column_names(orm_cls)
    extra = orm_cols - sql_cols
    assert not extra, (
        f"{orm_cls.__name__} declares ghost columns not in SQL "
        f"`{sql_table}`: {sorted(extra)}"
    )


@pytest.mark.parametrize(("orm_cls", "sql_table"), _PAIRS, ids=[p[1] for p in _PAIRS])
def test_orm_column_types_match_sql(orm_cls: type, sql_table: str):
    """For each shared column, ORM type must match the documented SQL mapping."""
    sql_cols = _ALL_SQL_TABLES[sql_table]
    orm_table = orm_cls.__table__
    failures: list[str] = []
    for col_name, sql_type in sql_cols.items():
        if col_name not in orm_table.columns:
            # column-name presence is asserted by a separate test; skip here.
            continue
        ok, reason = _check_type_mapping(sql_type, orm_table.columns[col_name])
        if not ok:
            failures.append(f"  - {col_name}: {reason}")
    assert not failures, (
        f"Type mismatches for {orm_cls.__name__} <-> {sql_table}:\n"
        + "\n".join(failures)
    )


# ---------------------------------------------------------------------------
# default_maxspeed(highway) PL/pgSQL function: required highway classes must
# map to a positive integer.
# ---------------------------------------------------------------------------

_REQUIRED_HIGHWAY_CLASSES: tuple[str, ...] = (
    "motorway",
    "trunk",
    "primary",
    "secondary",
    "tertiary",
    "residential",
    "service",
)


@pytest.mark.parametrize("highway_class", _REQUIRED_HIGHWAY_CLASSES)
def test_default_maxspeed_has_nonzero_mapping(highway_class: str):
    """default_maxspeed() must define each core highway class with a value > 0."""
    text = _MAXSPEED_SQL.read_text(encoding="utf-8")
    pattern = re.compile(
        r"WHEN\s+'" + re.escape(highway_class) + r"'\s+THEN\s+(\d+)",
        re.IGNORECASE,
    )
    match = pattern.search(text)
    assert match is not None, (
        f"default_maxspeed() is missing a CASE branch for highway class "
        f"{highway_class!r} in {_MAXSPEED_SQL}"
    )
    value = int(match.group(1))
    assert value > 0, (
        f"default_maxspeed() returns non-positive value {value} for highway "
        f"class {highway_class!r}"
    )
