## MODIFIED Requirements

### Requirement: 地名轉經緯度
系統 SHALL 提供 geocoding 功能，將地名查詢轉換為經緯度座標清單（支援多筆候選結果以利 autocomplete）。

#### Scenario: 成功查詢已知地點
- **WHEN** 以地名（如「台北車站」、「中正紀念堂」）呼叫 geocode 功能、傳入 `limit` 參數（預設 5、上限 10）
- **THEN** SHALL 回傳一個 list，長度 0 ~ `limit`，每個元素為 `{ latitude: float, longitude: float, display_name: string }`
- **AND** 若呼叫端傳入非空 `city_hint`，SHALL 將其附加到查詢字串尾端（例如 `"中正紀念堂 台北"`）以提升精度
- **AND** 若呼叫端未傳 `city_hint` 或傳 None / 空字串，SHALL 不附加任何 city 後綴、原樣查詢

#### Scenario: 查詢無結果
- **WHEN** 以無法辨識的地名呼叫 geocode 功能
- **THEN** SHALL 回傳空 list `[]`（**不再** 回 None；改 list 回傳是 BREAKING change，見 proposal）

#### Scenario: limit 上限
- **WHEN** 呼叫端傳入 `limit` 大於 10
- **THEN** SHALL clamp 至 10 後送 Nominatim API；SHALL NOT 拒絕請求

#### Scenario: Nominatim rate limit
- **WHEN** 連續發送多筆 geocode 請求
- **THEN** SHALL 在每次請求間間隔至少 1 秒，避免觸發 Nominatim rate limit

## ADDED Requirements

### Requirement: Geocoding Kafka handler
multiagent-service SHALL 註冊 `geocode.request` Kafka handler，將既有 `agents/geocoding.py` 包成 Kafka 觸發點，並把結果寫到 `geocode.response`。

#### Scenario: 收到 geocode 請求
- **WHEN** consumer 收到 `geocode.request` 訊息且 value 含 `correlation_id` 與 `query`
- **THEN** SHALL 以 `query` 為主要查詢、`city_hint`（可選，若無或空字串則不附加）、`limit`（可選，預設 5）呼叫 geocoding 模組
- **AND** SHALL 把回傳結果（最多 `limit` 筆 `{ latitude, longitude, display_name }`）包成 `geocode.response` 訊息（key 為原 correlation_id、value 含 `correlation_id`、`results`），發布到 `geocode.response` topic

#### Scenario: 缺少必要欄位
- **WHEN** `geocode.request` 訊息缺 `query` 欄位或其為空字串
- **THEN** SHALL 發 `geocode.response` 訊息含 `{ correlation_id, results: [], error: "query is required" }`

#### Scenario: Nominatim 失敗
- **WHEN** geocoding 模組因網路 / rate limit / 上游錯誤回傳 None 或拋例外
- **THEN** SHALL 發 `geocode.response` 訊息含 `{ correlation_id, results: [], error: "<簡短錯誤描述>" }`，SHALL NOT crash handler thread

### Requirement: 預設 city_hint 由呼叫端決定
geocoding 模組 SHALL NOT 在內部硬編任何預設 city 字串；是否附加 city、附加哪個 city 完全由呼叫端透過 `city_hint` 參數決定。

#### Scenario: 函數簽名
- **WHEN** 檢查 `agents/geocoding.py` 的 `geocode_location` 函數
- **THEN** 簽名 SHALL 為 `geocode_location(query: str, city_hint: str | None = None, limit: int = 5) -> list[dict]`
- **AND** 函數內部 SHALL NOT 出現任何寫死的 city 字串（例如 `"高雄"`、`"台北"`）作為自動附加邏輯

#### Scenario: 既有測試
- **WHEN** 執行 `uv run pytest tests/test_geocoding.py`
- **THEN** 所有測試 SHALL 通過
- **AND** 既有「自動附加『高雄』」相關斷言 SHALL 已被移除或改為驗證 `city_hint` 行為
