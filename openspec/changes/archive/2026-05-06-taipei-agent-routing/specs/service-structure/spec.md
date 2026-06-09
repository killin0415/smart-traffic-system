## MODIFIED Requirements

### Requirement: multiagent-service 進入點不含 gRPC
multiagent-service 的主要進入點 SHALL 只啟動 FastAPI（HTTP）和 Kafka consumer — 不包含 gRPC server。

#### Scenario: 服務啟動元件
- **WHEN** multiagent-service 透過 `main.py` 啟動
- **THEN** SHALL 啟動 FastAPI HTTP server 和 Kafka consumer 背景任務，且 SHALL NOT 啟動 gRPC server

#### Scenario: Kafka consumer 訂閱的 topic
- **WHEN** multiagent-service 的 Kafka consumer 啟動
- **THEN** SHALL 訂閱由環境變數 `KAFKA_SUBSCRIBE_TOPICS` 指定的 topic 集合（comma-separated 字串）
- **AND** 當 `KAFKA_SUBSCRIBE_TOPICS` 未設定、為空字串、或全為空白時 SHALL 使用預設值 `chat.request,route.request`（涵蓋 chat agent 與 route planning 的入口）
- **AND** SHALL NOT 預設訂閱 `traffic.metrics`（系統不再從外部 YOLO 節點吃擁塞訊號，graph 動態權重統一由 TDX live polling 提供）；若部署端有需要，可在 env var 中加入重新啟用
