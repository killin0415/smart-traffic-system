# 未來架構演進：高效能運算與 Rust 導入評估報告
**(Architecture Evolution: High-Performance Computing & Rust Migration)**

## 1. 系統現況與效能瓶頸分析

本系統目前採用 Kubernetes 部署，後端以 Spring Boot 為主，AI 與路徑運算則封裝於 FastAPI (Python) 中。此架構在專題初期能最大化開發效率，但在面對真實世界城市級別的圖資（Nodes 數量達百萬級）時，FastAPI 節點將面臨以下挑戰：

* **圖資記憶體開銷過大：** Python 的 `NetworkX` 等圖論套件在建立物件時會產生巨大的記憶體 Overhead。在 K8s 中，若多個 Worker 載入完整的城市圖資，極易觸發 OOM (Out of Memory) 被強制重啟。
* **A* 演算法的 CPU 密集運算：** Python 的 GIL (Global Interpreter Lock) 限制了多執行緒的平行運算能力。路徑搜尋屬於高度 CPU 密集型任務，純 Python 執行效率遠低於編譯型語言。

---

## 2. 核心模組語言選型評估

針對「AI 處理」與「路徑規劃」兩個截然不同的運算情境，未來的語言重構策略應予以拆分。

### ❌ AI Server (YOLO / LLM 推理)：維持 Python
* **原因：** 生態系綁定。YOLO (Ultralytics)、PyTorch 以及各式 LLM 的 MCP / SDK 皆以 Python 為第一級支援語言。
* **效能真相：** Python 在這裡只扮演「膠水語言」的角色，真正的矩陣運算與硬體加速（CUDA）都是由底層的 C++ / C 負責。將這部分改寫為 Rust 不僅開發成本極高，對推理速度的提升也微乎其微。

### ✅ 路徑規劃引擎 (Routing Engine)：強烈建議導入 Rust
* **原因：** 記憶體安全與極致效能。圖論演算法需要頻繁操作指標與圖結構。Rust 能夠以接近 C++ 的效能執行，同時避免記憶體洩漏與指標懸掛的問題。
* **平行處理優勢：** 結合 Rust 的 `Rayon` 套件，可以輕鬆將不同區域的權重更新操作（例如接收到 Kafka 串流時更新 Edges）平行化，徹底解決 Python GIL 帶來的效能天花板。
* **技術棧對齊：** 可使用 Rust 的 `petgraph` 套件來取代 Python 的 `NetworkX`，其記憶體佔用量與運算速度將有指數級的改善。



---

## 3. 漸進式重構計畫 (Phase-out Migration Strategy)

為了不影響現有系統的穩定性，未來導入 Rust 將採取「絞殺者模式 (Strangler Fig Pattern)」，將演算法模組從 FastAPI 中剝離。

* **Step 1: 架構解耦 (Decoupling)**
    * 將原本在 FastAPI 中的 `NetworkX` A* 演算法邏輯獨立出來。
    * 定義明確的 Protobuf 格式，確立 AI 模組與路由模組之間的資料交換標準。
* **Step 2: Rust 微服務建置 (Rust Microservice)**
    * 使用 Rust 的 Web 框架（如 `Actix-web` 或 `Axum`）建立一個專職的 Routing API。
    * 使用 `petgraph` 載入基礎靜態圖資。
* **Step 3: 內部通訊升級 (gRPC Integration)**
    * 廢棄原本 FastAPI 內部的函式呼叫。當 Spring Boot 接收到導航請求時，改為透過 gRPC 直接向 Rust Routing Service 請求路徑。
    * FastAPI 算出的動態權重（YOLO 擁塞指標、LLM 事件懲罰），同樣透過 Kafka 串流發送給 Rust 節點進行圖資記憶體的即時更新。