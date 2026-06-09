## ADDED Requirements

### Requirement: Configurable multiagent Kafka producer broker
multiagent-service 的 Kafka response producer SHALL 使用 `KAFKA_BOOTSTRAP_SERVERS` 作為 broker address 設定來源，並在未設定時預設為 `localhost:9092`。

#### Scenario: Docker broker override is honored
- **WHEN** multiagent-service 啟動時環境變數 `KAFKA_BOOTSTRAP_SERVERS` 設為 `kafka:29092`
- **THEN** Kafka producer SHALL 使用 `kafka:29092` 建立 producer client
- **AND** `chat.response`、`route.response`、`geocode.response` SHALL publish 到該 broker

#### Scenario: Local development default is preserved
- **WHEN** `KAFKA_BOOTSTRAP_SERVERS` 未設定
- **THEN** Kafka producer SHALL 使用 `localhost:9092` 作為預設 broker

#### Scenario: Consumer and producer share broker contract
- **WHEN** multiagent-service 同時 consume request topic 並 publish response topic
- **THEN** consumer 與 producer SHALL 讀取相同的 `KAFKA_BOOTSTRAP_SERVERS` contract
- **AND** response publishing path SHALL NOT hard-code a different broker address
