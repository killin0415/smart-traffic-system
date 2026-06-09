## ADDED Requirements

### Requirement: 地名轉經緯度
系統 SHALL 提供 geocoding 功能，將地名查詢轉換為經緯度座標。

#### Scenario: 成功查詢已知地點
- **WHEN** 以地名（如「夢時代」、「高雄火車站」）呼叫 geocode 功能
- **THEN** SHALL 回傳 `latitude`、`longitude`、`display_name`
- **AND** 查詢時 SHALL 自動附加「高雄」關鍵字以提升精度

#### Scenario: 查詢無結果
- **WHEN** 以無法辨識的地名呼叫 geocode 功能
- **THEN** SHALL 回傳 None 或錯誤訊息

#### Scenario: Nominatim rate limit
- **WHEN** 連續發送多筆 geocode 請求
- **THEN** SHALL 在每次請求間間隔至少 1 秒，避免觸發 Nominatim rate limit

### Requirement: Nominatim API 整合
系統 SHALL 使用 OpenStreetMap Nominatim API 作為 geocoding 後端。

#### Scenario: API 呼叫格式
- **WHEN** 發送 geocode 請求
- **THEN** SHALL 向 `https://nominatim.openstreetmap.org/search` 發送 GET 請求
- **AND** 參數 SHALL 包含 `q`（查詢字串）、`format=json`、`limit=1`
- **AND** request header SHALL 包含自定義 User-Agent 以遵守 Nominatim 使用條款

#### Scenario: API 失敗
- **WHEN** Nominatim API 回傳錯誤或逾時
- **THEN** SHALL 記錄 error log 並回傳 None
