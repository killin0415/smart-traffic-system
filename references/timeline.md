Week 1-2    基礎設施整理
            - 移除 gRPC，統一 Kafka
            - 定義 topic schema (chat-request, chat-response, traffic-metrics...)
            - TimescaleDB + Redis docker-compose
            - TDX 路網資料抓取 & 匯入

Week 3-5    核心引擎 (multiagent-service)
            - Chat Manager (Kafka 消費 → 分派給 agent)
            - Route Agent (A* + 高雄路網圖資)
            - Explainer Agent (Gemini API 解釋路徑推論)
            - YOLO 用 mock 資料替代

Week 5-6    main-service API
            - /chat/message (Kafka 橋接 + correlation ID)
            - /route/recommend, /traffic/{id}/current
            - 基本 Auth (JWT，簡單做)

Week 7-8    前端 + 整合
            - React 或 Android，看時間決定
            - 地圖 + 聊天介面
            - 端到端串接 & demo 準備
