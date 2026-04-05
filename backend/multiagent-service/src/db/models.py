from sqlalchemy import Column, Double, ForeignKey, Integer, String
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

    source_node = relationship("TrafficNode", foreign_keys=[source_node_id])
    target_node = relationship("TrafficNode", foreign_keys=[target_node_id])
