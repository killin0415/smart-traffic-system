"""
啟動時自動 seed 路網資料。
偵測 traffic_node 是否為空，若空則從 JSON 快照匯入。
"""

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import TrafficEdge, TrafficNode
from src.db.road_network import DEFAULT_JSON_PATH, load_road_sections, parse_road_network

logger = logging.getLogger(__name__)


async def seed_road_network(session: AsyncSession) -> None:
    """偵測 DB 是否需要 seed，必要時從 JSON 匯入路網資料。"""
    result = await session.execute(select(func.count()).select_from(TrafficNode))
    count = result.scalar_one()

    if count > 0:
        logger.info("traffic_node 已有 %d 筆資料，跳過 seed", count)
        return

    if not DEFAULT_JSON_PATH.exists():
        logger.warning(
            "JSON 快照不存在: %s — 跳過路網 seed，服務繼續啟動",
            DEFAULT_JSON_PATH,
        )
        return

    logger.info("traffic_node 為空，開始從 JSON seed 路網資料...")
    sections = load_road_sections()
    network = parse_road_network(sections)

    # 寫入 nodes
    node_objs = [
        TrafficNode(id=n.id, latitude=n.latitude, longitude=n.longitude)
        for n in network.nodes
    ]
    session.add_all(node_objs)
    await session.flush()

    # 寫入 edges
    edge_objs = [
        TrafficEdge(
            source_node_id=e.source_node_id,
            target_node_id=e.target_node_id,
            road_name=e.road_name,
            length_km=e.length_km,
            speed_limit_kmh=e.speed_limit_kmh,
            base_weight=e.base_weight,
        )
        for e in network.edges
    ]
    session.add_all(edge_objs)
    await session.commit()

    logger.info("路網 seed 完成：%d nodes, %d edges", len(network.nodes), len(network.edges))
