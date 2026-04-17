from sqlalchemy import Column, DateTime, Double, ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class TrafficNode(Base):
    __tablename__ = "traffic_node"

    id = Column(Integer, primary_key=True, autoincrement=True)
    latitude = Column(Double, nullable=False)
    longitude = Column(Double, nullable=False)


class TrafficEdge(Base):
    __tablename__ = "traffic_edge"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_node_id = Column(Integer, ForeignKey("traffic_node.id"), nullable=False)
    target_node_id = Column(Integer, ForeignKey("traffic_node.id"), nullable=False)
    road_name = Column(String(255))
    length_km = Column(Double, nullable=False)
    speed_limit_kmh = Column(Integer, nullable=False)
    base_weight = Column(Double, nullable=False)
    tdx_section_id = Column(String(64), nullable=True, index=True)

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


class TrafficHistory(Base):
    __tablename__ = "traffic_history"

    time = Column(DateTime(timezone=True), primary_key=True, nullable=False)
    tdx_section_id = Column(String(64), primary_key=True, nullable=False)
    travel_speed = Column(Double)
    travel_time = Column(Double)
