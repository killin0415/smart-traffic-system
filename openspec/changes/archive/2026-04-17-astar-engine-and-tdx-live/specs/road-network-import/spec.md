## MODIFIED Requirements

### Requirement: 路網解析保留 TDX Section ID
`road_network.py` 的 `ParsedEdge` dataclass 及解析邏輯 SHALL 保留 TDX 的 `RoadSectionID`。

#### Scenario: ParsedEdge 包含 tdx_section_id
- **WHEN** 檢查 `ParsedEdge` dataclass
- **THEN** SHALL 包含 `tdx_section_id: str` 欄位

#### Scenario: 解析時提取 RoadSectionID
- **WHEN** `parse_road_network()` 處理一筆 road section
- **THEN** SHALL 從 section dict 的 `RoadSectionID` 欄位提取值，寫入 `ParsedEdge.tdx_section_id`

### Requirement: Seed 時寫入 tdx_section_id
`seed.py` 的 seed 邏輯 SHALL 將 `ParsedEdge.tdx_section_id` 寫入 `TrafficEdge.tdx_section_id`。

#### Scenario: seed 路網時包含 tdx_section_id
- **WHEN** `seed_road_network()` 建立 TrafficEdge ORM 物件
- **THEN** SHALL 從對應的 ParsedEdge 取得 `tdx_section_id` 並設定到 TrafficEdge
