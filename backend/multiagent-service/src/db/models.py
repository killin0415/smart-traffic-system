from geoalchemy2 import Geometry
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Double,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class TrafficNode(Base):
    __tablename__ = "traffic_node"

    id = Column(Integer, primary_key=True, autoincrement=True)
    latitude = Column(Double, nullable=False)
    longitude = Column(Double, nullable=False)
    geom = Column(Geometry(geometry_type="POINT", srid=4326), nullable=True)
    has_signal = Column(Boolean, nullable=False, default=False, server_default="false")


class TrafficEdge(Base):
    __tablename__ = "traffic_edge"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_node_id = Column(Integer, ForeignKey("traffic_node.id"), nullable=False)
    target_node_id = Column(Integer, ForeignKey("traffic_node.id"), nullable=False)
    road_name = Column(String(255))
    length_km = Column(Double, nullable=False)
    road_class = Column(String(32))
    max_speed_kmh = Column(Integer)
    oneway = Column(Boolean, nullable=False, default=False, server_default="false")
    geom = Column(Geometry(geometry_type="LINESTRING", srid=4326), nullable=True)

    source_node = relationship("TrafficNode", foreign_keys=[source_node_id])
    target_node = relationship("TrafficNode", foreign_keys=[target_node_id])


class SpeedCamera(Base):
    __tablename__ = "speed_camera"

    id = Column(Integer, primary_key=True, autoincrement=True)
    latitude = Column(Double, nullable=False)
    longitude = Column(Double, nullable=False)
    direction = Column(String(64))
    speed_limit = Column(Integer, nullable=False)
    address = Column(String(255))
    nearest_edge_id = Column(Integer, ForeignKey("traffic_edge.id"), nullable=True)

    nearest_edge = relationship("TrafficEdge", foreign_keys=[nearest_edge_id])


class VDStatic(Base):
    __tablename__ = "vd_static"

    vdid = Column(String(64), primary_key=True)
    link_id = Column(String(64))
    road_name = Column(String(255))
    road_class = Column(String(32))
    bidirectional = Column(Boolean, nullable=False, default=False, server_default="false")
    bearing = Column(String(16))
    latitude = Column(Double, nullable=False)
    longitude = Column(Double, nullable=False)
    geom = Column(Geometry(geometry_type="POINT", srid=4326), nullable=True)
    snapped_road_class = Column(String(32))


class VDReading(Base):
    __tablename__ = "vd_reading"

    ts = Column(DateTime(timezone=True), primary_key=True, nullable=False)
    vdid = Column(String(64), primary_key=True, nullable=False)
    lane_no = Column(Integer, primary_key=True, nullable=False)
    avg_speed = Column(Double)
    volume = Column(Integer)
    occupancy = Column(Double)


class ParkingLot(Base):
    __tablename__ = "parking_lot"

    id = Column(Integer, primary_key=True, autoincrement=False)
    name = Column(String(255))
    address = Column(String(512))
    total_car = Column(Integer)
    total_motor = Column(Integer)
    latitude = Column(Double, nullable=False)
    longitude = Column(Double, nullable=False)
    geom = Column(Geometry(geometry_type="POINT", srid=4326), nullable=True)


class ParkingAvailability(Base):
    __tablename__ = "parking_availability"

    ts = Column(DateTime(timezone=True), primary_key=True, nullable=False)
    lot_id = Column(Integer, primary_key=True, nullable=False)
    available_car = Column(Integer)
    available_motor = Column(Integer)
