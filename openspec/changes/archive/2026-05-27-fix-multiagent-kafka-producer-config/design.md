## Context

main-service 透過 Kafka request topic 發出 chat、route、geocode request，multiagent-service 處理後透過 response topic 回傳結果。Docker Compose 已為 multiagent-service 設定 `KAFKA_BOOTSTRAP_SERVERS=kafka:29092`，且 consumer path 已讀取該 env var；producer path 目前固定 `localhost:9092`，導致 container 內 response publishing 指向錯誤 broker。

這是 deployment contract 問題，不是 topic schema 問題。HTTP controller、Kafka topic 名稱、payload schema 都不需要改。

## Goals / Non-Goals

**Goals:**

- 讓 multiagent-service 的 Kafka producer 與 consumer 使用同一個 `KAFKA_BOOTSTRAP_SERVERS` 設定來源。
- 保留未設定 env var 時的本機開發預設值 `localhost:9092`。
- 用 unit tests 鎖定 env var override 與預設值行為。
- 確認 handler response path 不會繞過可設定 producer。

**Non-Goals:**

- 不變更 Kafka topic、message schema 或 correlation ID contract。
- 不調整 main-service Kafka 設定，Spring Boot 已可由 `SPRING_KAFKA_BOOTSTRAP_SERVERS` override。
- 不新增 retry、batching、async flush 或 delivery QoS 改造。
- 不重構 Docker Compose 或 Kafka listener topology。

## Decisions

1. Producer 設定使用 env var lazy resolution。

   `get_producer()` 建立 singleton 時讀取 `os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")`。這避免 module import 時過早固定值，也讓 tests 可以 reset `_producer` 後用 monkeypatch 驗證不同 env。

2. 保留 producer singleton。

   現有 handlers 會頻繁呼叫 `publish_message()`；保留 singleton 可避免每次 response 都建立新的 confluent-kafka Producer。這次修正只改 broker resolution，不改 lifecycle model。

3. 測試直接驗證 Producer constructor config。

   `test_producer.py` 目前已 mock `Producer`，適合改成驗證 default 與 env override。這比端到端起 Kafka 更快，也直接覆蓋 regression root cause。

## Risks / Trade-offs

- [Risk] module-level `KAFKA_CONFIG` 若仍保留且在 import 時固定 env，測試可能錯過 runtime override。→ Mitigation: 將 config 建立包成函式或在 `get_producer()` 內建立 dict。
- [Risk] 已存在的 singleton 不會在 env var 變更後自動重建。→ Mitigation: production env 在 process start 前固定；tests 明確 reset `_producer`。
- [Risk] full Docker verification 需要 Kafka/DB/data-init，執行成本高。→ Mitigation: 先用 unit tests 鎖定 broker config，manual acceptance 再用 compose logs 驗證 response topics。
